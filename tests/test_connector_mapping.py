"""Step B: connector discovery + field-mapping validation (GAP-1 onboarding).

Uses the fake-ServiceNow MCP server. Requires Compose db+migrate.
"""

from __future__ import annotations

import pytest
from conftest import api_client
from fake_mcp import server_command

from opsforge.ops_model import load_starter_mapping

pytestmark = pytest.mark.usefixtures("db_required")

SNOW_TOOLS = [
    "describe_schema", "get_incident", "search_incidents", "get_related_cis",
    "get_sla", "add_work_note", "update_incident", "create_change",
]


async def _create_servicenow(headers) -> str:
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/connectors",
            headers=headers,
            json={
                "name": "snow",
                "kind": "servicenow",
                "transport": "stdio",
                "endpoint": server_command("servicenow"),
                "tool_allowlist": SNOW_TOOLS,
                # servicenow now requires a credential (A2 fail-closed); the fake ignores it
                "credentials": {"token": "fake"},
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_servicenow_connector_defaults_starter_mapping(auth_headers):
    body = await _create_servicenow(auth_headers)
    # Created with the bundled starter pack already applied (config, no code).
    assert body["field_mapping"] is not None
    assert body["field_mapping"]["incident.priority"]["field"] == "priority"


async def test_discover_caches_native_schema(auth_headers):
    body = await _create_servicenow(auth_headers)
    cid = body["id"]
    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/connectors/{cid}/discover", headers=auth_headers
        )
    assert resp.status_code == 200, resp.text
    schema = resp.json()["discovered_schema"]
    assert "incident" in schema["tables"]
    assert "priority" in schema["tables"]["incident"]["choices"]


async def test_put_valid_mapping_is_ops_ready(auth_headers):
    body = await _create_servicenow(auth_headers)
    cid = body["id"]
    async with api_client() as client:
        resp = await client.put(
            f"/api/v1/connectors/{cid}/mapping",
            headers=auth_headers,
            json={"field_mapping": load_starter_mapping("servicenow")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ops-ready"


async def test_put_incomplete_mapping_is_rejected(auth_headers):
    body = await _create_servicenow(auth_headers)
    cid = body["id"]
    async with api_client() as client:
        resp = await client.put(
            f"/api/v1/connectors/{cid}/mapping",
            headers=auth_headers,
            json={"field_mapping": {"incident.ref": {"field": "number"}}},
        )
    assert resp.status_code == 400
    missing = resp.json()["detail"]["missing"]
    assert "incident.priority" in missing and "incident.state" in missing
