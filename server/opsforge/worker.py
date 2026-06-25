"""Worker process: queue consumer + scheduler tick. Entrypoint: `worker`.

Run with `python -m opsforge.worker`. N replicas race for jobs safely via the
SKIP LOCKED claim in db.py — each job is claimed exactly once. Handlers must be
idempotent (a worker can die after claiming, before completing).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .config import get_settings
from .connectors import load_connector
from .db import claim_jobs, complete_job, fail_job, scope_to_org, session_factory
from .graph import sync_connector

logger = logging.getLogger("opsforge.worker")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def handle_noop(payload: dict[str, Any]) -> None:
    """The M0 smoke job: success is simply not raising."""
    return None


async def handle_graph_sync(payload: dict[str, Any]) -> None:
    """Re-sync one connector's slice of the operational graph (idempotent)."""
    connector_id = payload.get("connector_id")
    org_id = payload.get("org_id")
    if not connector_id or not org_id:
        raise ValueError("graph_sync job missing connector_id or org_id")
    # A1.5: connectors is under FORCE RLS — load_connector requires the job's org.
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    await sync_connector(connector)


async def handle_run_agent(payload: dict[str, Any]) -> None:
    """Run the agent loop for a queued run, then notify its surface (e.g. Slack)."""
    from .agent import run_agent
    from .gateway import LiteLLMGateway
    from .skills import get_skill_by_id
    from .surfaces.slack import notify_run

    run_id = payload.get("run_id")
    if not run_id:
        raise ValueError("run_agent job missing run_id")
    async with session_factory().begin() as s:
        skill_id = (
            await s.execute(
                text("SELECT skill_id FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one_or_none()
    skill = await get_skill_by_id(skill_id) if skill_id else None
    if skill is None:
        raise ValueError(f"run {run_id} has no installed skill")
    await run_agent(UUID(run_id), skill, LiteLLMGateway())
    # Deliver the report to the run's surface (no-op for non-Slack runs).
    try:
        await notify_run(UUID(run_id))
    except Exception:  # noqa: BLE001 - a delivery failure must not fail the run
        logger.warning("surface notify failed for run %s", run_id, exc_info=True)


async def handle_execute_action(payload: dict[str, Any]) -> None:
    """Execute an approved action through the deterministic executor (Phase 2)."""
    from .actions import execute_action

    action_id = payload.get("action_id")
    if not action_id:
        raise ValueError("execute_action job missing action_id")
    # payload["org_id"] is the authoritative job org the claim loop stamped (RLS-validated) —
    # the FORCE-RLS actions table needs it set so the restricted role can see the row.
    await execute_action(UUID(action_id), payload.get("org_id"))


async def handle_ingest(payload: dict[str, Any]) -> None:
    """Ingest a local markdown folder into the knowledge store (M6.7). Uses real
    gateway embeddings when a provider key is configured, else the keyless hash
    stand-in."""
    from .ingest import configured_embedder, ingest_directory

    org_id = payload.get("org_id")
    path = payload.get("path")
    if not org_id or not path:
        raise ValueError("ingest job missing org_id or path")
    await ingest_directory(path, org_id=org_id, embedder=configured_embedder())


async def handle_ingest_tickets(payload: dict[str, Any]) -> None:
    """Pull resolved tickets through a vault-credentialed connector and ingest them as
    behaviour observations with origin metadata (M7.5). The reconcile pass then
    decides which observations form an authoritative provenance-disjoint pattern."""
    from .ingest import configured_embedder
    from .tickets import ingest_tickets_from_connector

    org_id = payload.get("org_id")
    connector_id = payload.get("connector_id")
    if not org_id or not connector_id:
        raise ValueError("ingest_tickets job missing org_id or connector_id")
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    await ingest_tickets_from_connector(
        connector,
        org_id=org_id,
        embedder=configured_embedder(),
        since_days=int(payload.get("since_days", 90)),
    )


async def handle_ingest_knowledge(payload: dict[str, Any]) -> None:
    """Pull real documents through a vault-credentialed knowledge connector (Confluence,
    Phase B) and ingest them as document chunks with real provenance. Read-only."""
    from .ingest import configured_embedder
    from .knowledge_sources import ingest_knowledge_from_connector

    org_id = payload.get("org_id")
    connector_id = payload.get("connector_id")
    if not org_id or not connector_id:
        raise ValueError("ingest_knowledge job missing org_id or connector_id")
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    _ids, complete = await ingest_knowledge_from_connector(
        connector, org_id=org_id, embedder=configured_embedder(),
        default_process_key=payload.get("process_key"),
    )
    if not complete:
        # honest partial — surface it, do not report a partial pull as a clean success
        logger.warning("ingest_knowledge for connector %s was PARTIAL", connector_id)


async def handle_reconcile(payload: dict[str, Any]) -> None:
    """Reconcile a process's chunks then (re)generate its validated process (M6.7).
    Uses the LLM contradiction detector when a provider key is configured, else
    the lexical stand-in; the deterministic engine disposes either way."""
    from .processes import configured_drafter, generate_process
    from .reconcile import configured_detector, reconcile_process

    org_id = payload.get("org_id")
    process_key = payload.get("process_key")
    if not org_id or not process_key:
        raise ValueError("reconcile job missing org_id or process_key")
    await reconcile_process(org_id, process_key, detector=await configured_detector(org_id))
    await generate_process(org_id, process_key, drafter=configured_drafter())


# Dispatch table by job.kind.
HANDLERS: dict[str, Handler] = {
    "noop": handle_noop,
    "graph_sync": handle_graph_sync,
    "run_agent": handle_run_agent,
    "execute_action": handle_execute_action,
    "ingest": handle_ingest,
    "ingest_tickets": handle_ingest_tickets,
    "ingest_knowledge": handle_ingest_knowledge,
    "reconcile": handle_reconcile,
}


# Enqueue due connector syncs, but only every _TICK_EVERY_S so 3 workers polling
# at 500ms don't hammer the table.
_TICK_EVERY_S = 30.0
_last_tick = 0.0

_ENQUEUE_DUE_SYNCS = text(
    """
    INSERT INTO jobs (org_id, kind, payload, status, run_after, attempts)
    SELECT c.org_id, 'graph_sync',
           jsonb_build_object('connector_id', c.id::text, 'org_id', c.org_id::text),
           'queued', now(), 0
    FROM connectors c
    WHERE c.status = 'healthy'
      AND c.org_id = :org
      AND NOT EXISTS (
          SELECT 1 FROM jobs j
          WHERE j.kind = 'graph_sync' AND j.status IN ('queued', 'running')
            AND j.payload->>'connector_id' = c.id::text
      )
      AND NOT EXISTS (
          SELECT 1 FROM graph_nodes g
          WHERE g.source_connector_id = c.id
            AND g.last_seen_at > now() - make_interval(secs => :interval)
      )
    """
)


async def scheduler_tick() -> None:
    """Enqueue graph_sync jobs for due connectors and run_agent jobs for due
    cron schedules. Throttled to once per _TICK_EVERY_S across the worker pool."""
    global _last_tick
    now = time.monotonic()
    if now - _last_tick < _TICK_EVERY_S:
        return
    _last_tick = now
    org = get_settings().org_id
    async with session_factory().begin() as s:
        # Insert into the RLS-protected jobs table → declare the worker's org.
        await scope_to_org(s, org)
        await s.execute(
            _ENQUEUE_DUE_SYNCS,
            {"interval": get_settings().graph_sync_interval_s, "org": org},
        )
    await _run_due_cron_schedules()


async def _run_due_cron_schedules() -> None:
    from croniter import croniter

    from .dispatch import create_run
    from .skills import get_skill_by_id

    org = get_settings().org_id
    async with session_factory().begin() as s:
        # org-pinned worker: only fire this org's schedules, never a peer org's.
        due = (
            await s.execute(
                text(
                    "SELECT id, skill_id, cron_expr FROM schedules "
                    "WHERE enabled AND trigger_kind='cron' AND next_run_at <= now() "
                    "AND org_id = :org "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"org": org},
            )
        ).all()
        for sched in due:
            # Advance next_run_at inside the same locked txn so peers don't double-fire.
            nxt = croniter(sched.cron_expr, datetime.now(UTC)).get_next(datetime)
            await s.execute(
                text("UPDATE schedules SET next_run_at=:n WHERE id=:id"),
                {"n": nxt, "id": sched.id},
            )

    for sched in due:
        skill = await get_skill_by_id(sched.skill_id)
        if skill is None:
            continue
        result = await create_run(
            skill["slug"],
            {"query": f"scheduled run of {skill['slug']}"},
            trigger_kind="schedule",
        )
        if result:
            async with session_factory().begin() as s:
                await s.execute(
                    text("UPDATE schedules SET last_run_id=:r WHERE id=:id"),
                    {"r": result["run_id"], "id": sched.id},
                )


async def process_one(
    worker_id: str, *, max_attempts: int, org_id: str | None = None
) -> dict[str, Any] | None:
    """Claim and run a single job for this worker's org. Returns the claimed job
    dict, or None if the queue was empty. The claim commits in its own short
    transaction before the handler runs, so the row lock is never held across
    handler execution. The worker is org-pinned (M6.0): it only ever sees its own
    org's jobs."""
    org = org_id or get_settings().org_id
    async with session_factory().begin() as session:
        claimed = await claim_jobs(session, worker_id=worker_id, org_id=org, batch=1)
    if not claimed:
        return None

    job = claimed[0]
    # The org that gates connector/credential access is the AUTHORITATIVE job org —
    # RLS-validated and pinned at claim (claim_jobs RETURNING j.org_id) — never free-form
    # payload JSON. Stamp it over any payload org_id so a poisoned/legacy payload cannot
    # re-scope a handler (defense-in-depth now that connectors holds real credentials).
    payload = dict(job["payload"] or {})
    payload["org_id"] = str(job["org_id"])
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise ValueError(f"no handler for job kind {job['kind']!r}")
        await handler(payload)
        async with session_factory().begin() as session:
            await complete_job(session, job["id"], org_id=org)
    except Exception:
        logger.exception("job %s (%s) failed", job["id"], job["kind"])
        async with session_factory().begin() as session:
            await fail_job(session, job["id"], max_attempts=max_attempts, org_id=org)
    return job


async def run_forever(shutdown: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.worker_poll_interval_ms / 1000
    try:
        from .skills import install_builtin_skills

        await install_builtin_skills()
    except Exception:  # noqa: BLE001 - don't block the worker on skill install
        logger.warning("built-in skill install skipped", exc_info=True)
    logger.info("worker %s started", WORKER_ID)
    while not shutdown.is_set():
        await scheduler_tick()
        job = await process_one(
            WORKER_ID,
            max_attempts=settings.worker_max_attempts,
            org_id=settings.org_id,
        )
        if job is not None:
            # Greedily drain: loop again immediately when work was found.
            continue
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except TimeoutError:
            pass
    logger.info("worker %s stopped", WORKER_ID)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    # psycopg3 async requires a SelectorEventLoop on Windows (native dev only;
    # the container runs Linux). No-op elsewhere.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    shutdown = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_shutdown() -> None:
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows native dev: signal handlers on the loop are unsupported.
            pass

    try:
        loop.run_until_complete(run_forever(shutdown))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
