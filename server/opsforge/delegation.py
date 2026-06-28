"""Delegation token mint + verify for A2A trust boundary.

Delegation tokens are short-lived JWTs (max 15 min) issued by a run to
authorize a sub-agent to act on its behalf within a bounded tool scope.
They are signed with OPSFORGE_DELEGATION_SIGNING_KEY (HS256). When that key
is not set, the code falls back to OPSFORGE_TOKEN_HMAC_SECRET and logs a
WARNING in non-dev environments — set the dedicated key to decouple rotation.

Deployment note: when OPSFORGE_DELEGATION_SIGNING_KEY is first set, tokens
minted under the old key fail for up to 15 minutes (the max delegation token
TTL). Schedule the key rotation during low-traffic or drain active delegation
tokens first.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from .config import get_settings

_log = logging.getLogger("opsforge.delegation")

_MAX_EXP_SECONDS = 900  # 15-minute hard cap

_DEV_KEY = b"dev-delegation-key-not-for-production-use"

# Module-level flag: emit the fallback WARNING at most once per process to
# avoid log spam on every token mint/verify call.
_key_fallback_warned = False


def _signing_key() -> bytes:
    global _key_fallback_warned
    s = get_settings()
    if s.delegation_signing_key:
        return base64.urlsafe_b64decode(s.delegation_signing_key)
    if s.token_hmac_secret:
        if s.environment != "dev" and not _key_fallback_warned:
            _log.warning(
                "OPSFORGE_DELEGATION_SIGNING_KEY not set — falling back to "
                "OPSFORGE_TOKEN_HMAC_SECRET for delegation token signing. "
                "Set OPSFORGE_DELEGATION_SIGNING_KEY to decouple key rotation."
            )
            _key_fallback_warned = True
        return base64.urlsafe_b64decode(s.token_hmac_secret)
    if s.environment != "dev":
        raise RuntimeError(
            "OPSFORGE_DELEGATION_SIGNING_KEY must be set in non-dev environments. "
            "Generate with: python -c \"import os,base64; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )
    return _DEV_KEY


def mint_delegation_token(
    *,
    run_id: str,
    sub_run_id: str,
    org_id: str,
    scope: list[str],
    exp_seconds: int = 900,
) -> tuple[str, str]:
    """Mint a delegation JWT. Returns (jwt_str, jti).

    exp_seconds is capped at 900 (15 min). The jti must be written to
    delegation_tokens by the caller before the token is handed out.
    """
    exp_seconds = min(exp_seconds, _MAX_EXP_SECONDS)
    jti = str(uuid.uuid4())
    now = datetime.now(UTC)
    payload: dict = {
        "iss": run_id,
        "sub": sub_run_id,
        "org_id": org_id,
        "scope": scope,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_seconds)).timestamp()),
    }
    token = jwt.encode(payload, _signing_key(), algorithm="HS256")
    return token, jti


def verify_delegation_token(jwt_str: str, expected_org_id: str | None = None) -> dict:
    """Verify a delegation JWT and return its claims.

    Raises jwt.PyJWTError on invalid/expired tokens.
    Raises ValueError if expected_org_id is provided and does not match the claim.
    """
    claims: dict = jwt.decode(
        jwt_str,
        _signing_key(),
        algorithms=["HS256"],
        options={"require": ["iss", "sub", "org_id", "scope", "jti", "exp", "iat"]},
    )
    if expected_org_id is not None and claims.get("org_id") != expected_org_id:
        raise ValueError(
            f"org_id mismatch: token={claims.get('org_id')!r} "
            f"expected={expected_org_id!r}"
        )
    return claims
