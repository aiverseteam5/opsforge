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
