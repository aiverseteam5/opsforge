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

from ..db import session_factory
from ..dispatch import create_run, resolve_nl
from ..security import Principal, require_token
from ..skills import get_skill

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

_TERMINAL = {"done", "failed", "cancelled"}


class RunCreate(BaseModel):
    # Provide an explicit skill_slug, OR a natural-language `nl` to be resolved.
    skill_slug: str | None = None
    nl: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None


@router.post("", status_code=201)
async def create_run_endpoint(
    body: RunCreate, principal: Principal = Depends(require_token)
):
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
        res = await s.execute(
            text(
                "UPDATE runs SET status='cancelled' "
                "WHERE id=:id AND org_id=:org AND status NOT IN "
                "('done','failed','cancelled')"
            ),
            {"id": run_id, "org": principal.org_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=409, detail="run not cancellable")
    return {"status": "cancelled"}


async def _fetch_events(run_id: UUID, after_seq: int) -> list[dict[str, Any]]:
    async with session_factory().begin() as s:
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
            events = await _fetch_events(run_id, last_seq)
            for ev in events:
                last_seq = ev["seq"]
                data = json.dumps({"seq": ev["seq"], "payload": ev["payload"]})
                yield f"event: {ev['kind']}\ndata: {data}\n\n"
            async with session_factory().begin() as s:
                status = (
                    await s.execute(
                        text("SELECT status FROM runs WHERE id=:id"), {"id": run_id}
                    )
                ).scalar_one_or_none()
            if status in _TERMINAL:
                # one last drain already done above; emit done sentinel and stop
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(event_source(), media_type="text/event-stream")
