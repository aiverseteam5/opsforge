"""Findings — the reconciliation mirror surfaced in the approval queue (M6.3).

A finding is an insert-only record (doctrine #7) that something needs human
attention: a contradiction, drift, gap, prescriptive violation, or a stale
supersession. evidence_refs carries the chunk ids that prove it, so a reviewer
can always trace the claim back to its sources.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .db import scope_to_org, session_factory
from .models import FindingKind, FindingState

_INSERT = text(
    """
    INSERT INTO findings
        (org_id, process_key, kind, detail, evidence_refs, confidence, reconciliation_id)
    VALUES
        (:org, :process_key, :kind, CAST(:detail AS jsonb),
         CAST(:evidence_refs AS jsonb), :confidence, :reconciliation_id)
    RETURNING id
    """
)

_COLS = (
    "id, org_id, process_key, kind, detail, evidence_refs, confidence, state, "
    "reconciliation_id, seq"
)


class FindingRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    process_key: str | None
    kind: FindingKind
    detail: dict[str, Any] | None = None
    evidence_refs: list[Any]
    confidence: float | None = None
    state: FindingState
    reconciliation_id: UUID | None = None
    seq: int


async def emit_finding(
    *,
    org_id: Any,
    kind: FindingKind,
    process_key: str | None = None,
    detail: dict[str, Any] | None = None,
    evidence_refs: list[Any] | None = None,
    confidence: float | None = None,
    reconciliation_id: UUID | None = None,
) -> UUID:
    """Append a finding to the queue (state defaults to 'open'). RLS-scoped."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                _INSERT,
                {
                    "org": str(org_id),
                    "process_key": process_key,
                    "kind": kind,
                    "detail": json.dumps(detail or {}),
                    "evidence_refs": json.dumps([str(r) for r in (evidence_refs or [])]),
                    "confidence": confidence,
                    "reconciliation_id": str(reconciliation_id) if reconciliation_id else None,
                },
            )
        ).one()
    return row.id


async def list_findings(
    org_id: Any,
    *,
    state: FindingState | None = "open",
    process_key: str | None = None,
    limit: int = 200,
) -> list[FindingRow]:
    """List findings (oldest first by seq) for the queue. RLS-scoped."""
    clauses = ["org_id = :org"]
    params: dict[str, Any] = {"org": str(org_id), "limit": limit}
    if state is not None:
        clauses.append("state = :state")
        params["state"] = state
    if process_key is not None:
        clauses.append("process_key = :pk")
        params["pk"] = process_key
    where = " AND ".join(clauses)
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(f"SELECT {_COLS} FROM findings WHERE {where} ORDER BY seq LIMIT :limit"),
                params,
            )
        ).all()
    return [FindingRow.model_validate(dict(r._mapping)) for r in rows]


async def set_finding_state(org_id: Any, finding_id: UUID, state: FindingState) -> None:
    """Move a finding through its lifecycle (open → acknowledged/resolved/dismissed)."""
    if state not in ("open", "acknowledged", "resolved", "dismissed"):
        raise ValueError(f"invalid finding state {state!r}")
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text("UPDATE findings SET state = :state WHERE id = :id AND org_id = :org"),
            {"state": state, "id": str(finding_id), "org": str(org_id)},
        )
