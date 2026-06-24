"""M2 acceptance: the agent loop investigates payment-svc against fixtures via
the eval harness, cites the seeded deploy, and makes zero mutating calls.

Uses the offline HeuristicGateway (no LLM key) driving the *real* loop + tools.
Requires Compose db+migrate.
"""

from __future__ import annotations

import uuid

import pytest
import yaml
from heuristic_gateway import HeuristicGateway
from run_evals import run_scenario
from sqlalchemy import text

from opsforge.db import session_factory
from opsforge.skills import install_builtin_skills

pytestmark = pytest.mark.usefixtures("db_required")

SCENARIO = yaml.safe_load(
    open("skills/incident-investigation/evals/pool_exhaustion.yaml", encoding="utf-8")
)


async def test_pool_exhaustion_scenario_passes():
    await install_builtin_skills()
    result = await run_scenario(
        "incident-investigation", SCENARIO, HeuristicGateway(), "heuristic-demo"
    )
    # Every assertion in the scenario must pass.
    assert result["passed"], result["checks"]
    assert result["checks"]["cites_change:payment-svc@rev7"] is True
    assert result["checks"]["no_mutating_execution"] is True
    assert result["tool_calls"] >= 3  # it actually investigated


async def test_run_produces_report_and_streamable_events():
    await install_builtin_skills()
    result = await run_scenario(
        "incident-investigation", SCENARIO, HeuristicGateway(), "heuristic-demo"
    )
    run_id = uuid.UUID(result["run_id"])

    async with session_factory().begin() as s:
        run = (
            await s.execute(
                text("SELECT status, report_md, report_json FROM runs WHERE id=:r"),
                {"r": run_id},
            )
        ).first()
        events = (
            await s.execute(
                text("SELECT kind FROM run_events WHERE run_id=:r ORDER BY seq"),
                {"r": run_id},
            )
        ).all()
    assert run.status == "done"
    assert "payment-svc" in run.report_md
    kinds = [e.kind for e in events]
    # The stream shows thoughts, real tool calls/results, a proposal, and a report.
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "proposal" in kinds
    assert kinds[-1] == "report"


async def test_proposal_recorded_but_not_executed():
    await install_builtin_skills()
    result = await run_scenario(
        "incident-investigation", SCENARIO, HeuristicGateway(), "heuristic-demo"
    )
    run_id = uuid.UUID(result["run_id"])
    async with session_factory().begin() as s:
        actions = (
            await s.execute(
                text("SELECT action_class, state, tool FROM actions WHERE run_id=:r"),
                {"r": run_id},
            )
        ).all()
    assert len(actions) >= 1
    for a in actions:
        assert a.state == "awaiting_approval"  # phase-1 hold; never executes
        assert a.tool == "kubernetes.rollback_deploy"
