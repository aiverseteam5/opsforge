"""Tests for HMAC token hashing, token_version gating, and delegation JWTs.

Pure unit tests run anywhere. DB-backed tests are skipped when Compose is down.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid

import jwt
import pytest

from opsforge.config import get_settings
from opsforge.delegation import mint_delegation_token, verify_delegation_token
from opsforge.policy import check_tool_call
from opsforge.security import hash_token


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _set_hmac_secret(key_bytes: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(key_bytes).decode()


class _HmacSecret:
    """Context manager: temporarily set OPSFORGE_TOKEN_HMAC_SECRET."""

    def __init__(self, key_bytes: bytes | None = None):
        self._key = key_bytes or os.urandom(32)
        self._orig: str | None = None

    @property
    def key(self) -> bytes:
        return self._key

    @property
    def secret(self) -> str:
        return _set_hmac_secret(self._key)

    def __enter__(self) -> "_HmacSecret":
        self._orig = os.environ.get("OPSFORGE_TOKEN_HMAC_SECRET")
        os.environ["OPSFORGE_TOKEN_HMAC_SECRET"] = self.secret
        get_settings.cache_clear()
        return self

    def __exit__(self, *_):
        if self._orig is None:
            os.environ.pop("OPSFORGE_TOKEN_HMAC_SECRET", None)
        else:
            os.environ["OPSFORGE_TOKEN_HMAC_SECRET"] = self._orig
        get_settings.cache_clear()


class _NoHmacSecret:
    """Context manager: temporarily clear OPSFORGE_TOKEN_HMAC_SECRET."""

    def __init__(self):
        self._orig: str | None = None

    def __enter__(self) -> "_NoHmacSecret":
        self._orig = os.environ.pop("OPSFORGE_TOKEN_HMAC_SECRET", None)
        get_settings.cache_clear()
        return self

    def __exit__(self, *_):
        if self._orig is not None:
            os.environ["OPSFORGE_TOKEN_HMAC_SECRET"] = self._orig
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# hash_token — pure unit tests
# --------------------------------------------------------------------------- #


def test_hash_token_dev_fallback_is_sha256():
    """Without OPSFORGE_TOKEN_HMAC_SECRET, hash_token falls back to plain SHA-256."""
    with _NoHmacSecret():
        result = hash_token("ofg_testtoken")
    expected = hashlib.sha256("ofg_testtoken".encode()).hexdigest()
    assert result == expected


def test_hash_token_hmac_binding():
    """With OPSFORGE_TOKEN_HMAC_SECRET set, hash_token uses HMAC-SHA256."""
    key = os.urandom(32)
    with _HmacSecret(key):
        result = hash_token("ofg_testtoken")
    expected = hmac.new(key, "ofg_testtoken".encode(), hashlib.sha256).hexdigest()
    assert result == expected
    # Must differ from plain SHA-256 — the HMAC key is the binding.
    assert result != hashlib.sha256("ofg_testtoken".encode()).hexdigest()


def test_hash_token_hmac_is_deterministic():
    """Same key + same input always produces the same hash."""
    with _HmacSecret():
        h1 = hash_token("ofg_abc")
        h2 = hash_token("ofg_abc")
    assert h1 == h2


def test_hash_token_different_keys_produce_different_hashes():
    """Different HMAC keys produce different hashes for identical tokens."""
    raw = "ofg_sametoken"
    with _HmacSecret(os.urandom(32)):
        h1 = hash_token(raw)
    with _HmacSecret(os.urandom(32)):
        h2 = hash_token(raw)
    assert h1 != h2


def test_hash_token_hmac_differs_from_sha256_fallback():
    """An HMAC hash never collides with the plain-SHA-256 fallback for the same input."""
    raw = "ofg_collisioncheck"
    sha256_hash = hashlib.sha256(raw.encode()).hexdigest()
    # With overwhelming probability a random key won't produce the same digest.
    with _HmacSecret():
        hmac_hash = hash_token(raw)
    assert hmac_hash != sha256_hash


# --------------------------------------------------------------------------- #
# token_version=0 rejection — requires Compose DB
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("db_required")
async def test_old_sha256_hash_rejected():
    """A token_version=0 (plain SHA-256) row is rejected even with the right raw token.

    After migration 0024 the lookup SQL requires token_version=1; a legacy
    SHA-256 hash stored at version 0 must always return 401.
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from opsforge.db import session_factory
    from opsforge.main import app

    raw = "ofg_legacy_" + uuid.uuid4().hex
    sha256_hash = hashlib.sha256(raw.encode()).hexdigest()
    org_id = get_settings().org_id

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name, token_version) "
                "VALUES (:org, :hash, :name, 0)"
            ),
            {"org": org_id, "hash": sha256_hash, "name": f"legacy-{uuid.uuid4().hex}"},
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {raw}"}
        )

    assert resp.status_code == 401


@pytest.mark.usefixtures("db_required")
async def test_hmac_hash_accepted_at_version_1():
    """A token_version=1 row with an HMAC hash is accepted by require_token()."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from opsforge.db import session_factory
    from opsforge.main import app
    from opsforge.security import generate_token

    raw, token_hash = generate_token()
    org_id = get_settings().org_id

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name, token_version) "
                "VALUES (:org, :hash, :name, 1)"
            ),
            {"org": org_id, "hash": token_hash, "name": f"hmac-{uuid.uuid4().hex}"},
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {raw}"}
        )

    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Delegation token mint + verify — pure unit tests
# --------------------------------------------------------------------------- #


def _make_ids():
    return str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def test_delegation_token_round_trip():
    """mint → verify succeeds and preserves all claims."""
    run_id, sub_run_id, org_id = _make_ids()
    scope = ["datadog.get_metrics", "pagerduty.list_incidents"]

    token, jti = mint_delegation_token(
        run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=scope
    )
    claims = verify_delegation_token(token, org_id)

    assert claims["iss"] == run_id
    assert claims["sub"] == sub_run_id
    assert claims["org_id"] == org_id
    assert claims["scope"] == scope
    assert claims["jti"] == jti


def test_delegation_token_wrong_org_rejected():
    """verify_delegation_token raises ValueError when org_id does not match."""
    token, _ = mint_delegation_token(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id="org-a",
        scope=["x.y"],
    )
    with pytest.raises(ValueError, match="org_id mismatch"):
        verify_delegation_token(token, "org-b")


def test_delegation_token_exp_capped_at_15_min():
    """exp_seconds > 900 is silently capped to 900."""
    token, _ = mint_delegation_token(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id="test-org",
        scope=["x.y"],
        exp_seconds=9999,
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] <= 900


def test_delegation_token_jti_is_unique_per_mint():
    """Each call to mint_delegation_token produces a distinct jti."""
    kwargs = dict(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id="test-org",
        scope=["x.y"],
    )
    _, jti1 = mint_delegation_token(**kwargs)
    _, jti2 = mint_delegation_token(**kwargs)
    assert jti1 != jti2


def test_delegation_token_tampered_signature_rejected():
    """Flipping a byte in the JWT body fails signature verification."""
    token, _ = mint_delegation_token(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id="my-org",
        scope=["x.y"],
    )
    # Corrupt the payload segment (middle of the three JWT parts).
    header, payload, sig = token.split(".")
    corrupted = header + "." + payload[:-4] + "XXXX" + "." + sig
    with pytest.raises(jwt.PyJWTError):
        verify_delegation_token(corrupted, "my-org")


# --------------------------------------------------------------------------- #
# Delegation JWT in require_token() — requires Compose DB
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("db_required")
async def test_delegation_jwt_accepted_by_require_token():
    """A valid delegation JWT with a matching jti in delegation_tokens passes auth."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from opsforge.db import session_factory
    from opsforge.main import app

    org_id = get_settings().org_id
    run_id, sub_run_id = str(uuid.uuid4()), str(uuid.uuid4())
    scope = ["datadog.get_metrics"]

    token, jti = mint_delegation_token(
        run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=scope
    )

    from datetime import UTC, datetime, timedelta
    import json

    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO delegation_tokens "
                "(jti, org_id, iss, sub, scope, expires_at) "
                "VALUES (:jti, :org, :iss, :sub, CAST(:scope AS json), :exp)"
            ),
            {
                "jti": jti,
                "org": org_id,
                "iss": run_id,
                "sub": sub_run_id,
                "scope": json.dumps(scope),
                "exp": expires_at,
            },
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {token}"}
        )

    # Auth passed (delegation token accepted). The endpoint may return 403 if
    # it requires a role that delegation tokens don't carry — that's fine; the
    # relevant assertion is that it is NOT a 401 (auth failure).
    assert resp.status_code != 401


@pytest.mark.usefixtures("db_required")
async def test_delegation_jwt_with_unknown_jti_rejected():
    """A valid JWT whose jti was never inserted into delegation_tokens → 401."""
    from httpx import ASGITransport, AsyncClient

    from opsforge.main import app

    org_id = get_settings().org_id
    token, _ = mint_delegation_token(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id=org_id,
        scope=["x.y"],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 401


@pytest.mark.usefixtures("db_required")
async def test_delegation_jwt_revoked_rejected():
    """A JWT whose jti has revoked_at set is rejected even if otherwise valid."""
    from datetime import UTC, datetime, timedelta
    import json

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from opsforge.db import session_factory
    from opsforge.main import app

    org_id = get_settings().org_id
    run_id, sub_run_id = str(uuid.uuid4()), str(uuid.uuid4())
    token, jti = mint_delegation_token(
        run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=["x.y"]
    )

    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO delegation_tokens "
                "(jti, org_id, iss, sub, scope, expires_at, revoked_at) "
                "VALUES (:jti, :org, :iss, :sub, CAST(:scope AS json), :exp, now())"
            ),
            {
                "jti": jti,
                "org": org_id,
                "iss": run_id,
                "sub": sub_run_id,
                "scope": json.dumps(["x.y"]),
                "exp": expires_at,
            },
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Scope enforcement in policy.check_tool_call — pure unit
# --------------------------------------------------------------------------- #

_MANIFEST = {
    "tools": [{"tool": "datadog.get_metrics", "class": "read_only"}],
    "proposals": [],
    "policy": {},
}


def test_scope_none_does_not_restrict():
    """scope=None (regular API token, no delegation) never triggers the scope gate."""
    trace = check_tool_call(_MANIFEST, "datadog.get_metrics", scope=None)
    assert trace["allowed"] is True


def test_scope_permits_listed_tool():
    """A tool inside the delegation scope clears the scope gate."""
    trace = check_tool_call(
        _MANIFEST, "datadog.get_metrics", scope=["datadog.get_metrics"]
    )
    assert trace["allowed"] is True


def test_scope_blocks_unlisted_tool():
    """A tool absent from the delegation scope is denied before the manifest is checked."""
    trace = check_tool_call(
        _MANIFEST, "datadog.get_metrics", scope=["pagerduty.list_incidents"]
    )
    assert trace["allowed"] is False
    assert "scope_not_permitted" in trace["rules"]


def test_scope_check_fires_before_manifest_check():
    """scope_not_permitted is reported even for tools that aren't in the manifest."""
    trace = check_tool_call(_MANIFEST, "unknown.tool", scope=["other.tool"])
    assert trace["allowed"] is False
    assert "scope_not_permitted" in trace["rules"]
    # Must NOT say "tool_not_in_manifest" — scope gate fired first.
    assert "tool_not_in_manifest" not in trace["rules"]


# --------------------------------------------------------------------------- #
# Phase 5a: 401 WWW-Authenticate header for invalid tokens (T6)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("db_required")
async def test_invalid_token_returns_www_authenticate_header():
    """401 for a bad API token includes a WWW-Authenticate header with error_description."""
    from httpx import ASGITransport, AsyncClient

    from opsforge.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/runs",
            headers={"Authorization": "Bearer ofg_nosuchatoken_abc123"},
        )

    assert resp.status_code == 401
    www_auth = resp.headers.get("www-authenticate", "")
    assert "error_description" in www_auth
    assert "re-issuance" in www_auth


# --------------------------------------------------------------------------- #
# Phase 5a: delegation key decoupling (T8)
# --------------------------------------------------------------------------- #


class _DelegationKey:
    """Context manager: temporarily set OPSFORGE_DELEGATION_SIGNING_KEY."""

    def __init__(self, key_bytes: bytes | None = None):
        import base64
        self._key = key_bytes or os.urandom(32)
        self._secret = base64.urlsafe_b64encode(self._key).decode()
        self._orig: str | None = None

    @property
    def key(self) -> bytes:
        return self._key

    def __enter__(self) -> "_DelegationKey":
        from opsforge import delegation as _d
        self._orig = os.environ.get("OPSFORGE_DELEGATION_SIGNING_KEY")
        os.environ["OPSFORGE_DELEGATION_SIGNING_KEY"] = self._secret
        get_settings.cache_clear()
        _d._key_fallback_warned = False
        return self

    def __exit__(self, *_):
        from opsforge import delegation as _d
        if self._orig is None:
            os.environ.pop("OPSFORGE_DELEGATION_SIGNING_KEY", None)
        else:
            os.environ["OPSFORGE_DELEGATION_SIGNING_KEY"] = self._orig
        get_settings.cache_clear()
        _d._key_fallback_warned = False


def test_delegation_uses_dedicated_signing_key():
    """When OPSFORGE_DELEGATION_SIGNING_KEY is set, tokens are signed with that key."""
    run_id, sub_run_id, org_id = _make_ids()
    with _DelegationKey():
        token, _ = mint_delegation_token(
            run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=["x.y"]
        )
        # Must verify with the same key in context.
        claims = verify_delegation_token(token, org_id)
    assert claims["org_id"] == org_id


def test_delegation_fallback_to_hmac_secret_when_no_delegation_key():
    """When OPSFORGE_DELEGATION_SIGNING_KEY is unset, falls back to OPSFORGE_TOKEN_HMAC_SECRET."""
    run_id, sub_run_id, org_id = _make_ids()
    with _HmacSecret():
        # Ensure delegation key is cleared for this test.
        os.environ.pop("OPSFORGE_DELEGATION_SIGNING_KEY", None)
        from opsforge import delegation as _d
        _d._key_fallback_warned = False
        get_settings.cache_clear()
        token, _ = mint_delegation_token(
            run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=["x.y"]
        )
        claims = verify_delegation_token(token, org_id)
    assert claims["org_id"] == org_id


def test_delegation_token_with_new_key_fails_under_old_hmac_key():
    """Tokens minted with OPSFORGE_DELEGATION_SIGNING_KEY fail to verify with the fallback key."""
    run_id, sub_run_id, org_id = _make_ids()

    with _DelegationKey() as new_key_ctx:
        token, _ = mint_delegation_token(
            run_id=run_id, sub_run_id=sub_run_id, org_id=org_id, scope=["x.y"]
        )

    # Token was minted with new_key. Now verify under the fallback path (token_hmac_secret).
    os.environ.pop("OPSFORGE_DELEGATION_SIGNING_KEY", None)
    get_settings.cache_clear()
    with _HmacSecret():
        with pytest.raises(jwt.PyJWTError):
            verify_delegation_token(token, org_id)


def test_signing_key_raises_runtime_error_in_nondev_without_keys():
    """_signing_key() raises RuntimeError when both keys are absent in a non-dev environment."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from opsforge import delegation as _d
    from opsforge.delegation import _signing_key

    fake_settings = SimpleNamespace(
        delegation_signing_key="",
        token_hmac_secret="",
        environment="production",
    )
    with patch("opsforge.delegation.get_settings", return_value=fake_settings):
        with pytest.raises(RuntimeError, match="OPSFORGE_DELEGATION_SIGNING_KEY must be set"):
            _signing_key()


def test_signing_key_fallback_warning_fires_only_once():
    """_key_fallback_warned dedup: the fallback WARNING is logged at most once per process."""
    import base64
    from types import SimpleNamespace
    from unittest.mock import patch

    from opsforge import delegation as _d
    from opsforge.delegation import _signing_key

    hmac_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    fake_settings = SimpleNamespace(
        delegation_signing_key="",
        token_hmac_secret=hmac_key,
        environment="production",
    )
    _d._key_fallback_warned = False
    try:
        with patch("opsforge.delegation.get_settings", return_value=fake_settings):
            with patch.object(_d._log, "warning") as mock_warn:
                _signing_key()
                _signing_key()
                _signing_key()
            assert mock_warn.call_count == 1, (
                f"expected warning exactly once, got {mock_warn.call_count}"
            )
    finally:
        _d._key_fallback_warned = False
