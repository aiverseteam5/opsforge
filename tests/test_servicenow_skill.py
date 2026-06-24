"""Step C: ServiceNow connector → CMDB graph fusion + canonical translation +
the servicenow-incident skill produces an RCA. Requires db+migrate."""

from __future__ import annotations

import json

import pytest
import yaml
from fake_mcp import server_command
from heuristic_gateway import HeuristicGateway
from run_evals import run_scenario
from sqlalchemy import text

from opsforge.config import DEFAULT_ORG_ID
from opsforge.connectors import load_connector
from opsforge.db import session_factory
from opsforge.ops_adapter import read_incident
from opsforge.ops_model import Incident, load_starter_mapping
from opsforge.skills import install_builtin_skills

pytestmark = pytest.mark.usefixtures("db_required")

SCENARIO = yaml.safe_load(
    open("skills/servicenow-incident/evals/itsm_incident.yaml", encoding="utf-8")
)


async def _snow_connector() -> dict:
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM connectors WHERE kind='servicenow'"))
        cid = (
            await s.execute(
                text(
                    "INSERT INTO connectors (org_id,name,kind,transport,endpoint,"
                    "tool_allowlist,field_mapping,status) VALUES "
                    "(:o,'snow','servicenow','stdio',:e,CAST(:a AS jsonb),"
                    "CAST(:m AS jsonb),'healthy') RETURNING id"
                ),
                {
                    "o": DEFAULT_ORG_ID,
                    "e": server_command("servicenow"),
                    "a": '["get_incident","search_incidents","get_related_cis","get_sla"]',
                    "m": json.dumps(load_starter_mapping("servicenow")),
                },
            )
        ).scalar_one()
    return await load_connector(cid, DEFAULT_ORG_ID)


async def test_read_incident_translates_to_canonical():
    connector = await _snow_connector()
    inc = await read_incident(connector, "INC0012345")
    assert isinstance(inc, Incident)
    assert inc.ref == "INC0012345"
    assert inc.priority == "P1"          # mapped from native "1"
    assert inc.state == "investigating"  # mapped from native "2"
    assert inc.service_ref == "service://payment-svc"  # = graph natural_key


async def test_itsm_scenario_passes_via_mapped_connector():
    await install_builtin_skills()  # installs servicenow-incident too
    result = await run_scenario(
        "servicenow-incident", SCENARIO, HeuristicGateway(), "demo"
    )
    assert result["passed"], result["checks"]
    assert result["checks"]["cites_change:payment-svc@rev7"] is True
    assert result["checks"]["no_mutating_execution"] is True


async def test_cmdb_mapper_fuses_onto_graph():
    # After the scenario sync, the CMDB CI shares the node infra connectors use,
    # and its dependencies are linked.
    await install_builtin_skills()
    await run_scenario("servicenow-incident", SCENARIO, HeuristicGateway(), "demo")
    async with session_factory().begin() as s:
        deps = (
            await s.execute(
                text(
                    "SELECT count(*) FROM graph_edges WHERE kind='depends_on'"
                )
            )
        ).scalar_one()
        node = (
            await s.execute(
                text(
                    "SELECT props FROM graph_nodes "
                    "WHERE natural_key='service://payment-svc'"
                )
            )
        ).scalar_one()
    assert deps >= 1  # CMDB depends_on edges created
    assert node is not None  # same node the k8s mapper populated
