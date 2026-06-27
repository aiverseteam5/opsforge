"""Async engine, session factory, and the Postgres-backed job queue.

The queue is doctrine #1: no Redis/Kafka. Jobs live in the `jobs` table and are
claimed with `FOR UPDATE SKIP LOCKED`, which lets N workers race for work with
exactly-once *claim* semantics. Handlers must be idempotent (at-least-once
execution) because a worker can die after claiming but before completing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with session_factory()() as session:
        yield session


# --------------------------------------------------------------------------- #
# Queue helpers
# --------------------------------------------------------------------------- #

_ENQUEUE_SQL = text(
    """
    INSERT INTO jobs (org_id, kind, payload, status, run_after, attempts)
    VALUES (:org_id, :kind, CAST(:payload AS jsonb), 'queued',
            COALESCE(:run_after, now()), 0)
    RETURNING id
    """
)

# Select-and-lock claimable rows, then flip them to 'running' in the SAME
# transaction. SKIP LOCKED makes each row claimable by exactly one transaction;
# committing the status change means it can never be re-selected. The `org_id`
# predicate is the app-level half of M6.0's isolation (RLS on `jobs` is the DB
# net); together they guarantee a worker only ever claims its own org's jobs.
_CLAIM_SQL = text(
    """
    WITH claimed AS (
        SELECT id
        FROM jobs
        WHERE status = 'queued'
          AND org_id = :org
          AND run_after <= now()
        ORDER BY run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    UPDATE jobs j
    SET status = 'running',
        locked_by = :worker_id,
        locked_at = now(),
        attempts = j.attempts + 1,
        updated_at = now()
    FROM claimed
    WHERE j.id = claimed.id
    RETURNING j.id, j.org_id, j.kind, j.payload, j.attempts
    """
)

_COMPLETE_SQL = text(
    "UPDATE jobs SET status = 'done', locked_by = NULL, locked_at = NULL, "
    "updated_at = now() WHERE id = :id"
)

# On failure: retry with exponential-ish backoff until max attempts, then fail.
_FAIL_SQL = text(
    """
    UPDATE jobs
    SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
        locked_by = NULL,
        locked_at = NULL,
        run_after = now() + (interval '5 seconds' * attempts),
        updated_at = now()
    WHERE id = :id
    """
)


# Declare the org for this transaction. Postgres RLS on `jobs` (migration 0004,
# and the M6 knowledge tables) reads this GUC; is_local=true scopes it to the
# current transaction so it can never leak across pooled connections. Any
# transaction touching an RLS-protected table MUST call this first or it fails
# closed (sees and writes zero rows).
_SCOPE_ORG_SQL = text("SELECT set_config('opsforge.current_org', :org, true)")


async def scope_to_org(session: AsyncSession, org_id: Any) -> None:
    """Set the per-transaction org GUC that RLS policies enforce."""
    await session.execute(_SCOPE_ORG_SQL, {"org": str(org_id)})


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    org_id: str | None = None,
    run_after: str | None = None,
) -> UUID:
    """Insert a job. Caller owns the transaction (commit after)."""
    org = org_id or get_settings().org_id
    await scope_to_org(session, org)
    row = (
        await session.execute(
            _ENQUEUE_SQL,
            {
                "org_id": org,
                "kind": kind,
                "payload": json.dumps(payload or {}),
                "run_after": run_after,
            },
        )
    ).one()
    return row.id


_ENQUEUE_IDEMPOTENT_SQL = text(
    """
    INSERT INTO jobs (org_id, kind, payload, status, run_after, attempts)
    VALUES (:org_id, :kind, CAST(:payload AS jsonb), 'queued',
            COALESCE(:run_after, now()), 0)
    ON CONFLICT DO NOTHING
    RETURNING id
    """
)


async def enqueue_idempotent(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    org_id: str | None = None,
    run_after: str | None = None,
) -> UUID | None:
    """Insert a job, silently skipping on conflict. Returns the new job id, or None
    if a conflicting row already exists. Caller owns the transaction (commit after)."""
    org = org_id or get_settings().org_id
    await scope_to_org(session, org)
    row = (
        await session.execute(
            _ENQUEUE_IDEMPOTENT_SQL,
            {
                "org_id": org,
                "kind": kind,
                "payload": json.dumps(payload or {}),
                "run_after": run_after,
            },
        )
    ).first()
    return row.id if row else None


async def claim_jobs(
    session: AsyncSession, *, worker_id: str, org_id: str | None = None, batch: int = 1
) -> list[dict[str, Any]]:
    """Atomically claim up to `batch` of *this org's* jobs. The worker is pinned
    to one org (M6.0); caller owns the (short) transaction."""
    org = org_id or get_settings().org_id
    await scope_to_org(session, org)
    rows = (
        await session.execute(
            _CLAIM_SQL, {"worker_id": worker_id, "org": str(org), "batch": batch}
        )
    ).all()
    return [dict(r._mapping) for r in rows]


async def complete_job(
    session: AsyncSession, job_id: UUID, *, org_id: str | None = None
) -> None:
    await scope_to_org(session, org_id or get_settings().org_id)
    await session.execute(_COMPLETE_SQL, {"id": job_id})


async def fail_job(
    session: AsyncSession, job_id: UUID, *, max_attempts: int, org_id: str | None = None
) -> None:
    await scope_to_org(session, org_id or get_settings().org_id)
    await session.execute(
        _FAIL_SQL, {"id": job_id, "max_attempts": max_attempts}
    )


async def get_job(org_id: str, job_id: UUID) -> dict[str, Any] | None:
    """Status of one job for the operator surface (running/done/failed). RLS-scoped;
    the explicit org predicate keeps it correct under the dev superuser role too."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, kind, status, attempts, run_after, created_at, updated_at "
                    "FROM jobs WHERE id = :id AND org_id = :org"
                ),
                {"id": str(job_id), "org": org_id},
            )
        ).first()
    return dict(row._mapping) if row else None


# --------------------------------------------------------------------------- #
# run_events (append-only; monotonic seq per run). SSE streams this table.
# --------------------------------------------------------------------------- #
_APPEND_EVENT_SQL = text(
    """
    INSERT INTO run_events (org_id, run_id, seq, kind, payload)
    VALUES (
        :org_id, :run_id,
        (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = :run_id),
        :kind, CAST(:payload AS jsonb)
    )
    RETURNING seq
    """
)


async def append_run_event(
    run_id: UUID, org_id: Any, kind: str, payload: dict[str, Any]
) -> int:
    """Append one event to a run's stream. Payload must already be redacted."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        return (
            await s.execute(
                _APPEND_EVENT_SQL,
                {
                    "org_id": str(org_id),
                    "run_id": str(run_id),
                    "kind": kind,
                    "payload": json.dumps(payload),
                },
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# audit_log (append-only; immutable trail)
# --------------------------------------------------------------------------- #
_AUDIT_SQL = text(
    "INSERT INTO audit_log (org_id, actor, event, subject_ref, detail) "
    "VALUES (:org, :actor, :event, :subject, CAST(:detail AS jsonb))"
)


async def record_audit(
    org_id: Any,
    actor: str,
    event: str,
    subject_ref: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an immutable audit entry. Actor is user:<id> | system:<x> | agent:<run>."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            _AUDIT_SQL,
            {
                "org": str(org_id),
                "actor": actor,
                "event": event,
                "subject": subject_ref,
                "detail": json.dumps(detail or {}),
            },
        )
