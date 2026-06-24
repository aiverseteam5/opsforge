"""A2 — connector configuration + credential capture + edit/disconnect.

The credential-safety contract, by tests: the credential goes to the Fernet vault and is
NEVER returned by any API (list/detail/test/patch), never audited; edit is write-only
(rotate=overwrite, blank=keep); a cross-workspace write is impossible (A1.5 FORCE RLS);
disconnect purges the credential.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import api_client
from cryptography.fernet import Fernet
from sqlalchemy import text

from opsforge.security import generate_token

pytestmark = pytest.mark.usefixtures("db_required")

SECRET = "sk-CONNECTOR-SECRET-xyz"


@pytest.fixture
def vault():
    """A live Fernet key so credential encryption works in-process for these tests."""
    from opsforge.config import get_settings

    prev = os.environ.get("OPSFORGE_FERNET_KEY")
    os.environ["OPSFORGE_FERNET_KEY"] = Fernet.generate_key().decode()
    get_settings.cache_clear()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OPSFORGE_FERNET_KEY", None)
        else:
            os.environ["OPSFORGE_FERNET_KEY"] = prev
        get_settings.cache_clear()


async def _token(org: str, role: str = "operator") -> dict[str, str]:
    from opsforge.db import session_factory

    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        uid = (
            await s.execute(
                text("INSERT INTO users (org_id,email,name,role) "
                     "VALUES (:o,:e,'t',:r) RETURNING id"),
                {"o": org, "e": f"{uuid.uuid4().hex}@t.local", "r": role},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO api_tokens (org_id,user_id,token_hash,name) VALUES (:o,:u,:h,'t')"),
            {"o": org, "u": uid, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


async def _create(headers, name="snow-a2", creds=None):
    async with api_client() as c:
        r = await c.post("/api/v1/connectors", headers=headers, json={
            "name": name, "kind": "servicenow", "transport": "http",
            "endpoint": "http://stub.local", "tool_allowlist": [],
            "credentials": creds if creds is not None else {"api_key": SECRET},
        })
    assert r.status_code == 201, r.text
    return r.json()


async def _cred_blob(org, cid):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        return (await s.execute(
            text("SELECT credentials_enc FROM connectors WHERE id=:i"),
            {"i": cid})).scalar_one_or_none()


async def _status(org, cid):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        return (await s.execute(
            text("SELECT status FROM connectors WHERE id=:i"), {"i": cid})).scalar_one_or_none()


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        # audit_log is append-only (reject_mutation trigger) — left as harmless residue;
        # every test uses a fresh org uuid so nothing cross-contaminates.
        for t in ("connectors", "api_tokens", "users"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


# --------------------------------------------------------------------------- #
# the catalog declares the form (structured fields + secret flag + instance id)
# --------------------------------------------------------------------------- #
async def test_catalog_detail_declares_config_fields_and_instance_id(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            d = (await c.get("/api/v1/catalog/servicenow", headers=headers)).json()
            names = {f["name"]: f["secret"] for f in d["config_fields"]}  # name->secret
            assert names.get("credential") is True  # the secret is backend-declared
            assert names.get("endpoint") is False
            assert d["instance_id"] is None  # not configured yet

            cid = (await _create(headers))["id"]
            d2 = (await c.get("/api/v1/catalog/servicenow", headers=headers)).json()
            assert d2["instance_id"] == cid  # now points at the workspace's instance
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the credential goes to the vault and is NEVER returned
# --------------------------------------------------------------------------- #
async def test_credential_vaulted_and_never_returned(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        created = await _create(headers)
        cid = created["id"]
        async with api_client() as c:
            listed = (await c.get("/api/v1/connectors", headers=headers)).json()
            tested = await c.post(f"/api/v1/connectors/{cid}/test", headers=headers)
            detail = (await c.get("/api/v1/catalog/servicenow", headers=headers)).json()
        # the secret appears in NO response the browser holds
        for blob in (created, listed, tested.json(), detail):
            assert SECRET not in str(blob)
            assert "credentials" not in str(blob)
        # …but it IS in the vault as ciphertext (not plaintext)
        enc = await _cred_blob(org, cid)
        assert enc is not None and SECRET.encode() not in bytes(enc)
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# edit is write-only: rotate overwrites, blank keeps
# --------------------------------------------------------------------------- #
async def test_update_rotates_or_keeps_credential_write_only(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        cid = (await _create(headers))["id"]
        before = bytes(await _cred_blob(org, cid))
        async with api_client() as c:
            # edit config WITHOUT a credential → keep the existing vault secret
            r1 = await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                               json={"name": "snow-renamed"})
            assert r1.status_code == 200 and r1.json()["name"] == "snow-renamed"
            assert SECRET not in str(r1.json())
            kept = bytes(await _cred_blob(org, cid))
            assert kept == before  # blank → unchanged

            # rotate the credential → overwrite (new ciphertext), still never returned
            r2 = await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                               json={"credentials": {"api_key": "sk-ROTATED-9"}})
            assert r2.status_code == 200 and "sk-ROTATED-9" not in str(r2.json())
            rotated = bytes(await _cred_blob(org, cid))
            assert rotated != before and b"sk-ROTATED-9" not in rotated
    finally:
        await _cleanup(org)


async def test_update_audit_records_rotation_not_the_secret(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        cid = (await _create(headers))["id"]
        async with api_client() as c:
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                          json={"credentials": {"api_key": "sk-AUDIT-SECRET"}})
        from opsforge.db import scope_to_org, session_factory
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            rows = (await s.execute(
                text("SELECT event, detail FROM audit_log "
                     "WHERE org_id=:o AND event='connector.updated'"),
                {"o": org})).all()
        assert rows and rows[-1].detail.get("credential_rotated") is True
        assert "sk-AUDIT-SECRET" not in str([dict(r._mapping) for r in rows])
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# write isolation: A cannot create/modify B's connector (A1.5 FORCE RLS)
# --------------------------------------------------------------------------- #
async def test_cross_workspace_write_is_blocked(vault):
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    headers_a, headers_b = await _token(org_a), await _token(org_b)
    try:
        b_cid = (await _create(headers_b, name="snow-b"))["id"]
        async with api_client() as c:
            # A edits B's connector → not found (RLS + predicate)
            patch = await c.patch(f"/api/v1/connectors/{b_cid}", headers=headers_a,
                                  json={"name": "hijacked", "credentials": {"api_key": "evil"}})
            assert patch.status_code == 404
            dele = await c.delete(f"/api/v1/connectors/{b_cid}", headers=headers_a)
            assert dele.status_code == 404
        # B's connector is untouched (name + credential intact)
        async with api_client() as c:
            b_list = (await c.get("/api/v1/connectors", headers=headers_b)).json()
        assert any(x["id"] == b_cid and x["name"] == "snow-b" for x in b_list)
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)


# --------------------------------------------------------------------------- #
# disconnect purges the credential
# --------------------------------------------------------------------------- #
async def test_disconnect_purges_credential(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        cid = (await _create(headers))["id"]
        assert await _cred_blob(org, cid) is not None
        async with api_client() as c:
            r = await c.delete(f"/api/v1/connectors/{cid}", headers=headers)
        assert r.status_code == 204
        assert await _cred_blob(org, cid) is None  # row + credential gone
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# F1: a validation error must NOT echo the submitted credential back
# --------------------------------------------------------------------------- #
async def test_validation_error_never_leaks_credential(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            # (a) credentials sent as a bare string → 422; the string must not be echoed
            r1 = await c.post("/api/v1/connectors", headers=headers, json={
                "name": "x", "kind": "servicenow", "transport": "http",
                "endpoint": "http://x", "credentials": SECRET})
            assert r1.status_code == 422 and SECRET not in r1.text
            # (b) a required field omitted while credentials present → pydantic puts the WHOLE
            # body in `input`; the credential must not ride along into the 422
            r2 = await c.post("/api/v1/connectors", headers=headers, json={
                "transport": "http", "endpoint": "http://x",
                "credentials": {"api_key": SECRET}})
            assert r2.status_code == 422 and SECRET not in r2.text
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# Phase B: a Confluence credential ROTATION via PATCH actually replaces the token
# (the form key api_key must map to CONFLUENCE_TOKEN on edit, not land under a dead key)
# --------------------------------------------------------------------------- #
async def test_confluence_credential_rotation_updates_the_real_token(vault):
    from opsforge.connectors import _decrypt_credentials

    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            cid = (await c.post("/api/v1/connectors", headers=headers, json={
                "name": "conf", "kind": "confluence", "transport": "stdio",
                "endpoint": "http://127.0.0.1:1",  # unreachable → fast unhealthy, no real net
                "credentials": {"api_key": "email:OLD", "space": "OPS"}})).json()["id"]
            # the form fields are stored under the ENV keys the MCP server reads
            creds0 = _decrypt_credentials(await _cred_blob(org, cid))
            assert creds0["CONFLUENCE_TOKEN"] == "email:OLD"
            assert creds0["CONFLUENCE_BASE_URL"] == "http://127.0.0.1:1"
            assert "api_key" not in creds0  # no dead key
            # rotate the token (the compromised-token scenario)
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                          json={"credentials": {"api_key": "email:NEW"}})
        creds1 = _decrypt_credentials(await _cred_blob(org, cid))
        assert creds1["CONFLUENCE_TOKEN"] == "email:NEW"          # rotated under the RIGHT key
        assert creds1["CONFLUENCE_BASE_URL"] == "http://127.0.0.1:1"  # base kept (merge)
        assert "api_key" not in creds1  # the rotation did NOT leave the old token in effect
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# F2: a credential-bearing connector cannot be created with a blank vault (fail closed)
# --------------------------------------------------------------------------- #
async def test_blank_credential_rejected_for_auth_kind(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            r = await c.post("/api/v1/connectors", headers=headers, json={
                "name": "no-cred", "kind": "servicenow", "transport": "http",
                "endpoint": "http://x", "tool_allowlist": []})  # no credentials
        assert r.status_code == 400 and "credential" in r.text.lower()
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# F4: a connectivity-affecting edit resets status so the badge stops claiming connected
# --------------------------------------------------------------------------- #
async def test_update_resets_status_until_retested(vault):
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        cid = (await _create(headers))["id"]
        async with api_client() as c:
            # a name-only edit leaves the prior health verdict intact
            before = await _status(org, cid)
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers, json={"name": "n2"})
            assert await _status(org, cid) == before
            # an endpoint change invalidates it → 'unknown' (no longer 'connected')
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                          json={"endpoint": "http://new.local"})
            assert await _status(org, cid) == "unknown"
            # a credential rotation also invalidates it
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers, json={"name": "n3"})
            assert await _status(org, cid) == "unknown"  # still unknown (name-only, kept)
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                          json={"credentials": {"api_key": "rotated"}})
            assert await _status(org, cid) == "unknown"
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# F5: rotating ONE field of a multi-field credential keeps the un-supplied keys (merge)
# --------------------------------------------------------------------------- #
async def test_oauth_edit_merges_credentials(vault):
    from opsforge.connectors import _decrypt_credentials

    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            created = await c.post("/api/v1/connectors", headers=headers, json={
                "name": "slack-a2", "kind": "slack", "transport": "http",
                "endpoint": "http://slack.local", "tool_allowlist": [],
                "credentials": {"client_id": "cid-123", "client_secret": "sec-OLD"}})
            cid = created.json()["id"]
            # rotate ONLY client_secret; leave client_id blank → must keep client_id
            await c.patch(f"/api/v1/connectors/{cid}", headers=headers,
                          json={"credentials": {"client_secret": "sec-NEW"}})
        creds = _decrypt_credentials(await _cred_blob(org, cid))
        # client_id kept (un-supplied), client_secret rotated — the merge, not a wholesale replace
        assert creds == {"client_id": "cid-123", "client_secret": "sec-NEW"}
    finally:
        await _cleanup(org)
