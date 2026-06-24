"""Audit API: read the immutable audit trail (append-only; never mutated)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from ..db import session_factory
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("")
async def list_audit(
    principal: Principal = Depends(require_token),
    subject: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    clauses = ["org_id = :org"]
    params: dict[str, object] = {"org": principal.org_id, "limit": limit}
    if subject:
        clauses.append("subject_ref = :subject")
        params["subject"] = subject
    if actor:
        clauses.append("actor = :actor")
        params["actor"] = actor
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT seq, actor, event, subject_ref, detail, created_at "
                    "FROM audit_log WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY seq DESC LIMIT :limit"
                ),
                params,
            )
        ).all()
    return [dict(r._mapping) for r in rows]
