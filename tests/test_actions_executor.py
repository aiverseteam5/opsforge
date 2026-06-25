"""M5: the trust-ladder executor — approve, execute, health-gate, auto-rollback,
denied paths, role gating, and the policy_trace refusal. Requires db+migrate."""

from __future__ import annotations

import json
import uuid

import pytest
from fake_mcp import server_command
from sqlalchemy import text

from opsforge.actions import (
    ActionError,
    approve_action,
    deny_action,
    execute_action,
    undo_action,
)
from opsforge.config import DEFAULT_ORG_ID
from opsforge.db import session_factory

pytestmark = pytest.mark.usefixtures("db_required")

STAGING_TOOLS = ["apply_fix", "check_health", "revert", "rollback_deploy", "restart_pod"]


async def _staging_connector() -> str:
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM connectors WHERE kind='kubernetes'"))
        cid = (
            await s.execute(
                text(
                    "INSERT INTO connectors (org_id,name,kind,transport,endpoint,"
                    "tool_allowlist,status) VALUES (:o,'staging','kubernetes','stdio',"
                    ":e,CAST(:a AS jsonb),'healthy') RETURNING id"
                ),
                {
                    "o": DEFAULT_ORG_ID,
                    "e": server_command("staging"),
                    "a": json.dumps(STAGING_TOOLS),
                },
            )
        ).scalar_one()
    return str(cid)


async def _insert_action(outcome: str, *, with_trace: bool = True) -> uuid.UUID:
    target = "k8s://prod/deploy/payment-svc"
    trace = {"allowed": True, "state": "awaiting_approval", "action_class": "reversible"}
    async with session_factory().begin() as s:
        aid = (
            await s.execute(
                text(
                    "INSERT INTO actions (org_id, action_class, tool, params, target_ref, "
                    "rollback, state, policy_trace) VALUES (:o,'reversible',"
                    "'kubernetes.apply_fix',CAST(:p AS jsonb),:t,CAST(:rb AS jsonb),"
                    "'awaiting_approval',CAST(:tr AS jsonb)) RETURNING id"
                ),
                {
                    "o": DEFAULT_ORG_ID,
                    "p": json.dumps({"target": target, "outcome": outcome}),
                    "t": target,
                    "rb": json.dumps({"tool": "kubernetes.revert", "params": {"target": target}}),
                    "tr": json.dumps(trace) if with_trace else "null",
                },
            )
        ).scalar_one()
    return aid


async def _state(aid: uuid.UUID) -> str:
    async with session_factory().begin() as s:
        return (
            await s.execute(text("SELECT state FROM actions WHERE id=:i"), {"i": aid})
        ).scalar_one()


async def _audit_events(aid: uuid.UUID) -> list[str]:
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text("SELECT event FROM audit_log WHERE subject_ref=:s ORDER BY seq"),
                {"s": str(aid)},
            )
        ).all()
    return [r.event for r in rows]


async def test_approved_action_executes_and_succeeds():
    await _staging_connector()
    aid = await _insert_action("ok")
    await approve_action(aid, actor_role="operator", actor=f"user:{uuid.uuid4()}")
    assert await _state(aid) == "approved"
    await execute_action(aid)
    assert await _state(aid) == "succeeded"
    events = await _audit_events(aid)
    assert events == ["action.approved", "action.executing", "action.succeeded"]


# --------------------------------------------------------------------------- #
# G4 undo: reverse a succeeded reversible action by running its declared rollback
# --------------------------------------------------------------------------- #
async def test_undo_reverses_a_succeeded_reversible_action():
    await _staging_connector()
    aid = await _insert_action("ok")
    await approve_action(aid, actor_role="operator", actor=f"user:{uuid.uuid4()}")
    await execute_action(aid)
    assert await _state(aid) == "succeeded"
    # the operator undoes it — the declared rollback (kubernetes.revert) runs
    res = await undo_action(aid, actor=f"user:{uuid.uuid4()}")
    assert res["state"] == "rolled_back"
    assert await _state(aid) == "rolled_back"
    assert "action.undone" in await _audit_events(aid)


async def test_undo_rejects_a_non_succeeded_action():
    await _staging_connector()
    aid = await _insert_action("ok")  # still awaiting_approval
    with pytest.raises(ActionError, match="succeeded"):
        await undo_action(aid, actor="user:x")
    assert await _state(aid) == "awaiting_approval"


async def test_undo_rejects_an_action_with_no_rollback():
    await _staging_connector()
    # a succeeded action whose rollback is null cannot be undone
    async with session_factory().begin() as s:
        aid = (await s.execute(
            text("INSERT INTO actions (org_id, action_class, tool, params, target_ref, "
                 "rollback, state, policy_trace) VALUES (:o,'reversible',"
                 "'kubernetes.apply_fix',CAST('{}' AS jsonb),'k8s://x',NULL,'succeeded',"
                 "CAST(:tr AS jsonb)) RETURNING id"),
            {"o": DEFAULT_ORG_ID, "tr": json.dumps({"allowed": True})})).scalar_one()
    with pytest.raises(ActionError, match="rollback"):
        await undo_action(aid, actor="user:x")
    assert await _state(aid) == "succeeded"  # untouched


async def test_undo_rejects_cross_workspace():
    """An operator in another workspace cannot undo this org's action (the by-id load is
    workspace-scoped) — the rollback is NEVER run cross-tenant."""
    await _staging_connector()
    aid = await _insert_action("ok")
    await approve_action(aid, actor_role="operator", actor=f"user:{uuid.uuid4()}")
    await execute_action(aid)
    assert await _state(aid) == "succeeded"
    with pytest.raises(ActionError, match="not found"):
        await undo_action(aid, actor=f"user:{uuid.uuid4()}", actor_role="admin",
                          org_id=str(uuid.uuid4()))
    assert await _state(aid) == "succeeded"  # the foreign-org caller did NOT reverse it


async def test_undo_respects_priority_escalation():
    """Undo honours the same priority escalation as approve: an operator cannot reverse an
    admin-only P1 remediation; an admin can."""
    await _staging_connector()
    manifest = {"policy": {"requires_role_for_priority": {"P1": "admin"}}}
    async with session_factory().begin() as s:
        skill_id = (await s.execute(
            text("INSERT INTO skills (org_id,slug,version,manifest,source,enabled) "
                 "VALUES (:o,:slug,'0.1.0',CAST(:m AS jsonb),'org',true) RETURNING id"),
            {"o": DEFAULT_ORG_ID, "slug": f"p1-{uuid.uuid4().hex[:8]}",
             "m": json.dumps(manifest)})).scalar_one()
        run_id = (await s.execute(
            text("INSERT INTO runs (org_id,skill_id,status,trigger) "
                 "VALUES (:o,:sk,'done',CAST(:t AS jsonb)) RETURNING id"),
            {"o": DEFAULT_ORG_ID, "sk": str(skill_id),
             "t": json.dumps({"payload": {"priority": "P1"}})})).scalar_one()
        aid = (await s.execute(
            text("INSERT INTO actions (org_id,skill_id,run_id,action_class,tool,target_ref,"
                 "rollback,state,policy_trace) VALUES (:o,:sk,:r,'reversible',"
                 "'kubernetes.apply_fix','k8s://x',CAST(:rb AS jsonb),'succeeded',"
                 "CAST(:tr AS jsonb)) RETURNING id"),
            {"o": DEFAULT_ORG_ID, "sk": str(skill_id), "r": str(run_id),
             "rb": json.dumps({"tool": "kubernetes.revert", "params": {"target": "k8s://x"}}),
             "tr": json.dumps({"allowed": True})})).scalar_one()
    with pytest.raises(ActionError, match="requires role admin"):
        await undo_action(aid, actor=f"user:{uuid.uuid4()}", actor_role="operator",
                          org_id=DEFAULT_ORG_ID)
    assert await _state(aid) == "succeeded"
    res = await undo_action(aid, actor=f"user:{uuid.uuid4()}", actor_role="admin",
                            org_id=DEFAULT_ORG_ID)
    assert res["state"] == "rolled_back"


async def test_execute_action_is_workspace_scoped():
    """The executor's by-id load is workspace-scoped: a worker job pinned to the WRONG org can
    never execute another workspace's action (the explicit org predicate enforces under the dev
    superuser; FORCE RLS enforces it for the restricted opsforge_app role — proven live)."""
    await _staging_connector()
    async with session_factory().begin() as s:
        aid = (await s.execute(
            text("INSERT INTO actions (org_id, action_class, tool, params, target_ref, "
                 "rollback, state, policy_trace) VALUES (:o,'reversible','kubernetes.apply_fix',"
                 "CAST(:p AS jsonb),:t,CAST(:rb AS jsonb),'approved',CAST(:tr AS jsonb)) "
                 "RETURNING id"),
            {"o": DEFAULT_ORG_ID, "p": json.dumps({"target": "k8s://x", "outcome": "ok"}),
             "t": "k8s://x", "rb": json.dumps({"tool": "kubernetes.revert", "params": {}}),
             "tr": json.dumps({"allowed": True})})).scalar_one()
    # a foreign-org executor cannot see it
    with pytest.raises(ActionError, match="not found"):
        await execute_action(aid, str(uuid.uuid4()))
    assert await _state(aid) == "approved"  # untouched
    # the action's own org executes it
    res = await execute_action(aid, DEFAULT_ORG_ID)
    assert res["state"] == "succeeded"


async def test_execution_failure_triggers_auto_rollback():
    await _staging_connector()
    aid = await _insert_action("exec_error")
    await approve_action(aid, actor_role="admin", actor=f"user:{uuid.uuid4()}")
    await execute_action(aid)
    assert await _state(aid) == "rolled_back"
    events = await _audit_events(aid)
    assert "action.failed" in events
    assert "action.rolled_back" in events


async def test_failed_health_check_triggers_auto_rollback():
    await _staging_connector()
    aid = await _insert_action("unhealthy")  # applies, but check_health reports unhealthy
    await approve_action(aid, actor_role="operator", actor=f"user:{uuid.uuid4()}")
    await execute_action(aid)
    assert await _state(aid) == "rolled_back"
    assert "action.rolled_back" in await _audit_events(aid)


async def test_deny_is_terminal():
    aid = await _insert_action("ok")
    await deny_action(aid, actor=f"user:{uuid.uuid4()}")
    assert await _state(aid) == "denied"
    # A denied action cannot then be approved.
    with pytest.raises(ActionError):
        await approve_action(aid, actor_role="admin", actor=f"user:{uuid.uuid4()}")


async def test_role_gating_rejects_non_approvers():
    aid = await _insert_action("ok")
    with pytest.raises(ActionError):
        await approve_action(aid, actor_role="viewer", actor=f"user:{uuid.uuid4()}")
    with pytest.raises(ActionError):
        await approve_action(aid, actor_role=None, actor="system")


async def test_executor_refuses_action_without_policy_trace():
    aid = await _insert_action("ok", with_trace=False)
    with pytest.raises(ActionError):
        await approve_action(aid, actor_role="admin", actor=f"user:{uuid.uuid4()}")


async def test_destructive_action_never_auto_executes_via_policy():
    # Sanity: effective_trust never grants auto_allow to destructive tools.
    from opsforge.policy import effective_trust

    decision = effective_trust("destructive", "k.del", {"k.del": "auto_with_notify"})
    assert decision == "awaiting_approval"
