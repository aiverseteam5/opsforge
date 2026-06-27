"""Knowledge & Truth Plane API (M6.7) — the ingest-to-gate vertical slice.

ingest a source → declare disposition → reconcile (findings + a draft process) →
review the validated process → sign it off. The low-grounding gate that a run
then hits is surfaced by the existing actions API (GET /api/v1/actions). All
routes are org-scoped via the token principal; the library calls underneath set
the RLS org context, so isolation is enforced by the DB (M6.6).
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import enqueue, get_job, session_factory
from ..dispositions import declare_disposition
from ..findings import list_findings, set_finding_state
from ..knowledge import get_chunks
from ..models import DispositionDeclaration, FindingState
from ..processes import (
    get_current_process,
    list_process_versions,
    list_processes,
    sign_off_process,
)
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1", tags=["knowledge"])

_WRITER_ROLES = {"admin", "operator"}


def _require_writer(principal: Principal) -> None:
    if principal.role not in _WRITER_ROLES:
        raise HTTPException(status_code=403, detail="requires admin or operator")


class IngestBody(BaseModel):
    path: str


class ReconcileBody(BaseModel):
    process_key: str
    disposition: DispositionDeclaration | None = None
    rationale: str | None = None


class DispositionBody(BaseModel):
    process_key: str
    disposition: DispositionDeclaration
    rationale: str | None = None


class FindingTriageBody(BaseModel):
    state: FindingState


@router.post("/knowledge/ingest", status_code=202)
async def ingest_source(body: IngestBody, principal: Principal = Depends(require_token)):
    """Enqueue ingestion of a local markdown folder. Returns the job id."""
    _require_writer(principal)
    async with session_factory().begin() as s:
        job_id = await enqueue(
            s, kind="ingest",
            payload={"org_id": principal.org_id, "path": body.path},
            org_id=principal.org_id,
        )
    return {"job_id": str(job_id), "kind": "ingest"}


@router.post("/dispositions", status_code=201)
async def declare(body: DispositionBody, principal: Principal = Depends(require_token)):
    """Declare a process descriptive | prescriptive (governs conflict resolution)."""
    _require_writer(principal)
    await declare_disposition(
        org_id=principal.org_id, process_key=body.process_key,
        disposition=body.disposition, declared_by=principal.user_id, rationale=body.rationale,
    )
    return {"process_key": body.process_key, "disposition": body.disposition}


@router.post("/knowledge/reconcile", status_code=202)
async def reconcile(body: ReconcileBody, principal: Principal = Depends(require_token)):
    """Optionally declare the disposition, then enqueue reconciliation (which also
    regenerates the validated process). Returns the job id."""
    _require_writer(principal)
    if body.disposition:
        await declare_disposition(
            org_id=principal.org_id, process_key=body.process_key,
            disposition=body.disposition, declared_by=principal.user_id, rationale=body.rationale,
        )
    async with session_factory().begin() as s:
        job_id = await enqueue(
            s, kind="reconcile",
            payload={"org_id": principal.org_id, "process_key": body.process_key},
            org_id=principal.org_id,
        )
    return {"job_id": str(job_id), "kind": "reconcile"}


@router.get("/findings")
async def get_findings(
    principal: Principal = Depends(require_token),
    process_key: str | None = Query(default=None),
    state: str | None = Query(default="open"),
):
    """The reconciliation mirror: contradiction | drift | gap | violation | stale.
    `state` filters one lifecycle state; "all" (or empty) means NO filter — every
    state. (An empty string must NOT become a `state = ''` predicate, which would
    match nothing and dishonestly show an empty mirror.)"""
    eff_state = None if state in ("all", "", None) else cast(FindingState, state)
    rows = await list_findings(principal.org_id, state=eff_state, process_key=process_key)
    return [r.model_dump(mode="json") for r in rows]


@router.get("/processes/{process_key}")
async def get_process(process_key: str, principal: Principal = Depends(require_token)):
    """The current validated process — every step carries source/freshness/confidence."""
    proc = await get_current_process(principal.org_id, process_key)
    if proc is None:
        raise HTTPException(status_code=404, detail="no validated process for that key")
    return proc.model_dump(mode="json")


@router.post("/processes/{process_key}/signoff")
async def signoff(process_key: str, principal: Principal = Depends(require_token)):
    """Human signoff of the current draft process (reuses the kernel audit)."""
    _require_writer(principal)
    proc = await get_current_process(principal.org_id, process_key)
    if proc is None:
        raise HTTPException(status_code=404, detail="no validated process for that key")
    try:
        await sign_off_process(principal.org_id, proc.id, signed_by=principal.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"signed_off": str(proc.id), "version": proc.version}


# --------------------------------------------------------------------------- #
# M8 read/triage surface for the workbench (UI over existing proven engine).
# Every handler is workspace-scoped by the token principal; the library calls set
# the RLS org context, so cross-workspace data is unreachable (M6.6).
# --------------------------------------------------------------------------- #
@router.get("/knowledge/chunks")
async def list_chunks(
    principal: Principal = Depends(require_token),
    process_key: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
):
    """What was ingested, with provenance: source, kind, observed-vs-ingested dates,
    and — for ticket-sourced behaviour — the origin and its VERIFIED identity
    (provenance_root). An origin with no provenance_root is unverified/demoted; the UI
    flags it honestly. The UI never recomputes trust — confidence is shown as stored."""
    rows = await get_chunks(
        principal.org_id, process_key, include_superseded=include_superseded
    )
    return [r.model_dump(mode="json") for r in rows]


@router.get("/jobs/{job_id}")
async def job_status(job_id: UUID, principal: Principal = Depends(require_token)):
    """Status of an ingest/reconcile job (queued | running | done | failed) so a
    long-running op shows honest status instead of a spinner-forever."""
    job = await get_job(principal.org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {**job, "id": str(job["id"])}


@router.get("/processes")
async def list_all_processes(principal: Principal = Depends(require_token)):
    """The current validated process for every key — the process-review index."""
    rows = await list_processes(principal.org_id)
    return [r.model_dump(mode="json") for r in rows]


@router.get("/processes/{process_key}/versions")
async def process_versions(process_key: str, principal: Principal = Depends(require_token)):
    """Every version of one process (newest first), each with full steps + per-step
    provenance — the data behind the diff view. The UI renders the structural diff;
    the trust-bearing numbers are the backend's, shown as-is."""
    rows = await list_process_versions(principal.org_id, process_key)
    if not rows:
        raise HTTPException(status_code=404, detail="no validated process for that key")
    return [r.model_dump(mode="json") for r in rows]


@router.patch("/findings/{finding_id}")
async def triage_finding(
    finding_id: UUID,
    body: FindingTriageBody,
    principal: Principal = Depends(require_token),
):
    """Move a finding through its lifecycle (open → acknowledged | resolved | dismissed)."""
    _require_writer(principal)
    try:
        await set_finding_state(principal.org_id, finding_id, body.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": str(finding_id), "state": body.state}
