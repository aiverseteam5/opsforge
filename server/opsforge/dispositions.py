"""Process disposition — the human-owned descriptive/prescriptive declaration.

This is the policy decision the system cannot guess (doctrine #4): for a
*descriptive* process the document should match reality (behaviour wins on
conflict → propose a doc update); for a *prescriptive* process reality should
match the document (document is law → behaviour that diverges is a violation).
Until a human declares it, a process is `undeclared` and conflicts are surfaced,
never auto-resolved.

Append-only and audited (doctrine #7): declaring inserts a new row, the latest
per (org, process_key) is current, and every declaration writes an audit_log
entry, so "who signed this off, when, and why" is always answerable.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from .db import record_audit, scope_to_org, session_factory
from .models import DispositionDeclaration, ProcessDisposition

_INSERT = text(
    """
    INSERT INTO process_dispositions
        (org_id, process_key, disposition, declared_by, rationale)
    VALUES (:org, :process_key, :disposition, :declared_by, :rationale)
    RETURNING id
    """
)

_CURRENT = text(
    """
    SELECT disposition FROM process_dispositions
    WHERE org_id = :org AND process_key = :process_key
    ORDER BY seq DESC
    LIMIT 1
    """
)


async def declare_disposition(
    *,
    org_id: Any,
    process_key: str,
    disposition: DispositionDeclaration,
    declared_by: UUID | str | None = None,
    rationale: str | None = None,
) -> UUID:
    """Record a human's disposition declaration for a process (append-only) and
    audit it. RLS-scoped."""
    if disposition not in ("descriptive", "prescriptive"):
        raise ValueError(f"invalid disposition {disposition!r}")
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                _INSERT,
                {
                    "org": str(org_id),
                    "process_key": process_key,
                    "disposition": disposition,
                    "declared_by": str(declared_by) if declared_by else None,
                    "rationale": rationale,
                },
            )
        ).one()
    await record_audit(
        org_id,
        actor=f"user:{declared_by}" if declared_by else "system",
        event="disposition.declared",
        subject_ref=process_key,
        detail={"disposition": disposition, "rationale": rationale},
    )
    return row.id


async def get_disposition(org_id: Any, process_key: str) -> ProcessDisposition:
    """Current disposition for a process: the latest declaration, or 'undeclared'
    if a human has not declared one yet. RLS-scoped."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        current = (
            await s.execute(_CURRENT, {"org": str(org_id), "process_key": process_key})
        ).scalar_one_or_none()
    return current or "undeclared"
