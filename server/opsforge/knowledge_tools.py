"""Internal read-only tools (G2) — the chat assistant's INVESTIGATE surface.

The agent calls these to look up the validated knowledge plane and answer with
evidence + provenance. They are READ-ONLY by construction: every tool only SELECTs,
each is org/RLS-scoped, none writes or proposes — so the trust ladder never gates a
read (policy.check_tool_call passes class=read_only freely during a run). Low-confidence
material is marked `unverified` here exactly as context-assembly marks it (M6.5
honesty), so the streamed evidence never dresses a weak source up as fact.

The registry maps an fqn in the reserved ``kb.*`` namespace to its JSON schema +
handler; build_toolbelt exposes exactly the ones a skill manifest declares as tools.
Handlers take (org_id, params, run_id) and return JSON-safe, bounded dicts.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .config import get_settings
from .db import scope_to_org, session_factory
from .dispositions import get_disposition
from .findings import list_findings
from .knowledge import freshness_days, get_chunks

Handler = Callable[[Any, dict[str, Any], UUID], Awaitable[Any]]

# Never flood the model context or the SSE stream — reads are bounded.
_MAX = 12


@dataclass(frozen=True)
class ToolSpec:
    description: str
    parameters: dict[str, Any]
    handler: Handler


# --------------------------------------------------------------------------- #
# evidence views (honest: low-confidence is marked, never hidden)
# --------------------------------------------------------------------------- #
def _chunk_view(c: Any, threshold: float) -> dict[str, Any]:
    conf = float(c.confidence) if c.confidence is not None else None
    return {
        "content": c.content,
        "process_key": c.process_key,
        "source_kind": c.source_kind,
        "source_ref": c.source_ref,
        "origin": c.origin,
        "confidence": round(conf, 2) if conf is not None else None,
        "age_days": freshness_days(c.observed_at),
        "corroborated_by": c.corroborated_by,
        "contradicted_by": c.contradicted_by,
        # mirrors _render_knowledge: below threshold (or unscored) is NOT fact
        "unverified": conf is None or conf < threshold,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def _list_processes(org_id: Any, params: dict[str, Any], run_id: UUID) -> Any:
    """Discover which processes exist in this workspace (the investigate entrypoint)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT process_key, version, status, "
                    "jsonb_array_length(steps) AS step_count, min_confidence "
                    "FROM validated_processes WHERE org_id = :o AND status != 'superseded' "
                    "ORDER BY process_key LIMIT :lim"
                ),
                {"o": str(org_id), "lim": _MAX},
            )
        ).all()
    out = []
    for r in rows:
        out.append(
            {
                "process_key": r.process_key,
                "version": r.version,
                "status": r.status,
                "step_count": r.step_count,
                "min_confidence": (
                    round(float(r.min_confidence), 2) if r.min_confidence is not None else None
                ),
                "disposition": await get_disposition(org_id, r.process_key),
            }
        )
    return {"processes": out, "count": len(out)}


async def _process(org_id: Any, params: dict[str, Any], run_id: UUID) -> Any:
    """The current validated process for a key — its steps with per-step confidence,
    provenance kinds, and disposition. Absent → say so (acting blind)."""
    pk = (params or {}).get("process_key")
    if not pk:
        return {"error": "process_key is required"}
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT process_key, version, status, steps, min_confidence "
                    "FROM validated_processes WHERE org_id = :o AND process_key = :pk "
                    "AND status != 'superseded' ORDER BY version DESC LIMIT 1"
                ),
                {"o": str(org_id), "pk": str(pk)},
            )
        ).first()
    if row is None:
        return {
            "found": False,
            "process_key": pk,
            "note": "no validated process on file — treat as UNVERIFIED and gather evidence",
        }
    threshold = get_settings().context_grounding_threshold
    steps = []
    for st in row.steps or []:
        conf = st.get("confidence")
        steps.append(
            {
                "index": st.get("index"),
                "kind": st.get("kind", "step"),
                "text": st.get("text", ""),
                "confidence": round(float(conf), 2) if conf is not None else None,
                "unverified": bool(st.get("low_confidence"))
                or (conf is not None and float(conf) < threshold),
                "source_kinds": st.get("source_kinds", []),
            }
        )
    return {
        "found": True,
        "process_key": row.process_key,
        "version": row.version,
        "status": row.status,
        "min_confidence": (
            round(float(row.min_confidence), 2) if row.min_confidence is not None else None
        ),
        "disposition": await get_disposition(org_id, str(pk)),
        "steps": steps,
    }


async def _search_knowledge(org_id: Any, params: dict[str, Any], run_id: UUID) -> Any:
    """Search validated knowledge chunks (by process_key, by free-text query, or the
    most recent) — each with its provenance so the answer can cite a basis."""
    params = params or {}
    pk = params.get("process_key")
    query = str(params.get("query") or "").strip()
    try:
        limit = int(params.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(limit, _MAX))
    threshold = get_settings().context_grounding_threshold

    if pk:
        chunks = await get_chunks(org_id, str(pk), limit=_MAX)
    else:
        pool = await get_chunks(org_id, limit=200)
        if query:
            toks = [t.lower() for t in re.findall(r"[A-Za-z0-9_-]+", query) if len(t) >= 3]

            def score(c: Any) -> int:
                hay = (c.content + " " + (c.process_key or "")).lower()
                return sum(1 for t in toks if t in hay)

            ranked = sorted(
                ((score(c), i, c) for i, c in enumerate(pool)), key=lambda x: (-x[0], x[1])
            )
            chunks = [c for sc, _, c in ranked if sc > 0]
        else:
            chunks = pool
    chunks = chunks[:limit]
    return {
        "query": query or None,
        "process_key": pk,
        "chunks": [_chunk_view(c, threshold) for c in chunks],
        "count": len(chunks),
    }


async def _findings(org_id: Any, params: dict[str, Any], run_id: UUID) -> Any:
    """Open reconciliation findings (contradiction / drift / gap / stale / violation) —
    what the plane already knows is inconsistent."""
    params = params or {}
    pk = params.get("process_key")
    state = params.get("state") or "open"
    rows = await list_findings(org_id, state=state, process_key=pk, limit=_MAX)
    out = [
        {
            "kind": f.kind,
            "process_key": f.process_key,
            "state": f.state,
            "confidence": round(float(f.confidence), 2) if f.confidence is not None else None,
            "detail": f.detail,
            "evidence_count": len(f.evidence_refs or []),
        }
        for f in rows
    ]
    return {"findings": out, "count": len(out), "state": state}


# --------------------------------------------------------------------------- #
# registry (fqn -> spec). The `kb.*` namespace is reserved for internal tools.
# --------------------------------------------------------------------------- #
INTERNAL_TOOLS: dict[str, ToolSpec] = {
    "kb.list_processes": ToolSpec(
        description=(
            "List the validated processes in this workspace (process_key, version, status, "
            "step_count, min_confidence, disposition). Start here to discover what exists."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_processes,
    ),
    "kb.process": ToolSpec(
        description=(
            "Fetch the current validated process for a process_key: its ordered steps with "
            "per-step confidence and provenance kinds, plus disposition. UNVERIFIED steps are "
            "marked — do not present them as fact."
        ),
        parameters={
            "type": "object",
            "properties": {"process_key": {"type": "string"}},
            "required": ["process_key"],
            "additionalProperties": False,
        },
        handler=_process,
    ),
    "kb.search_knowledge": ToolSpec(
        description=(
            "Search validated knowledge chunks with provenance — by process_key, by free-text "
            "query, or the most recent if neither. Each chunk carries source/origin/confidence/"
            "age and an `unverified` flag. Cite these as evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "process_key": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        handler=_search_knowledge,
    ),
    "kb.findings": ToolSpec(
        description=(
            "List reconciliation findings (contradiction / drift / gap / stale / violation), "
            "default state=open, optionally for one process_key. Use to answer 'what is "
            "inconsistent / stale'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_key": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["open", "acknowledged", "resolved", "dismissed"],
                },
            },
            "additionalProperties": False,
        },
        handler=_findings,
    ),
}
