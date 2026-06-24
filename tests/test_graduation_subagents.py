"""M5: trust graduation (admin-granted, audited) and structured sub-agents."""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from conftest import api_client
from heuristic_gateway import HeuristicGateway
from run_evals import run_scenario
from sqlalchemy import text

from opsforge.agent import run_agent
from opsforge.config import DEFAULT_ORG_ID
from opsforge.db import session_factory
from opsforge.gateway import ChatResult, ToolCall
from opsforge.skills import get_skill, install_builtin_skills

pytestmark = pytest.mark.usefixtures("db_required")

SCENARIO = yaml.safe_load(
    open("skills/incident-investigation/evals/pool_exhaustion.yaml", encoding="utf-8")
)


async def _role_headers(role: str) -> dict[str, str]:
    from opsforge.security import generate_token

    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        uid = (
            await s.execute(
                text(
                    "INSERT INTO users (org_id,email,name,role) "
                    "VALUES (:o,:e,'t',:r) RETURNING id"
                ),
                {"o": DEFAULT_ORG_ID, "e": f"{uuid.uuid4().hex}@t.local", "r": role},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id,user_id,token_hash,name) "
                "VALUES (:o,:u,:h,'t')"
            ),
            {"o": DEFAULT_ORG_ID, "u": uid, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


async def _seed_succeeded_actions(tool: str, n: int) -> None:
    async with session_factory().begin() as s:
        for _ in range(n):
            await s.execute(
                text(
                    "INSERT INTO actions (org_id,action_class,tool,state,policy_trace) "
                    "VALUES (:o,'reversible',:t,'succeeded',CAST(:tr AS jsonb))"
                ),
                {"o": DEFAULT_ORG_ID, "t": tool, "tr": json.dumps({"allowed": True})},
            )


async def test_graduation_requires_admin_and_enough_clean_runs():
    await install_builtin_skills()
    tool = "kubernetes.restart_pod"
    # Isolate from prior suite runs (the dev DB persists).
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM actions WHERE tool=:t"), {"t": tool})
        await s.execute(
            text(
                "UPDATE skills SET trust_overrides = trust_overrides - CAST(:t AS text) "
                "WHERE slug='incident-investigation'"
            ),
            {"t": tool},
        )
    operator = await _role_headers("operator")
    admin = await _role_headers("admin")

    async with api_client() as client:
        # Non-admin is forbidden.
        r = await client.post(
            "/api/v1/skills/incident-investigation/graduate",
            headers=operator, json={"tool": tool},
        )
        assert r.status_code == 403

        # Admin, but not enough clean executions yet.
        r = await client.post(
            "/api/v1/skills/incident-investigation/graduate",
            headers=admin, json={"tool": tool},
        )
        assert r.status_code == 409

    await _seed_succeeded_actions(tool, 3)
    async with api_client() as client:
        r = await client.post(
            "/api/v1/skills/incident-investigation/graduate",
            headers=admin, json={"tool": tool},
        )
    assert r.status_code == 200, r.text
    assert r.json()["trust"] == "auto_with_notify"

    # The grant is persisted and recorded in the audit log.
    skill = await get_skill("incident-investigation")
    assert skill["trust_overrides"].get(tool) == "auto_with_notify"
    async with session_factory().begin() as s:
        graduated = (
            await s.execute(
                text("SELECT count(*) FROM audit_log WHERE event='skill.graduated'")
            )
        ).scalar_one()
    assert graduated >= 1


def test_graduated_tool_auto_approves_in_policy():
    from opsforge.policy import resolve_proposal

    manifest = {"proposals": [{"tool": "k.restart", "class": "reversible"}]}
    held = resolve_proposal(manifest, "k.restart", None)
    assert held["state"] == "awaiting_approval"
    graduated = resolve_proposal(manifest, "k.restart", {"k.restart": "auto_with_notify"})
    assert graduated["state"] == "approved"
    assert graduated["auto_execute"] is True


class _SubagentGateway:
    """Drives parent -> dispatch_subagent -> child -> reports. One instance serves
    both levels; the child lacks the dispatch tool so it goes straight to report."""

    def __init__(self):
        self._delegated = False

    async def chat(self, messages, tools, model):
        names = {t["function"]["name"] for t in (tools or [])}
        if "dispatch_subagent" in names and not self._delegated:
            self._delegated = True
            return ChatResult(
                text="Delegating dependency check.",
                tool_calls=[
                    ToolCall("d", "dispatch_subagent",
                             {"skill_slug": "dependency-audit",
                              "inputs": {"query": "check deps of payment-svc"}})
                ],
            )
        return ChatResult(
            text="Reporting.",
            tool_calls=[
                ToolCall("s", "submit_report",
                         {"hypothesis": "payment-svc deploy at fault",
                          "confidence": "medium", "evidence": []})
            ],
        )

    async def embedding(self, texts, model):
        return [[0.0] * 1536 for _ in texts]


async def test_subagent_delegation_creates_child_run():
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM connectors"))
    await install_builtin_skills()
    # Populate graph + connectors for both skills.
    await run_scenario("incident-investigation", SCENARIO, HeuristicGateway(), "demo")

    skill = await get_skill("incident-investigation")
    async with session_factory().begin() as s:
        parent_run = (
            await s.execute(
                text(
                    "INSERT INTO runs (org_id,skill_id,status,trigger) "
                    "VALUES (:o,:s,'queued',CAST(:t AS jsonb)) RETURNING id"
                ),
                {
                    "o": DEFAULT_ORG_ID,
                    "s": skill["id"],
                    "t": json.dumps(
                        {"kind": "manual", "payload": {"query": "why is payment-svc failing"}}
                    ),
                },
            )
        ).scalar_one()

    report = await run_agent(parent_run, skill, _SubagentGateway())
    assert report.hypothesis

    # A child run exists with parent_run_id set, and it completed.
    async with session_factory().begin() as s:
        child = (
            await s.execute(
                text(
                    "SELECT status, skill_id FROM runs WHERE parent_run_id=:p"
                ),
                {"p": parent_run},
            )
        ).first()
    assert child is not None
    assert child.status == "done"
    dep_skill = await get_skill("dependency-audit")
    assert str(child.skill_id) == str(dep_skill["id"])
