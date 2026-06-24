"""A1 — the connector catalog: registry truthfulness + per-workspace, RLS-scoped status.

The governing rule under test: the catalog cannot lie about capability. A stub is never
`connected`; a genuinely-wired path shows its real status; one workspace never sees
another's connection status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from conftest import api_client
from sqlalchemy import text

from opsforge.catalog import CATALOG, ZONE_ORDER, catalog_by_zone, catalog_detail
from opsforge.knowledge import ProvenanceEnvelope, store_chunk
from opsforge.security import generate_token

pytestmark = pytest.mark.usefixtures("db_required")

NOW = datetime(2026, 6, 22, tzinfo=UTC)


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
            text("INSERT INTO api_tokens (org_id,user_id,token_hash,name) "
                 "VALUES (:o,:u,:h,'t')"),
            {"o": org, "u": uid, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


async def _add_connector(org: str, kind: str, status: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(
            text("INSERT INTO connectors (org_id,name,kind,transport,endpoint,status) "
                 "VALUES (:o,:n,:k,'stdio','stub://x',:st)"),
            {"o": org, "n": f"{kind}-1", "k": kind, "st": status},
        )


async def _add_document(org: str) -> None:
    await store_chunk(
        org_id=org, content="a documented procedure",
        envelope=ProvenanceEnvelope(source_kind="document", source_ref="doc://d",
                                    observed_at=NOW, ingested_at=NOW),
        embedding=[0.0] * 1536, process_key="p")


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("connectors", "knowledge_chunks", "api_tokens", "users"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


def _flat(zones):
    return {c["key"]: c for z in zones for c in z["connectors"]}


# --------------------------------------------------------------------------- #
# registry truthfulness (the catalog cannot lie about capability)
# --------------------------------------------------------------------------- #
def test_registry_keys_unique_and_zones_known():
    keys = [e.key for e in CATALOG]
    assert len(keys) == len(set(keys))
    assert all(e.zone in ZONE_ORDER for e in CATALOG)


def test_only_genuinely_wired_paths_are_implemented():
    impl = {e.key for e in CATALOG if e.implementation_status == "implemented"}
    # local files (chunk path) + ServiceNow + the SRE kinds + Confluence (Phase B) — no more.
    assert impl == {"local_files", "servicenow", "aws", "kubernetes", "datadog",
                    "jira", "pagerduty", "slack", "confluence"}
    # every implemented instance-backed entry maps to a real connectors.kind; stubs do not.
    for e in CATALOG:
        if e.implementation_status == "stub_coming_soon":
            assert e.instance_kind is None


async def test_a_stub_is_never_connectable_or_connected():
    org = str(uuid.uuid4())
    try:
        # even with (impossibly) a same-named instance, a stub stays coming_soon
        flat = _flat(await catalog_by_zone(org))
        for key in ("splunk", "ansible", "powerscale", "idrac", "sharepoint"):
            assert flat[key]["status"] == "coming_soon"
            assert flat[key]["connectable"] is False
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# per-workspace status (computed from real signals)
# --------------------------------------------------------------------------- #
async def test_local_files_status_derives_from_ingested_documents():
    org = str(uuid.uuid4())
    try:
        assert _flat(await catalog_by_zone(org))["local_files"]["status"] == "available"
        await _add_document(org)
        assert _flat(await catalog_by_zone(org))["local_files"]["status"] == "connected"
    finally:
        await _cleanup(org)


async def test_servicenow_status_tracks_its_configured_instance():
    org = str(uuid.uuid4())
    try:
        assert _flat(await catalog_by_zone(org))["servicenow"]["status"] == "available"
        await _add_connector(org, "servicenow", "unknown")
        assert _flat(await catalog_by_zone(org))["servicenow"]["status"] == "configured"
        await _cleanup(org)
        await _add_connector(org, "servicenow", "healthy")
        sn = _flat(await catalog_by_zone(org))["servicenow"]
        assert sn["status"] == "connected" and sn["connectable"] is False
        await _cleanup(org)
        await _add_connector(org, "servicenow", "unhealthy")
        sn = _flat(await catalog_by_zone(org))["servicenow"]
        assert sn["status"] == "error" and sn["connectable"] is True  # re-connectable
    finally:
        await _cleanup(org)


async def test_status_is_workspace_scoped():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await _add_connector(org_b, "servicenow", "healthy")
        await _add_document(org_b)
        # A sees its OWN (empty) status, never B's connected ServiceNow / ingested docs
        flat_a = _flat(await catalog_by_zone(org_a))
        assert flat_a["servicenow"]["status"] == "available"
        assert flat_a["local_files"]["status"] == "available"
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)


# --------------------------------------------------------------------------- #
# detail
# --------------------------------------------------------------------------- #
async def test_detail_returns_config_requirements_and_404():
    org = str(uuid.uuid4())
    try:
        sn = await catalog_detail(org, "servicenow")
        assert sn is not None and sn["config_requirements"] == ["endpoint", "credential_ref"]
        assert sn["instance_kind"] == "servicenow"
        assert await catalog_detail(org, "nope") is None
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the API surface
# --------------------------------------------------------------------------- #
async def test_catalog_endpoint_grouped_and_populated():
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            body = (await c.get("/api/v1/catalog", headers=headers)).json()
        zones = body["zones"]
        # always populated, grouped, in zone order — never an empty state on load
        assert [z["zone"] for z in zones] == ZONE_ORDER
        assert all(z["connectors"] for z in zones)
        flat = _flat(zones)
        assert flat["servicenow"]["status"] == "available"
        assert flat["confluence"]["status"] == "available"  # Phase B: implemented + connectable
        assert flat["splunk"]["status"] == "coming_soon"
    finally:
        await _cleanup(org)


async def test_catalog_detail_endpoint_and_404():
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            ok = await c.get("/api/v1/catalog/servicenow", headers=headers)
            assert ok.status_code == 200 and ok.json()["config_requirements"]
            miss = await c.get("/api/v1/catalog/nope", headers=headers)
            assert miss.status_code == 404
    finally:
        await _cleanup(org)


async def test_catalog_endpoint_is_workspace_scoped():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    headers_a = await _token(org_a)
    try:
        await _add_connector(org_b, "servicenow", "healthy")
        async with api_client() as c:
            flat = _flat((await c.get("/api/v1/catalog", headers=headers_a)).json()["zones"])
        assert flat["servicenow"]["status"] == "available"  # A never sees B's connection
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)
