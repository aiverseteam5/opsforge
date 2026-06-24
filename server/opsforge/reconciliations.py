"""Reconciliation run records (M7.4) — one row per reconcile_process run.

The point is observability of DEGRADED runs: with the LLM detector on the
production hot path, a provider failure falls back to the lexical floor, and a
fallback run must not silently look like a normal one. Each run records which
detector actually ran (`lexical_fallback` = the LLM failed) plus its counts, so a
human or a scorecard can see it ran degraded. RLS-scoped like the rest of the
plane; this module only touches `db` (its own layer band), never its peers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .db import scope_to_org, session_factory

DetectorMode = str  # 'llm' | 'lexical' | 'lexical_fallback' | 'scripted' | 'unknown'

_INSERT = text(
    """
    INSERT INTO reconciliations
        (id, org_id, process_key, detector, scored, superseded, findings)
    VALUES (:id, :org, :pk, :detector, :scored, :superseded, :findings)
    """
)


class ReconciliationRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    process_key: str
    detector: str
    scored: int
    superseded: int
    findings: int


async def record_reconciliation(
    org_id: Any,
    recon_id: UUID,
    process_key: str,
    *,
    detector: DetectorMode,
    scored: int,
    superseded: int,
    findings: int,
) -> None:
    """Append the run record. RLS-scoped. Best-effort: a failure to record must not
    fail the reconciliation itself (the scoring already happened)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            _INSERT,
            {
                "id": str(recon_id),
                "org": str(org_id),
                "pk": process_key,
                "detector": detector,
                "scored": scored,
                "superseded": superseded,
                "findings": findings,
            },
        )


async def get_reconciliation(org_id: Any, recon_id: UUID) -> ReconciliationRecord | None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, process_key, detector, scored, superseded, findings "
                    "FROM reconciliations WHERE id = :id AND org_id = :org"
                ),
                {"id": str(recon_id), "org": str(org_id)},
            )
        ).first()
    return ReconciliationRecord.model_validate(dict(row._mapping)) if row else None


async def latest_reconciliation(
    org_id: Any, process_key: str
) -> ReconciliationRecord | None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, process_key, detector, scored, superseded, findings "
                    "FROM reconciliations WHERE org_id = :org AND process_key = :pk "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"org": str(org_id), "pk": process_key},
            )
        ).first()
    return ReconciliationRecord.model_validate(dict(row._mapping)) if row else None
