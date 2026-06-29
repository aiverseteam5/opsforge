"""Runs API: dispatch, list, detail, live SSE stream, cancel."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import scope_to_org, session_factory
from ..dispatch import create_run, resolve_nl
from ..ratelimit import run_dispatch_rate_limit
from ..security import Principal, require_token
from ..skills import get_skill

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

_TERMINAL = {"done", "failed", "cancelled"}
_SUMMARY_MAX_LEN = 200


class RunCreate(BaseModel):
    # Provide an explicit skill_slug, OR a natural-language `nl` to be resolved.
    skill_slug: str | None = None
    nl: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None


@router.post("", status_code=201)
async def create_run_endpoint(
    body: RunCreate,
    _rl: None = Depends(run_dispatch_rate_limit),
    principal: Principal = Depends(require_token),
):
    if principal.role is None:
        raise HTTPException(status_code=403, detail="delegation tokens cannot dispatch runs")
    # NL path: resolve to a skill + entities (or return candidates if ambiguous).
    if body.nl:
        resolved = await resolve_nl(
            body.nl, surface="api", user_id=principal.user_id
        )
        if resolved.get("status") == "ambiguous":
            return resolved  # 201 with candidates for the caller to disambiguate
        if "run_id" not in resolved:
            raise HTTPException(status_code=404, detail="could not resolve a skill")
        return resolved

    if not body.skill_slug:
        raise HTTPException(status_code=400, detail="skill_slug or nl is required")
    result = await create_run(
        body.skill_slug,
        body.inputs,
        trigger_kind="manual",
        surface="api",
        user_id=principal.user_id,
        model=body.model,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"skill {body.skill_slug} not found")
    return result


class RunSummary(BaseModel):
    id: UUID
    skill_id: UUID | None
    status: str
    model: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@router.get("", response_model=list[RunSummary])
async def list_runs(
    principal: Principal = Depends(require_token),
    status: str | None = Query(default=None),
    skill: str | None = Query(default=None),
):
    clauses = ["org_id = :org"]
    params: dict[str, Any] = {"org": principal.org_id}
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if skill:
        sk = await get_skill(skill)
        clauses.append("skill_id = :skill_id")
        params["skill_id"] = sk["id"] if sk else None
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT id, skill_id, status, model, created_at, started_at, "
                    "finished_at FROM runs WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at DESC LIMIT 100"
                ),
                params,
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/{run_id}")
async def get_run(run_id: UUID, principal: Principal = Depends(require_token)):
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, skill_id, status, model, trigger, report_md, "
                    "report_json, tokens_in, tokens_out, created_at, started_at, "
                    "finished_at FROM runs WHERE id = :id AND org_id = :org"
                ),
                {"id": run_id, "org": principal.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return dict(row._mapping)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: UUID, principal: Principal = Depends(require_token)):
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        res = await s.execute(
            text(
                "UPDATE runs SET status='cancelled' "
                "WHERE id=:id AND org_id=:org AND status NOT IN "
                "('done','failed','cancelled')"
            ),
            {"id": run_id, "org": principal.org_id},
        )
    if res.rowcount == 0:  # type: ignore[attr-defined]
        raise HTTPException(status_code=409, detail="run not cancellable")
    return {"status": "cancelled"}


async def _fetch_events(
    run_id: UUID, after_seq: int, org_id: str
) -> list[dict[str, Any]]:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT seq, kind, payload FROM run_events "
                    "WHERE run_id = :id AND seq > :after ORDER BY seq"
                ),
                {"id": run_id, "after": after_seq},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/{run_id}/events")
async def stream_events(run_id: UUID, principal: Principal = Depends(require_token)):
    """SSE stream of run_events: replays from seq 0, then follows live until the
    run reaches a terminal state."""

    async def event_source():
        last_seq = 0
        for _ in range(900):  # ~6 min cap
            events = await _fetch_events(run_id, last_seq, principal.org_id)
            for ev in events:
                last_seq = ev["seq"]
                payload = ev["payload"] or {}
                if principal.scope is not None:
                    payload = {k: v for k, v in payload.items() if k != "scope"}
                data = json.dumps({"seq": ev["seq"], "payload": payload})
                yield f"event: {ev['kind']}\ndata: {data}\n\n"
            async with session_factory().begin() as s:
                await scope_to_org(s, principal.org_id)
                status = (
                    await s.execute(
                        text(
                            "SELECT status FROM runs WHERE id=:id AND org_id=:org"
                        ),
                        {"id": run_id, "org": principal.org_id},
                    )
                ).scalar_one_or_none()
            if status in _TERMINAL:
                # one last drain already done above; emit done sentinel and stop
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(event_source(), media_type="text/event-stream")


class PostmortemBody(BaseModel):
    channel: str | None = None  # optional Slack channel override


@router.post("/{run_id}/postmortem", status_code=202)
async def request_postmortem(
    run_id: UUID,
    body: PostmortemBody,
    principal: Principal = Depends(require_token),
):
    """Enqueue an AI postmortem generation job for a completed run (C4/E1).

    The job reads the run's events, asks the LLM to write a blameless postmortem,
    stores the result in the patterns table, and optionally delivers it to Slack.
    Returns 409 if the run is not in a terminal state.
    """
    from ..db import enqueue

    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :id AND org_id = :org"),
                {"id": run_id, "org": principal.org_id},
            )
        ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    if row.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"run status is '{row.status}' — postmortem requires a terminal run",
        )

    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        job_id = await enqueue(
            s,
            kind="postmortem",
            payload={
                "run_id": str(run_id),
                "org_id": principal.org_id,
                "channel": body.channel,
            },
            org_id=principal.org_id,
        )

    return {"job_id": str(job_id), "run_id": str(run_id), "status": "queued"}


@router.get("/{run_id}/timeline")
async def get_run_timeline(
    run_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    after_seq: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_token),
):
    """Shaped timeline of run_events for the war-room view.

    Pagination: use `after_seq` cursor + `next_after_seq` from the response for
    incremental load. Returns at most `limit` events (max 200).
    """
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        run_row = (
            await s.execute(
                text("SELECT id FROM runs WHERE id=:id AND org_id=:org"),
                {"id": run_id, "org": principal.org_id},
            )
        ).first()
        if run_row is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        rows = (
            await s.execute(
                text(
                    "SELECT seq, created_at AS ts, kind, payload "
                    "FROM run_events "
                    "WHERE run_id=:id AND seq > :after "
                    "ORDER BY seq LIMIT :limit"
                ),
                {"id": run_id, "after": after_seq, "limit": limit},
            )
        ).all()

    events = []
    next_seq = after_seq
    for r in rows:
        payload = r.payload or {}
        # Delegation callers must not receive scope lists stored in event payloads —
        # those belong to the issuing run's trust context, not the caller's view.
        if principal.scope is not None:
            payload = {k: v for k, v in payload.items() if k != "scope"}
        actor = "human" if r.kind in ("proposal",) else "agent"
        summary = (
            payload.get("summary")
            or payload.get("tool")
            or payload.get("hypothesis")
            or r.kind
        )
        events.append(
            {
                "seq": r.seq,
                "ts": r.ts.isoformat() if r.ts else None,
                "kind": r.kind,
                "actor": actor,
                "summary": str(summary)[:_SUMMARY_MAX_LEN],
                "payload": payload,
                "proposal_id": payload.get("proposal_id"),
            }
        )
        next_seq = r.seq

    return {"run_id": str(run_id), "next_after_seq": next_seq, "events": events}
