"""Step D: NL intent resolver — nl → skill + entity, ambiguous → candidates."""

from __future__ import annotations

import json
import uuid

import pytest
from conftest import api_client
from fake_mcp import server_command
from sqlalchemy import text

from opsforge.config import DEFAULT_ORG_ID
from opsforge.db import session_factory
from opsforge.dispatch import resolve_nl
from opsforge.ops_model import load_starter_mapping
from opsforge.skills import install_builtin_skills

pytestmark = pytest.mark.usefixtures("db_required")


async def _seed_servicenow() -> None:
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM connectors WHERE kind='servicenow'"))
        await s.execute(
            text(
                "INSERT INTO connectors (org_id,name,kind,transport,endpoint,"
                "tool_allowlist,field_mapping,status) VALUES "
                "(:o,'snow','servicenow','stdio',:e,CAST(:a AS jsonb),"
                "CAST(:m AS jsonb),'healthy')"
            ),
            {
                "o": DEFAULT_ORG_ID,
                "e": server_command("servicenow"),
                "a": '["get_incident","search_incidents","get_related_cis","get_sla"]',
                "m": json.dumps(load_starter_mapping("servicenow")),
            },
        )


async def test_explicit_ref_resolves_to_servicenow_skill():
    await install_builtin_skills()
    res = await resolve_nl("investigate servicenow incident INC0012345")
    assert res["skill_slug"] == "servicenow-incident"
    assert res["inputs"]["incident_ref"] == "INC0012345"  # regex-extracted
    assert "run_id" in res


async def test_entity_looked_up_via_connector_when_ref_absent():
    await install_builtin_skills()
    await _seed_servicenow()
    res = await resolve_nl("check the servicenow incident for payment-svc")
    assert res["skill_slug"] == "servicenow-incident"
    assert res["inputs"]["incident_ref"] == "INC0012345"  # resolved via search


async def test_infra_query_resolves_to_incident_investigation():
    await install_builtin_skills()
    res = await resolve_nl("run an incident investigation on the cluster")
    assert res["skill_slug"] == "incident-investigation"


async def test_ambiguous_query_returns_candidates_not_a_guess():
    await install_builtin_skills()
    # "incident" matches both incident-investigation and servicenow-incident
    # equally → a genuine tie → offer candidates, never guess.
    res = await resolve_nl("handle the incident")
    assert res["status"] == "ambiguous"
    assert len(res["candidates"]) >= 2
    assert "run_id" not in res


async def test_no_keyword_signal_defaults_to_general_investigator():
    await install_builtin_skills()
    res = await resolve_nl("why is payment-svc throwing 5xx")
    assert res["skill_slug"] == "incident-investigation"  # sensible default


async def test_runs_api_accepts_nl(auth_headers):
    await install_builtin_skills()
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/runs",
            headers=auth_headers,
            json={"nl": "investigate servicenow incident INC0012345"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["skill_slug"] == "servicenow-incident"
    uuid.UUID(body["run_id"])  # valid run id
