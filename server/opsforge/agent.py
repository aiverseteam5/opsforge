"""The agent loop — the only place the LLM runs.

Deterministic context assembly, then a bounded tool loop over `gateway.chat()`
exposing ONLY the skill manifest's read-only tools (plus two reserved tools:
`propose_action`, which writes an `actions` row but never executes, and
`submit_report`, which validates the rca_v1 contract). Every step is appended to
`run_events` so the SSE stream shows the investigation live. No framework.
"""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .config import get_settings
from .connectors import (
    ConnectorSession,
    load_connectors_by_kind,
    open_connector,
)
from .db import append_run_event, enqueue_idempotent, scope_to_org, session_factory
from .gateway import ModelGateway, make_assistant_message, make_tool_message
from .graph import neighborhood, render_neighborhood
from .knowledge import KnowledgeChunkRow, freshness_days, get_chunks
from .policy import check_tool_call, resolve_proposal
from .reports import SUBMIT_REPORT_SCHEMA, RcaReport, render_markdown
from .security import redact

RESERVED_PROPOSE = "propose_action"
RESERVED_SUBMIT = "submit_report"
RESERVED_SUBAGENT = "dispatch_subagent"
MAX_SUBAGENT_DEPTH = 2


# --------------------------------------------------------------------------- #
# fqn <-> function name (OpenAI/Anthropic names can't contain '.')
# --------------------------------------------------------------------------- #
def fqn_to_name(fqn: str) -> str:
    return fqn.replace(".", "__")


def name_to_fqn(name: str) -> str:
    return name.replace("__", ".")


# --------------------------------------------------------------------------- #
# ToolBelt: live connector sessions + the schemas exposed to the model
# --------------------------------------------------------------------------- #
class ToolBelt:
    def __init__(self) -> None:
        self.sessions: dict[str, ConnectorSession] = {}
        self.schemas: list[dict[str, Any]] = []
        self.available_fqns: list[str] = []

    async def call(self, fqn: str, params: dict[str, Any], run_id: UUID) -> Any:
        kind = fqn.split(".", 1)[0]
        return await self.sessions[kind].call(fqn, params, run_id=run_id)


async def build_toolbelt(
    manifest: dict[str, Any], org_id: Any, stack: AsyncExitStack
) -> ToolBelt:
    tb = ToolBelt()
    read_only_fqns = {t["tool"] for t in manifest.get("tools", []) or []}
    kinds_needed = {f.split(".", 1)[0] for f in read_only_fqns}
    by_kind = await load_connectors_by_kind(org_id)
    for kind in kinds_needed:
        connector = by_kind.get(kind)
        if connector is None:
            continue
        session = await stack.enter_async_context(open_connector(connector))
        tb.sessions[kind] = session
        for d in await session.list_tool_defs():
            if d["fqn"] not in read_only_fqns:
                continue
            tb.available_fqns.append(d["fqn"])
            tb.schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": fqn_to_name(d["fqn"]),
                        "description": d["description"],
                        "parameters": d["input_schema"],
                    },
                }
            )
    return tb


def _reserved_schemas(manifest: dict[str, Any], depth: int) -> list[dict[str, Any]]:
    proposal_tools = [p["tool"] for p in manifest.get("proposals", []) or []]
    subagents = manifest.get("subagents", []) or []
    schemas = [
        {
            "type": "function",
            "function": {
                "name": RESERVED_SUBMIT,
                "description": (
                    "Finish the investigation by submitting the rca_v1 report. "
                    "Call this exactly once when done."
                ),
                "parameters": SUBMIT_REPORT_SCHEMA,
            },
        }
    ]
    if subagents and depth < MAX_SUBAGENT_DEPTH:
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": RESERVED_SUBAGENT,
                    "description": (
                        "Delegate a sub-question to another skill and get its "
                        f"rca_v1 report back. Allowed skills: {subagents}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_slug": {"type": "string", "enum": subagents},
                            "inputs": {"type": "object"},
                        },
                        "required": ["skill_slug", "inputs"],
                    },
                },
            }
        )
    if proposal_tools:
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": RESERVED_PROPOSE,
                    "description": (
                        "Propose a remediation (NOT executed; queued for human "
                        f"approval). Allowed proposal tools: {proposal_tools}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "params": {"type": "object"},
                            "target_ref": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["tool"],
                    },
                },
            }
        )
    return schemas


# --------------------------------------------------------------------------- #
# Context assembly (deterministic)
# --------------------------------------------------------------------------- #
async def _find_root_keys(org_id: Any, query: str) -> list[str]:
    tokens = [w for w in re.findall(r"[A-Za-z0-9_-]+", query) if len(w) >= 4]
    if not tokens:
        return []
    patterns = [f"%{t}%" for t in tokens]
    async with session_factory().begin() as s:
        await scope_to_org(s, str(org_id))
        rows = (
            await s.execute(
                text(
                    "SELECT natural_key FROM graph_nodes "
                    "WHERE org_id = :org AND ("
                    "  natural_key ILIKE ANY(:pats) OR props->>'name' ILIKE ANY(:pats)"
                    ") ORDER BY (kind = 'service') DESC, last_seen_at DESC LIMIT 5"
                ),
                {"org": str(org_id), "pats": patterns},
            )
        ).all()
    return [r[0] for r in rows]


async def _recent_changes(
    org_id: Any, node_keys: list[str], window_hours: int
) -> list[dict[str, Any]]:
    async with session_factory().begin() as s:
        await scope_to_org(s, str(org_id))
        rows = (
            await s.execute(
                text(
                    "SELECT kind, ref, summary, occurred_at FROM changes "
                    "WHERE org_id = :org AND ("
                    "  occurred_at > now() - make_interval(hours => :w)"
                    "  OR target_keys && :keys"
                    ") ORDER BY occurred_at DESC LIMIT 20"
                ),
                {"org": str(org_id), "w": window_hours, "keys": node_keys or [""]},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


_OPS_KINDS = ("servicenow", "jira", "pagerduty")


async def _incident_context(org_id: Any, inputs: dict[str, Any]) -> str | None:
    """If an ITSM connector is present and an incident_ref is known, translate it
    to the canonical model and render priority/state/SLA for the agent."""
    ref = inputs.get("incident_ref")
    if not ref:
        return None
    from .ops_adapter import read_incident

    by_kind = await load_connectors_by_kind(org_id)
    connector = next((by_kind[k] for k in _OPS_KINDS if k in by_kind), None)
    if connector is None:
        return None
    try:
        inc = await read_incident(connector, str(ref))
    except Exception:  # noqa: BLE001 - context enrichment must never fail the run
        return None
    sla = (
        f"SLA due {inc.sla.deadline} (risk: {inc.sla.breach_risk})"
        if inc.sla and inc.sla.deadline
        else "no SLA"
    )
    return (
        "## Incident (canonical)\n"
        f"- ref: {inc.ref}\n- priority: {inc.priority}\n- state: {inc.state}\n"
        f"- service: {inc.service_ref}\n- assignment: {inc.assignment_group}\n- {sla}"
    )


def _grounding_summary(
    process_key: str, chunks: list[KnowledgeChunkRow]
) -> dict[str, Any]:
    """Summarize how trustworthy the run's knowledge basis is. The grounding
    confidence is the BEST available knowledge (the agent could act on its
    strongest source); if even that is below threshold — or there is no knowledge
    at all for a named process — grounding is low and consequential actions gate."""
    threshold = get_settings().context_grounding_threshold
    confs = [float(c.confidence) if c.confidence is not None else 0.0 for c in chunks]
    best = max(confs) if confs else 0.0
    return {
        "process_key": process_key,
        "chunk_count": len(chunks),
        "grounding_confidence": best,
        "low_confidence": best < threshold,
    }


def _render_knowledge(chunks: list[KnowledgeChunkRow]) -> str:
    """Inject validated knowledge with provenance. High-confidence material reads
    as fact; low-confidence material is explicitly marked UNVERIFIED so the agent
    treats it as a reason to gather more evidence, never a basis to act. With no
    knowledge on file the agent is told it is acting blind, so its context mirrors
    the low grounding the policy layer already enforces."""
    if not chunks:
        return (
            "## Validated process knowledge\n"
            "- (NO process knowledge on file — acting blind; treat any process "
            "claim as UNVERIFIED and gather evidence before any consequential action)"
        )
    threshold = get_settings().context_grounding_threshold

    def line(c: KnowledgeChunkRow, mark: str = "") -> str:
        conf = float(c.confidence) if c.confidence is not None else 0.0
        age = freshness_days(c.observed_at)
        return (
            f"- {mark}{c.content}  "
            f"(source: {c.source_kind}, {age}d old, confidence {conf:.2f})"
        )

    high = [c for c in chunks if (c.confidence or 0.0) >= threshold]
    low = [c for c in chunks if (c.confidence or 0.0) < threshold]
    parts = ["## Validated process knowledge"]
    parts += [line(c) for c in high] or ["- (no high-confidence knowledge)"]
    if low:
        parts.append(
            "### UNVERIFIED — low confidence, do NOT treat as fact "
            "(gather more evidence or escalate before any consequential action):"
        )
        parts += [line(c, mark="[UNVERIFIED] ") for c in low]
    return "\n".join(parts)


async def assemble_context(
    org_id: Any,
    manifest: dict[str, Any],
    instructions: str,
    inputs: dict[str, Any],
    available_fqns: list[str],
    embedding: list[float] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Build the system context and a grounding summary. The summary is None for
    runs with no `process_key` (the kernel's telemetry-grounded path is
    unchanged); when a process is named, it tells the policy layer how trustworthy
    the knowledge basis is so low grounding can force a human gate (M6.5)."""
    raw_query = str(inputs.get("query", ""))
    # Sanitize query before embedding in the system context: strip control
    # characters and cap length so injected alert payloads cannot override
    # system-level instructions (F5 — prompt injection via alert webhook body).
    query = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_query)[:4096]
    parts = [instructions, f"## Trigger\nQuery: {query}"]
    if inputs.get("incident_ref"):
        parts.append(f"Incident ref: {inputs['incident_ref']}")

    # Validated process knowledge for this run, with provenance (M6.5 keystone).
    grounding: dict[str, Any] | None = None
    process_key = inputs.get("process_key")
    if process_key:
        chunks = await get_chunks(org_id, str(process_key))
        grounding = _grounding_summary(str(process_key), chunks)
        # Always render a block so the context mirrors the grounding — an empty
        # process tells the agent it is acting blind.
        parts.append(_render_knowledge(chunks))

    node_keys: list[str] = []
    if manifest.get("context", {}).get("graph", True):
        root_keys = await _find_root_keys(org_id, query)
        if root_keys:
            nb = await neighborhood(root_keys[0], 2, org_id=str(org_id))
            node_keys = [n["natural_key"] for n in nb["nodes"]]
            parts.append("## Operational graph\n" + render_neighborhood(nb))

    window = manifest.get("context", {}).get("change_window_hours", 24)
    changes = await _recent_changes(org_id, node_keys, window)
    if changes:
        rendered = "\n".join(
            f"- [{c['occurred_at']}] {c['kind']} {c['ref']}: {c['summary']}"
            for c in changes
        )
        parts.append("## Recent changes (most recent first)\n" + rendered)

    # Canonical incident (translated from whatever ITSM tool is connected).
    incident_block = await _incident_context(org_id, inputs)
    if incident_block:
        parts.append(incident_block)

    # Similar past patterns — semantic search over codified run embeddings.
    similar_k = manifest.get("context", {}).get("similar_patterns", 0)
    if similar_k > 0 and embedding is not None:
        try:
            from .knowledge import _vector_literal  # reuse pgvector text helper
            vec_text = _vector_literal(embedding)
            async with session_factory().begin() as s:
                await scope_to_org(s, str(org_id))
                pat_rows = (
                    await s.execute(
                        text(
                            "SELECT summary, resolution FROM patterns "
                            "WHERE org_id = :org AND embedding IS NOT NULL "
                            "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :k"
                        ),
                        {"org": str(org_id), "vec": vec_text, "k": similar_k},
                    )
                ).all()
            if pat_rows:
                rendered_pats = "\n".join(
                    f"- {r.summary}: {r.resolution}" for r in pat_rows
                )
                parts.append(f"## Similar past patterns\n{rendered_pats}")
        except Exception:  # noqa: BLE001 - pattern context is optional; degrade gracefully
            pass

    parts.append(
        "## Your read-only tools\n"
        + "\n".join(f"- {f}" for f in available_fqns)
        + f"\n\nWhen finished, call `{RESERVED_SUBMIT}`."
    )
    return "\n\n".join(parts), grounding


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
async def _load_run(run_id: UUID, org_id: str = "") -> dict[str, Any] | None:
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, skill_id, status, trigger, model, parent_run_id "
                    "FROM runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).first()
    return dict(row._mapping) if row else None


async def _run_status(run_id: UUID, org_id: str = "") -> str | None:
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        return (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one_or_none()


async def _set_running(run_id: UUID, model: str, org_id: str = "") -> None:
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        await s.execute(
            text(
                "UPDATE runs SET status='running', model=:m, started_at=now() "
                "WHERE id=:id"
            ),
            {"m": model, "id": run_id},
        )


async def _finalize(
    run_id: UUID,
    status: str,
    report: RcaReport | None,
    tokens_in: int,
    tokens_out: int,
    org_id: str = "",
    parent_run_id: UUID | None = None,
) -> None:
    import json

    md = render_markdown(report) if report else None
    rjson = json.dumps(report.model_dump()) if report else None
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        await s.execute(
            text(
                "UPDATE runs SET status=:st, report_md=:md, "
                "report_json=CAST(:rj AS jsonb), tokens_in=:ti, tokens_out=:to, "
                "finished_at=now() WHERE id=:id"
            ),
            {
                "st": status,
                "md": md,
                "rj": rjson,
                "ti": tokens_in,
                "to": tokens_out,
                "id": run_id,
            },
        )
        # Quality gate: only codify top-level, completed, high-confidence runs.
        if (
            status == "done"
            and parent_run_id is None
            and report is not None
            and report.confidence != "low"
            and org_id
        ):
            await enqueue_idempotent(
                s,
                kind="codify_skill",
                payload={"run_id": str(run_id), "org_id": org_id},
                org_id=org_id,
            )


async def _insert_proposal(
    run: dict[str, Any], tool_fqn: str, params: dict, target_ref: str | None, trace: dict
) -> str:
    import json

    async with session_factory().begin() as s:
        await scope_to_org(s, str(run["org_id"]))
        action_id = (
            await s.execute(
                text(
                    "INSERT INTO actions (org_id, run_id, skill_id, action_class, "
                    "tool, params, target_ref, state, policy_trace) "
                    "VALUES (:org,:run,:skill,:cls,:tool,CAST(:params AS jsonb),"
                    ":target,:state,CAST(:trace AS jsonb)) RETURNING id"
                ),
                {
                    "org": str(run["org_id"]),
                    "run": str(run["id"]),
                    "skill": run["skill_id"],
                    "cls": trace.get("action_class") or "reversible",
                    "tool": tool_fqn,
                    "params": json.dumps(params or {}),
                    "target": target_ref,
                    "state": trace["state"],
                    "trace": json.dumps(trace),
                },
            )
        ).scalar_one()
    return str(action_id)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
async def run_agent(
    run_id: UUID,
    skill: dict[str, Any],
    gateway: ModelGateway,
    model: str | None = None,
    depth: int = 0,
    org_id: str = "",
) -> RcaReport:
    run = await _load_run(run_id, org_id=org_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    org_id = str(run["org_id"])
    manifest = skill["manifest"]
    instructions = skill.get("instructions") or ""
    inputs = (run.get("trigger") or {}).get("payload") or {}
    chosen_model = model or run.get("model") or skill.get("model") or _default_model()

    await _set_running(run_id, chosen_model, org_id=org_id)

    max_tool_calls = manifest.get("policy", {}).get("max_tool_calls", 25)
    tokens_in = tokens_out = 0
    report: RcaReport | None = None

    async with AsyncExitStack() as stack:
        toolbelt = await build_toolbelt(manifest, org_id, stack)

        # Pre-compute embedding for similar_patterns lookup (HTTP call, outside any DB tx).
        query_embedding: list[float] | None = None
        similar_k = manifest.get("context", {}).get("similar_patterns", 0)
        if similar_k > 0:
            try:
                vecs = await gateway.embedding([str(inputs.get("query", ""))], _default_model_embedding())  # noqa: E501
                query_embedding = vecs[0] if vecs else None
            except Exception:  # noqa: BLE001 - embedding failure must not block the run
                pass

        context, grounding = await assemble_context(
            org_id, manifest, instructions, inputs, toolbelt.available_fqns,
            embedding=query_embedding,
        )
        await append_run_event(
            run_id,
            org_id,
            "thought",
            {"context_chars": len(context), "grounding": grounding},
        )

        schemas = toolbelt.schemas + _reserved_schemas(manifest, depth)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": context},
            {"role": "user", "content": str(inputs.get("query", ""))},
        ]

        tool_calls_used = 0
        # A few extra steps beyond the tool budget for reasoning/report turns.
        for _ in range(max_tool_calls + 5):
            if (await _run_status(run_id, org_id=org_id)) == "cancelled":
                await _finalize(run_id, "cancelled", None, tokens_in, tokens_out,
                                org_id=org_id, parent_run_id=run.get("parent_run_id"))
                return _incomplete_report("run cancelled")

            result = await gateway.chat(messages, schemas, chosen_model)
            tokens_in += result.usage.get("prompt_tokens", 0)
            tokens_out += result.usage.get("completion_tokens", 0)
            if result.text:
                await append_run_event(
                    run_id, org_id, "thought", {"text": redact(result.text)}
                )
            messages.append(make_assistant_message(result))

            if not result.tool_calls:
                break  # model stopped without submitting; we force a report below

            for tc in result.tool_calls:
                if tc.name == RESERVED_SUBMIT:
                    report, ack = _parse_report(tc.arguments)
                    if report is not None:
                        await append_run_event(
                            run_id, org_id, "report", report.model_dump()
                        )
                    messages.append(make_tool_message(tc, ack))
                    continue

                if tc.name == RESERVED_PROPOSE:
                    out = await _handle_propose(
                        run, manifest, skill, tc.arguments, grounding
                    )
                    await append_run_event(run_id, org_id, "proposal", redact(out))
                    messages.append(make_tool_message(tc, out))
                    continue

                if tc.name == RESERVED_SUBAGENT:
                    out = await _handle_subagent(
                        run, manifest, gateway, chosen_model, depth, tc.arguments
                    )
                    await append_run_event(run_id, org_id, "thought", {"subagent": redact(out)})
                    messages.append(make_tool_message(tc, out))
                    continue

                # A connector tool call.
                fqn = name_to_fqn(tc.name)
                trace = check_tool_call(manifest, fqn, tc.arguments)
                if not trace["allowed"]:
                    await append_run_event(
                        run_id, org_id, "error", {"tool": fqn, "policy": trace}
                    )
                    messages.append(make_tool_message(tc, {"error": trace["reason"]}))
                    continue
                if tool_calls_used >= max_tool_calls:
                    messages.append(
                        make_tool_message(
                            tc,
                            {"error": "tool-call budget exhausted; submit_report now"},
                        )
                    )
                    continue
                try:
                    output = await toolbelt.call(fqn, tc.arguments or {}, run_id)
                    tool_calls_used += 1
                except Exception as exc:  # noqa: BLE001
                    output = {"error": str(redact(str(exc)))}
                    await append_run_event(run_id, org_id, "error", {"tool": fqn})
                messages.append(make_tool_message(tc, output))

            if report is not None:
                break

        if report is None:
            report = _incomplete_report(
                "The agent did not submit a report within its budget."
            )
            await append_run_event(run_id, org_id, "report", report.model_dump())

        await _finalize(run_id, "done", report, tokens_in, tokens_out,
                        org_id=org_id, parent_run_id=run.get("parent_run_id"))
        return report


def _default_model() -> str:
    from .config import get_settings

    return get_settings().model


def _default_model_embedding() -> str:
    from .config import get_settings

    return get_settings().embedding_model


def _parse_report(args: dict[str, Any]) -> tuple[RcaReport | None, dict[str, Any]]:
    try:
        report = RcaReport.model_validate(args)
        return report, {"status": "accepted"}
    except Exception as exc:  # noqa: BLE001 - feed the error back to the model
        return None, {"status": "rejected", "error": str(exc)}


def _incomplete_report(reason: str) -> RcaReport:
    return RcaReport(
        hypothesis="Investigation incomplete.",
        confidence="low",
        evidence=[],
        missing_evidence=reason,
    )


async def _handle_subagent(
    run: dict[str, Any],
    manifest: dict[str, Any],
    gateway: ModelGateway,
    model: str,
    depth: int,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Structured delegation: run a child skill and return its rca_v1 report.

    Delegation is a tree of contracts (parent_run_id set), not free-form chatter:
    the only thing returned is the child's report. Bounded by manifest allowlist
    and MAX_SUBAGENT_DEPTH.
    """
    from .skills import get_skill

    target = args.get("skill_slug", "")
    if target not in (manifest.get("subagents", []) or []):
        return {"error": f"skill {target!r} not in this skill's subagents allowlist"}
    if depth + 1 >= MAX_SUBAGENT_DEPTH + 1:
        return {"error": "max sub-agent depth reached"}
    child_skill = await get_skill(target)
    if child_skill is None or not child_skill["enabled"]:
        return {"error": f"sub-agent skill {target!r} not installed"}

    child_run_id = await _create_child_run(run, child_skill["id"], args.get("inputs") or {})
    report = await run_agent(
        child_run_id, child_skill, gateway, model=model, depth=depth + 1,
        org_id=str(run["org_id"]),
    )
    return {
        "child_run_id": str(child_run_id),
        "report": report.model_dump(),
    }


async def _create_child_run(
    parent: dict[str, Any], skill_id: Any, inputs: dict[str, Any]
) -> UUID:
    import json

    trigger = {"kind": "subagent", "payload": inputs, "surface": None}
    async with session_factory().begin() as s:
        await scope_to_org(s, str(parent["org_id"]))
        run_id = (
            await s.execute(
                text(
                    "INSERT INTO runs (org_id, skill_id, status, parent_run_id, trigger) "
                    "VALUES (:org,:skill,'queued',:parent,CAST(:trigger AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "org": str(parent["org_id"]),
                    "skill": skill_id,
                    "parent": str(parent["id"]),
                    "trigger": json.dumps(trigger),
                },
            )
        ).scalar_one()
    return run_id


async def _handle_propose(
    run: dict[str, Any],
    manifest: dict[str, Any],
    skill: dict[str, Any],
    args: dict[str, Any],
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_fqn = args.get("tool", "")
    trace = resolve_proposal(
        manifest, tool_fqn, skill.get("trust_overrides"), grounding=grounding
    )
    if not trace["allowed"]:
        return {"error": trace["reason"], "policy": trace}
    action_id = await _insert_proposal(
        run, tool_fqn, args.get("params") or {}, args.get("target_ref"), trace
    )
    # A graduated (auto_with_notify) tool is already 'approved' — hand it to the
    # executor immediately (with the audit trail standing in for "notify").
    if trace.get("auto_execute"):
        from .db import enqueue

        async with session_factory().begin() as s:
            await scope_to_org(s, str(run["org_id"]))
            await enqueue(
                s, kind="execute_action", payload={"action_id": action_id},
                org_id=str(run["org_id"]),
            )
    return {"action_id": action_id, "state": trace["state"], "tool": tool_fqn}
