"""Action lifecycle + executor — the trust ladder made live (Phase 2).

The deterministic state machine:

  awaiting_approval ─┬─► denied                         (terminal)
                     ├─► dry_run_done ─► approved
                     └─► approved ─► executing ─┬─► succeeded            (terminal)
                                                └─► failed ─► rolled_back

Every transition inserts an audit_log row. The executor REFUSES any action whose
policy_trace is absent or whose transition is not allowed (defense in depth,
doctrine #13.2). Execution and rollback go through the connector layer; a failed
post-execution health check triggers auto-rollback.

This is plain Python — the LLM never runs here (doctrine #3).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from .connectors import ConnectorError, load_connectors_by_kind, open_connector
from .db import append_run_event, enqueue, record_audit, scope_to_org, session_factory
from .security import redact

# Allowed state transitions. Anything not listed is rejected.
_TRANSITIONS: dict[str, set[str]] = {
    "awaiting_approval": {"approved", "denied", "dry_run_done"},
    "dry_run_done": {"approved", "denied"},
    "approved": {"executing"},
    "executing": {"succeeded", "failed"},
    "failed": {"rolled_back"},
}

_APPROVER_ROLES = {"admin", "operator"}


class ActionError(RuntimeError):
    pass


async def _load_action(action_id: UUID, org_id: UUID) -> dict[str, Any] | None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, run_id, skill_id, action_class, tool, params, "
                    "target_ref, rollback, state, policy_trace, approved_by "
                    "FROM actions WHERE id = :id AND org_id = :org"
                ),
                {"id": action_id, "org": org_id},
            )
        ).first()
    return dict(row._mapping) if row else None


async def _transition(
    action_id: UUID,
    expected_from: str,
    to_state: str,
    org_id: UUID,
    *,
    extra_sql: str = "",
    extra_params: dict[str, Any] | None = None,
) -> bool:
    """Atomically move an action to `to_state` iff it is currently in
    `expected_from` and the transition is allowed. Returns True on success."""
    if to_state not in _TRANSITIONS.get(expected_from, set()):
        raise ActionError(f"illegal transition {expected_from} -> {to_state}")
    params = {"id": action_id, "to": to_state, "from": expected_from}
    params.update(extra_params or {})
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        res = await s.execute(
            text(
                f"UPDATE actions SET state = :to {extra_sql} "
                "WHERE id = :id AND state = :from"
            ),
            params,
        )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _audit(
    action: dict[str, Any], actor: str, event: str, detail: dict | None = None
) -> None:
    await record_audit(
        action["org_id"],
        actor,
        event,
        subject_ref=str(action["id"]),
        detail=detail or {},
    )
    # Mirror into the run's event stream so the SSE timeline shows approvals too.
    if action.get("run_id"):
        await append_run_event(
            action["run_id"],
            action["org_id"],
            "proposal",
            {"event": event, "action": str(action["id"])},
        )


def _require_policy_trace(action: dict[str, Any]) -> None:
    trace = action.get("policy_trace")
    if not trace or not isinstance(trace, dict):
        raise ActionError("action has no policy_trace; refusing (defense in depth)")
    if not trace.get("allowed", False):
        raise ActionError("policy_trace denies this action; refusing")


# --------------------------------------------------------------------------- #
# Human-driven transitions (called from the API)
# --------------------------------------------------------------------------- #
async def approve_action(
    action_id: UUID, *, org_id: UUID, actor_role: str | None, actor: str
) -> dict[str, Any]:
    if actor_role not in _APPROVER_ROLES:
        raise ActionError("approval requires role admin or operator")
    action = await _load_action(action_id, org_id)
    if action is None:
        raise ActionError("action not found")
    _require_policy_trace(action)
    if action["state"] not in ("awaiting_approval", "dry_run_done"):
        raise ActionError(f"cannot approve from state {action['state']}")

    # GAP 3: priority-aware escalation (e.g. a P1 remediation requires admin).
    required = await _required_role(action)
    from .policy import role_allows

    if not role_allows(actor_role, required):
        raise ActionError(f"this priority requires role {required} to approve")

    ok = await _transition(
        action_id,
        action["state"],
        "approved",
        action["org_id"],
        extra_sql=", approved_by = :by, approved_at = now()",
        extra_params={"by": _actor_uuid(actor)},
    )
    if not ok:
        raise ActionError("approve race lost; state changed")
    await _audit(action, actor, "action.approved")
    # Hand execution to the worker (deterministic, no human in the hot path).
    async with session_factory().begin() as s:
        await scope_to_org(s, action["org_id"])
        await enqueue(
            s, kind="execute_action", payload={"action_id": str(action_id)},
            org_id=str(action["org_id"]),
        )
    return {"state": "approved", "id": str(action_id)}


async def deny_action(action_id: UUID, *, org_id: UUID, actor: str) -> dict[str, Any]:
    action = await _load_action(action_id, org_id)
    if action is None:
        raise ActionError("action not found")
    if not await _transition(action_id, action["state"], "denied", action["org_id"]):
        raise ActionError(f"cannot deny from state {action['state']}")
    await _audit(action, actor, "action.denied")
    return {"state": "denied", "id": str(action_id)}


async def dry_run_action(action_id: UUID, *, org_id: UUID, actor: str) -> dict[str, Any]:
    """Render the exact tool + params + target WITHOUT calling any mutating tool."""
    action = await _load_action(action_id, org_id)
    if action is None:
        raise ActionError("action not found")
    _require_policy_trace(action)
    plan = {
        "tool": action["tool"],
        "params": redact(action["params"] or {}),
        "target_ref": action["target_ref"],
        "rollback": action["rollback"],
        "note": "dry-run only; no mutating tool was called",
    }
    await _transition(action_id, action["state"], "dry_run_done", action["org_id"])
    await _audit(action, actor, "action.dry_run", detail=plan)
    return {"state": "dry_run_done", "plan": plan}


# --------------------------------------------------------------------------- #
# Execution (called from the worker via execute_action job)
# --------------------------------------------------------------------------- #
async def execute_action(action_id: UUID, org_id: UUID | None = None) -> dict[str, Any]:
    if org_id is None:
        raise ActionError("execute_action requires org_id")
    action = await _load_action(action_id, org_id)
    if action is None:
        raise ActionError("action not found")
    _require_policy_trace(action)

    # GAP 3: a change-freeze window defers execution (the action stays approved).
    if await _in_change_freeze(action):
        await _audit(action, "system:executor", "action.frozen",
                     detail={"reason": "change freeze in effect"})
        return {"state": action["state"], "frozen": True}

    if not await _transition(action_id, "approved", "executing", action["org_id"]):
        raise ActionError(f"cannot execute from state {action['state']}")
    await _audit(action, "system:executor", "action.executing")

    kind = action["tool"].split(".", 1)[0]
    by_kind = await load_connectors_by_kind(action["org_id"])
    connector = by_kind.get(kind)
    if connector is None:
        await _fail(action, reason=f"no connector for kind {kind}")
        return {"state": "failed", "reason": "no connector"}

    run_id = action["run_id"]
    try:
        async with open_connector(connector) as cs:
            result = await cs.call(action["tool"], action["params"] or {}, run_id=run_id)
            # Post-execution health check, if the connector exposes one.
            healthy = await _health_ok(cs, kind, action["target_ref"], run_id)
            if not healthy:
                raise ActionError("post-execution health check failed")
    except Exception as exc:  # noqa: BLE001 - any failure → failed, then rollback
        await _fail(action, reason=str(redact(str(exc))))
        await _maybe_rollback(action, connector, kind)
        return {"state": "rolled_back" if action.get("rollback") else "failed"}

    await _transition(
        action_id, "executing", "succeeded", action["org_id"],
        extra_sql=", executed_at = now(), result = CAST(:res AS jsonb)",
        extra_params={"res": _json(redact(result))},
    )
    await _audit(action, "system:executor", "action.succeeded")
    return {"state": "succeeded", "result": result}


async def _required_role(action: dict[str, Any]) -> str | None:
    """The role this action's incident priority demands (None if unconstrained)."""
    if not action.get("skill_id"):
        return None
    from .policy import min_approval_role
    from .skills import get_skill_by_id

    skill = await get_skill_by_id(action["skill_id"])
    policy = ((skill or {}).get("manifest") or {}).get("policy", {})
    if not policy.get("requires_role_for_priority"):
        return None
    priority = None
    if action.get("run_id"):
        async with session_factory().begin() as s:
            await scope_to_org(s, action["org_id"])
            trig = (
                await s.execute(
                    text("SELECT trigger FROM runs WHERE id = :id"),
                    {"id": action["run_id"]},
                )
            ).scalar_one_or_none()
        priority = ((trig or {}).get("payload") or {}).get("priority")
    return min_approval_role(policy, priority)


async def _in_change_freeze(action: dict[str, Any]) -> bool:
    if not action.get("skill_id"):
        return False
    from datetime import UTC, datetime

    from .policy import freeze_active
    from .skills import get_skill_by_id

    skill = await get_skill_by_id(action["skill_id"])
    policy = ((skill or {}).get("manifest") or {}).get("policy", {})
    return freeze_active(policy.get("freeze_windows"), datetime.now(UTC))


async def _health_ok(cs, kind: str, target_ref: str | None, run_id) -> bool:
    """Call `{kind}.check_health` if allowlisted; True if no health tool exists."""
    if "check_health" not in cs.allowlist:
        return True
    health = await cs.call(f"{kind}.check_health", {"target": target_ref}, run_id=run_id)
    if isinstance(health, dict):
        return bool(health.get("healthy", True))
    return True


async def _fail(action: dict[str, Any], *, reason: str) -> None:
    await _transition(
        action["id"], "executing", "failed", action["org_id"],
        extra_sql=", result = CAST(:res AS jsonb)",
        extra_params={"res": _json({"error": reason})},
    )
    await _audit(action, "system:executor", "action.failed", detail={"reason": reason})


async def _maybe_rollback(action: dict[str, Any], connector: dict, kind: str) -> None:
    rollback = action.get("rollback")
    if not rollback or not rollback.get("tool"):
        return
    try:
        async with open_connector(connector) as cs:
            await cs.call(
                rollback["tool"], rollback.get("params") or {}, run_id=action["run_id"]
            )
        await _transition(action["id"], "failed", "rolled_back", action["org_id"])
        await _audit(
            action, "system:executor", "action.rolled_back",
            detail={"via": rollback["tool"]},
        )
    except ConnectorError as exc:
        await _audit(
            action, "system:executor", "action.rollback_failed",
            detail={"error": str(redact(str(exc)))},
        )


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _actor_uuid(actor: str) -> str | None:
    # actor is "user:<uuid>" | "system:..."; bind approved_by only for users.
    if actor.startswith("user:"):
        return actor.split(":", 1)[1]
    return None
