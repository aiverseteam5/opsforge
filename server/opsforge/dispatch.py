"""Run dispatch — create a run row + enqueue its run_agent job.

Shared by the runs API, the Slack surface, the alert webhook, and the scheduler
so "start an investigation" has exactly one implementation. Lives below the
api/surfaces layer so both can depend on it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from .config import get_settings
from .db import enqueue, record_audit, scope_to_org, session_factory
from .skills import get_skill, list_skills

_OPS_KINDS = ("servicenow", "jira", "pagerduty")
_ENTITY_REF_RE = re.compile(r"\b(INC\d+|CHG\d+|PRB\d+|[A-Z]{2,}-\d+)\b")
_WORD = re.compile(r"[a-z0-9]+")


def _safe_format(template: str, values: dict[str, Any]) -> str:
    return template.format_map(defaultdict(lambda: "?", values))


async def _insert_run(
    skill_id: Any,
    org_id: str,
    inputs: dict[str, Any],
    *,
    trigger_kind: str,
    surface: str | None,
    channel: str | None,
    user_id: str | None,
    model: str | None,
) -> str:
    import json

    trigger = {
        "kind": trigger_kind,
        "payload": inputs,
        "surface": surface,
        "channel": channel,
        "user_id": user_id,
    }
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        run_id = (
            await s.execute(
                text(
                    "INSERT INTO runs (org_id, skill_id, status, trigger, model) "
                    "VALUES (:org,:skill,'queued',CAST(:trigger AS jsonb),:model) "
                    "RETURNING id"
                ),
                {
                    "org": org_id,
                    "skill": skill_id,
                    "trigger": json.dumps(trigger),
                    "model": model,
                },
            )
        ).scalar_one()
        await enqueue(s, kind="run_agent", payload={"run_id": str(run_id)}, org_id=org_id)
    return str(run_id)


async def create_run(
    skill_slug: str,
    inputs: dict[str, Any],
    *,
    trigger_kind: str = "manual",
    surface: str | None = None,
    channel: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a skill by slug, create a queued run, enqueue the agent job."""
    skill = await get_skill(skill_slug)
    if skill is None or not skill["enabled"]:
        return None
    org_id = get_settings().org_id
    run_id = await _insert_run(
        skill["id"],
        org_id,
        inputs,
        trigger_kind=trigger_kind,
        surface=surface,
        channel=channel,
        user_id=user_id,
        model=model,
    )
    actor = f"user:{user_id}" if user_id else f"system:{trigger_kind}"
    await record_audit(
        org_id,
        actor,
        "run.dispatched",
        subject_ref=run_id,
        detail={"skill": skill_slug, "surface": surface, "trigger_kind": trigger_kind},
    )
    return {"run_id": run_id, "status": "queued"}


# --------------------------------------------------------------------------- #
# NL intent resolution (GAP 2): nl -> skill + entity -> run
# --------------------------------------------------------------------------- #
def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if len(w) >= 3}


def _skill_terms(skill: dict[str, Any]) -> set[str]:
    m = skill.get("manifest") or {}
    blob = f"{skill['slug']} {m.get('name', '')} {m.get('description', '')}"
    return _tokens(blob)


def _rank_skills(nl: str, skills: list[dict[str, Any]]) -> list[tuple[int, dict]]:
    q = _tokens(nl)
    scored = [(len(q & _skill_terms(sk)), sk) for sk in skills]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


async def _resolve_incident_entity(org_id: str, nl: str) -> str | None:
    """Extract an incident ref from the text, or look one up via an ITSM connector."""
    m = _ENTITY_REF_RE.search(nl)
    if m:
        return m.group(1)
    from .connectors import load_connectors_by_kind
    from .ops_adapter import search_incident_ref

    by_kind = await load_connectors_by_kind(org_id)
    connector = next((by_kind[k] for k in _OPS_KINDS if k in by_kind), None)
    if connector is None:
        return None
    # crude service-token extraction: a hyphenated token like "payment-svc"
    svc = next((t for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", nl.lower())), None)
    if not svc:
        return None
    try:
        return await search_incident_ref(connector, svc.split("-")[0])
    except Exception:  # noqa: BLE001 - entity lookup is best-effort
        return None


async def resolve_nl(
    nl: str,
    *,
    surface: str | None = None,
    channel: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a natural-language command to a skill + entities, then dispatch.

    Deterministic v1: keyword match over installed skills (embedding tie-break is
    the documented fast-follow); one entity lookup; ambiguous → candidates (never
    guess). Returns either a created run or `{status: "ambiguous", candidates}`.
    """
    skills = [s for s in await list_skills() if s["enabled"]]
    if not skills:
        return {"status": "no_skills"}

    ranked = _rank_skills(nl, skills)
    top_score, top = ranked[0]
    if top_score == 0:
        # No keyword signal → default to the general investigator (never dead-end;
        # matches the original ⌘K behaviour). Embedding ranking is the fast-follow.
        top = next(
            (s for s in skills if s["slug"] == "incident-investigation"), top
        )
    elif len(ranked) > 1 and ranked[1][0] == top_score:
        # A genuine tie between matched skills → ask, never guess.
        cands = [r[1] for r in ranked if r[0] == top_score]
        return {
            "status": "ambiguous",
            "nl": nl,
            "candidates": [
                {"slug": c["slug"], "name": (c.get("manifest") or {}).get("name", c["slug"])}
                for c in cands[:4]
            ],
        }

    org_id = get_settings().org_id
    inputs: dict[str, Any] = {"query": nl}
    declared = [i.get("name") for i in (top.get("manifest") or {}).get("inputs", [])]
    if "incident_ref" in declared:
        ref = await _resolve_incident_entity(org_id, nl)
        if ref:
            inputs["incident_ref"] = ref

    result = await create_run(
        top["slug"], inputs, trigger_kind="manual",
        surface=surface, channel=channel, user_id=user_id,
    )
    if result is None:
        return {"status": "no_skills"}
    return {**result, "skill_slug": top["slug"], "inputs": inputs}


def _matches(match: dict[str, Any], alert: dict[str, Any]) -> bool:
    """Every key in the filter's `match` must equal the alert's value."""
    return all(str(alert.get(k)) == str(v) for k, v in (match or {}).items())


async def dispatch_from_alert(alert: dict[str, Any]) -> list[dict[str, Any]]:
    """Match an inbound alert against enabled event schedules and dispatch a run
    for each match, reporting to the schedule's configured surface."""
    org_id = get_settings().org_id
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        schedules = (
            await s.execute(
                text(
                    "SELECT id, skill_id, event_filter FROM schedules "
                    "WHERE org_id=:org AND enabled AND trigger_kind='event'"
                ),
                {"org": org_id},
            )
        ).all()

    dispatched: list[dict[str, Any]] = []
    for sched in schedules:
        ef = sched.event_filter or {}
        if not _matches(ef.get("match", {}), alert):
            continue
        template = ef.get("query_template", "Investigate alert: {summary}")
        inputs = {
            "query": _safe_format(template, alert),
            "incident_ref": alert.get("incident_ref") or alert.get("ref"),
            "alert": alert,
        }
        notify = ef.get("notify", {})
        run_id = await _insert_run(
            sched.skill_id,
            org_id,
            inputs,
            trigger_kind="event",
            surface=notify.get("surface"),
            channel=notify.get("channel"),
            user_id=None,
            model=None,
        )
        async with session_factory().begin() as s:
            await scope_to_org(s, org_id)
            await s.execute(
                text("UPDATE schedules SET last_run_id=:r WHERE id=:id"),
                {"r": run_id, "id": sched.id},
            )
        dispatched.append({"run_id": run_id, "schedule_id": str(sched.id)})
    return dispatched
