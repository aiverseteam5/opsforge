"""M4: audit trail endpoint + SPA serving (index.html fallback for client routes)."""

from __future__ import annotations

import os

import pytest
from conftest import api_client

from opsforge.skills import install_builtin_skills


@pytest.mark.usefixtures("db_required")
async def test_dispatch_writes_audit_entry(auth_headers):
    await install_builtin_skills()
    async with api_client() as client:
        await client.post(
            "/api/v1/runs",
            headers=auth_headers,
            json={"skill_slug": "incident-investigation", "inputs": {"query": "audit me"}},
        )
        audit = (await client.get("/api/v1/audit", headers=auth_headers)).json()
    assert any(a["event"] == "run.dispatched" for a in audit)


@pytest.mark.usefixtures("db_required")
async def test_audit_requires_auth():
    async with api_client() as client:
        resp = await client.get("/api/v1/audit")
    assert resp.status_code == 401


@pytest.mark.skipif(
    not os.path.isdir("workbench/dist"), reason="SPA not built"
)
async def test_spa_index_served_for_client_routes():
    async with api_client() as client:
        # Deep-link to a client route must return the SPA shell, not 404.
        resp = await client.get("/runs/some-id")
        assert resp.status_code == 200
        assert "<div id=\"root\">" in resp.text
        # API 404s are still JSON, not the SPA.
        missing = await client.get("/api/v1/does-not-exist")
        assert missing.status_code == 404
