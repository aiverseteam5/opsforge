"""G2 — investigate freely: the internal read-only tools (kb.*) over the validated
knowledge plane.

Proves: the handlers return evidence + provenance; low-confidence material is marked
`unverified` (M6.5 honesty); they are workspace-isolated (RLS); and end-to-end the agent
can call them during a run, they stream as tool_call/tool_result, they NEVER gate (reads
are class read_only), and no action row is ever created on the read path.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge import knowledge_tools as kt
from opsforge.agent import run_agent
from opsforge.db import scope_to_org, session_factory
from opsforge.findings import emit_finding
from opsforge.knowledge import ProvenanceEnvelope, set_reconciliation, store_chunk
from opsforge.policy import check_tool_call

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)
RID = uuid.uuid4()  # a fixed run id for direct handler calls (handlers ignore it)


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
async def _seed_chunk(org, pk, kind, content, confidence):
    cid = await store_chunk(
        org_id=org,
        content=content,
        envelope=ProvenanceEnvelope(
            source_kind=kind, source_ref=f"x://{content}", observed_at=AS_OF, ingested_at=AS_OF
        ),
        process_key=pk,
    )
    await set_reconciliation(
        org, cid, confidence=confidence, corroborated_by=0, contradicted_by=0,
        reconciliation_id=uuid.uuid4(),
    )
    return cid


async def _seed_process(org, pk, steps, min_conf):
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(
            text(
                "INSERT INTO validated_processes (org_id, process_key, version, steps, "
                "min_confidence) VALUES (:o,:pk,1,CAST(:steps AS jsonb),:mc)"
            ),
            {"o": str(org), "pk": pk, "steps": json.dumps(steps), "mc": min_conf},
        )


async def _cleanup(org):
    # run_events + audit_log are append-only (DB trigger rejects DELETE) — leave them;
    # the org is random per test, so the rows never collide with another test.
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in ("actions", "findings", "validated_processes", "knowledge_chunks", "runs"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


# --------------------------------------------------------------------------- #
# handlers: evidence + provenance + honesty
# --------------------------------------------------------------------------- #
async def test_search_knowledge_returns_provenance_and_marks_unverified():
    org, pk = str(uuid.uuid4()), "rollback"
    try:
        await _seed_chunk(org, pk, "behaviour", "rollback drains the node first", 0.9)
        await _seed_chunk(org, pk, "document", "stale: rollback reboots the host", 0.3)

        by_key = await kt._search_knowledge(org, {"process_key": pk}, RID)
        assert by_key["count"] == 2
        marks = {c["content"]: c["unverified"] for c in by_key["chunks"]}
        assert marks["rollback drains the node first"] is False   # 0.9 — fact
        assert marks["stale: rollback reboots the host"] is True  # 0.3 — UNVERIFIED
        # provenance is carried, not flattened away
        c0 = next(c for c in by_key["chunks"] if c["confidence"] == 0.9)
        assert c0["source_kind"] == "behaviour" and c0["source_ref"].startswith("x://")

        # free-text query path finds the relevant chunk
        q = await kt._search_knowledge(org, {"query": "drains node"}, RID)
        assert any("drains the node" in c["content"] for c in q["chunks"])
    finally:
        await _cleanup(org)


async def test_process_returns_steps_with_per_step_unverified():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_process(
            org, pk,
            steps=[
                {"index": 0, "kind": "step", "text": "freeze the queue",
                 "confidence": 0.92, "low_confidence": False, "source_kinds": ["behaviour"]},
                {"index": 1, "kind": "step", "text": "old guess nobody trusts",
                 "confidence": 0.2, "low_confidence": True, "source_kinds": ["document"]},
            ],
            min_conf=0.2,
        )
        out = await kt._process(org, {"process_key": pk}, RID)
        assert out["found"] is True and out["version"] == 1
        assert [s["unverified"] for s in out["steps"]] == [False, True]
        assert out["min_confidence"] == 0.2

        # absent process: honest, not a fabricated answer
        missing = await kt._process(org, {"process_key": "does-not-exist"}, RID)
        assert missing["found"] is False and "UNVERIFIED" in missing["note"]
    finally:
        await _cleanup(org)


async def test_findings_and_list_processes():
    org, pk = str(uuid.uuid4()), "incident"
    try:
        await _seed_process(org, pk, min_conf=0.8, steps=[
            {"index": 0, "kind": "step", "text": "page on-call", "confidence": 0.8,
             "low_confidence": False, "source_kinds": ["document"]}])
        await emit_finding(org_id=org, kind="contradiction", process_key=pk,
                           detail={"a": "x", "b": "y"}, evidence_refs=[uuid.uuid4()])

        procs = await kt._list_processes(org, {}, RID)
        assert any(p["process_key"] == pk for p in procs["processes"])

        f = await kt._findings(org, {"process_key": pk}, RID)
        assert f["count"] == 1 and f["findings"][0]["kind"] == "contradiction"
        assert f["findings"][0]["evidence_count"] == 1
    finally:
        await _cleanup(org)


async def test_kb_tools_are_workspace_isolated():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await _seed_chunk(org_b, "secret", "behaviour", "B-only secret runbook", 0.9)
        await _seed_process(org_b, "secret", steps=[
            {"index": 0, "kind": "step", "text": "B step", "confidence": 0.9,
             "low_confidence": False, "source_kinds": ["behaviour"]}], min_conf=0.9)
        await emit_finding(org_id=org_b, kind="gap", process_key="secret")

        # A sees NOTHING of B's knowledge through any kb.* tool
        assert (await kt._search_knowledge(org_a, {"process_key": "secret"}, RID))["count"] == 0
        assert (await kt._process(org_a, {"process_key": "secret"}, RID))["found"] is False
        assert (await kt._list_processes(org_a, {}, RID))["count"] == 0
        assert (await kt._findings(org_a, {}, RID))["count"] == 0
        # B sees its own
        assert (await kt._search_knowledge(org_b, {"process_key": "secret"}, RID))["count"] == 1
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)


# --------------------------------------------------------------------------- #
# policy invariant: a read_only kb tool is callable during a run; it never gates
# --------------------------------------------------------------------------- #
_MANIFEST = {
    "context": {"graph": False},
    "tools": [
        {"tool": "kb.list_processes", "class": "read_only"},
        {"tool": "kb.process", "class": "read_only"},
        {"tool": "kb.search_knowledge", "class": "read_only"},
        {"tool": "kb.findings", "class": "read_only"},
    ],
    "proposals": [],
    "policy": {"max_tool_calls": 6},
}


def test_check_tool_call_allows_read_only_kb_tools():
    for fqn in ("kb.list_processes", "kb.process", "kb.search_knowledge", "kb.findings"):
        trace = check_tool_call(_MANIFEST, fqn)
        assert trace["allowed"] is True
        assert trace["action_class"] == "read_only"


# --------------------------------------------------------------------------- #
# end-to-end: the agent investigates via a kb tool, it streams, it never gates,
# and the read path creates NO action row.
# --------------------------------------------------------------------------- #
class _InvestigateGateway:
    """Calls kb.search_knowledge once, then submits a grounded report."""

    def __init__(self) -> None:
        self._searched = False

    async def chat(self, messages, tools, model):
        from opsforge.gateway import ChatResult, ToolCall

        names = {t["function"]["name"] for t in (tools or [])}
        assert "kb__search_knowledge" in names  # the read tool is exposed to the model
        if not self._searched:
            self._searched = True
            return ChatResult(
                text="investigating",
                tool_calls=[ToolCall("t1", "kb__search_knowledge", {"query": "rollback"})],
            )
        return ChatResult(
            text="answering",
            tool_calls=[ToolCall("s", "submit_report", {
                "hypothesis": "Rollback drains the node first.",
                "confidence": "medium",
                "evidence": [{"claim": "rollback drains the node first",
                              "raw_ref": "x://rollback drains the node first"}],
            })],
        )

    async def embedding(self, texts, model):
        return [[0.0] * 1536 for _ in texts]


async def test_agent_investigates_with_read_tool_and_never_gates():
    org, pk = str(uuid.uuid4()), "rollback"
    try:
        await _seed_chunk(org, pk, "behaviour", "rollback drains the node first", 0.9)
        skill = {"id": None, "manifest": _MANIFEST, "instructions": "investigate",
                 "trust_overrides": {}, "model": None}
        trigger = {"kind": "chat", "payload": {"query": "what is the rollback process?"}}
        async with session_factory().begin() as s:
            run_id = (await s.execute(
                text("INSERT INTO runs (org_id, status, trigger) "
                     "VALUES (:o,'queued',CAST(:t AS jsonb)) RETURNING id"),
                {"o": org, "t": json.dumps(trigger)})).scalar_one()

        report = await run_agent(run_id, skill, _InvestigateGateway())
        assert report.hypothesis.startswith("Rollback")
        assert report.evidence  # answered WITH evidence

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            events = (await s.execute(
                text("SELECT kind, payload FROM run_events WHERE run_id=:r ORDER BY seq"),
                {"r": str(run_id)})).all()
            n_actions = (await s.execute(
                text("SELECT count(*) FROM actions WHERE run_id=:r"),
                {"r": str(run_id)})).scalar_one()

        kinds = [e.kind for e in events]
        assert "tool_call" in kinds and "tool_result" in kinds  # the read streamed
        # the read tool was allowed (executed), never gated/blocked
        tr = next(e for e in events if e.kind == "tool_result")
        assert tr.payload["tool"] == "kb.search_knowledge" and tr.payload["is_error"] is False
        assert "error" not in kinds
        # READ PATH: nothing was proposed or executed
        assert n_actions == 0
    finally:
        await _cleanup(org)
