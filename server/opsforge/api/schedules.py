"""Schedules API: CRUD for cron and event triggers.

cron schedules are scanned by the worker scheduler tick; event schedules are
matched by the alert webhook (see dispatch.dispatch_from_alert).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from ..db import record_audit, session_factory
from ..security import Principal, require_token
from ..skills import get_skill

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str
    skill_slug: str
    trigger_kind: Literal["cron", "event"]
    cron_expr: str | None = None
    event_filter: dict[str, Any] | None = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron_expr: str | None = None
    event_filter: dict[str, Any] | None = None
    enabled: bool | None = None


def _next_cron(cron_expr: str) -> datetime:
    return croniter(cron_expr, datetime.now(UTC)).get_next(datetime)


_COLS = (
    "id, name, skill_id, trigger_kind, cron_expr, event_filter, enabled, "
    "next_run_at, last_run_id, created_at"
)


@router.get("")
async def list_schedules(principal: Principal = Depends(require_token)):
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(f"SELECT {_COLS} FROM schedules WHERE org_id=:org ORDER BY created_at"),
                {"org": principal.org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.post("", status_code=201)
async def create_schedule(
    body: ScheduleCreate, principal: Principal = Depends(require_token)
):
    skill = await get_skill(body.skill_slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if body.trigger_kind == "cron":
        if not body.cron_expr or not croniter.is_valid(body.cron_expr):
            raise HTTPException(status_code=400, detail="valid cron_expr required")
        next_run = _next_cron(body.cron_expr)
    else:
        next_run = None

    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text(
                    "INSERT INTO schedules (org_id, skill_id, name, trigger_kind, "
                    "cron_expr, event_filter, enabled, next_run_at) "
                    "VALUES (:org,:skill,:name,:tk,:cron,CAST(:ef AS jsonb),:en,:nr) "
                    f"RETURNING {_COLS}"
                ),
                {
                    "org": principal.org_id,
                    "skill": skill["id"],
                    "name": body.name,
                    "tk": body.trigger_kind,
                    "cron": body.cron_expr,
                    "ef": json.dumps(body.event_filter) if body.event_filter else None,
                    "en": body.enabled,
                    "nr": next_run,
                },
            )
        ).one()
    out = dict(row._mapping)
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id,
        actor,
        "schedule.created",
        subject_ref=str(out["id"]),
        detail={"name": body.name, "trigger_kind": body.trigger_kind},
    )
    return out


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: UUID,
    body: ScheduleUpdate,
    principal: Principal = Depends(require_token),
):
    sets: list[str] = []
    params: dict[str, Any] = {"id": schedule_id, "org": principal.org_id}
    if body.name is not None:
        sets.append("name=:name")
        params["name"] = body.name
    if body.enabled is not None:
        sets.append("enabled=:en")
        params["en"] = body.enabled
    if body.event_filter is not None:
        sets.append("event_filter=CAST(:ef AS jsonb)")
        params["ef"] = json.dumps(body.event_filter)
    if body.cron_expr is not None:
        if not croniter.is_valid(body.cron_expr):
            raise HTTPException(status_code=400, detail="invalid cron_expr")
        sets.append("cron_expr=:cron")
        sets.append("next_run_at=:nr")
        params["cron"] = body.cron_expr
        params["nr"] = _next_cron(body.cron_expr)
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")

    async with session_factory().begin() as s:
        res = await s.execute(
            text(
                f"UPDATE schedules SET {', '.join(sets)} "
                "WHERE id=:id AND org_id=:org"
            ),
            params,
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"status": "updated"}


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: UUID, principal: Principal = Depends(require_token)
):
    async with session_factory().begin() as s:
        res = await s.execute(
            text("DELETE FROM schedules WHERE id=:id AND org_id=:org"),
            {"id": schedule_id, "org": principal.org_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="schedule not found")
