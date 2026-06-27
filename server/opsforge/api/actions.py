"""Actions API: list proposals + approve / dry-run / deny (Phase 2)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from ..actions import (
    ActionError,
    approve_action,
    deny_action,
    dry_run_action,
)
from ..db import session_factory
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

_APPROVER_ROLES = {"admin", "operator"}


def _actor(principal: Principal) -> str:
    return f"user:{principal.user_id}" if principal.user_id else "system"


@router.get("")
async def list_actions(
    principal: Principal = Depends(require_token),
    state: str | None = Query(default=None),
):
    clauses = ["org_id = :org"]
    params: dict[str, Any] = {"org": principal.org_id}
    if state:
        clauses.append("state = :state")
        params["state"] = state
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, run_id, action_class, tool, params, target_ref, "
                    "rollback, state, policy_trace, approved_by, approved_at, "
                    "executed_at, result, created_at FROM actions WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at DESC LIMIT 200"
                ),
                params,
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/{action_id}")
async def get_action(action_id: UUID, principal: Principal = Depends(require_token)):
    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text("SELECT * FROM actions WHERE id = :id AND org_id = :org"),
                {"id": action_id, "org": principal.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    return dict(row._mapping)


@router.post("/{action_id}/approve")
async def approve(action_id: UUID, principal: Principal = Depends(require_token)):
    if principal.role not in _APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="approval requires admin or operator")
    try:
        return await approve_action(
            action_id,
            org_id=UUID(principal.org_id),
            actor_role=principal.role,
            actor=_actor(principal),
        )
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{action_id}/dry-run")
async def dry_run(action_id: UUID, principal: Principal = Depends(require_token)):
    try:
        return await dry_run_action(
            action_id, org_id=UUID(principal.org_id), actor=_actor(principal)
        )
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{action_id}/deny")
async def deny(action_id: UUID, principal: Principal = Depends(require_token)):
    if principal.role not in _APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="deny requires admin or operator")
    try:
        return await deny_action(
            action_id, org_id=UUID(principal.org_id), actor=_actor(principal)
        )
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
