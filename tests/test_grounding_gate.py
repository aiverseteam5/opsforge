"""M6.5 keystone — the low-grounding gate.

assemble_context pulls the run's process knowledge with provenance, marks
low-confidence material UNVERIFIED, and summarizes grounding; the policy layer
then forces a consequential action proposed on low-confidence grounding to
awaiting_approval regardless of the trust line, recording the grounding in the
policy_trace. The headline acceptance test: an agent given deliberately-stale
context does NOT auto-execute a graduated reversible action — it gates to a human.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge.agent import assemble_context, run_agent
from opsforge.gateway import ChatResult, ToolCall
from opsforge.knowledge import ProvenanceEnvelope, set_reconciliation, store_chunk
from opsforge.policy import resolve_proposal

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)
_PROPOSAL = {"proposals": [{"tool": "k.restart", "class": "reversible"}]}


# --------------------------------------------------------------------------- #
# the policy gate (pure, no DB)
# --------------------------------------------------------------------------- #
def test_low_grounding_gates_an_auto_allowed_action():
    overrides = {"k.restart": "auto_with_notify"}  # graduated → would auto_allow
    high = resolve_proposal(_PROPOSAL, "k.restart", overrides, grounding={"low_confidence": False})
    assert high["state"] == "approved" and high["auto_execute"] is True

    low = resolve_proposal(
        _PROPOSAL, "k.restart", overrides,
        grounding={"low_confidence": True, "grounding_confidence": 0.3},
    )
    assert low["state"] == "awaiting_approval"
    assert low["auto_execute"] is False
    assert "low_grounding_gate" in low["rules"]
    assert low["grounding"]["grounding_confidence"] == 0.3  # recorded in the trace
    assert "gated:low_grounding" in low["reason"]


def test_low_grounding_does_not_affect_a_denied_proposal():
    # a tool not declared as a proposal is denied; grounding must not change that,
    # and the reason must not claim a gate that did not fire
    trace = resolve_proposal(_PROPOSAL, "k.unknown", None, grounding={"low_confidence": True})
    assert trace["state"] == "denied"
    assert trace["allowed"] is False
    assert "low_grounding_gate" not in trace.get("rules", [])


def test_low_grounding_cannot_upgrade_a_held_action():
    # a non-graduated reversible is already awaiting_approval; grounding can only
    # keep it there, never relax it
    held = resolve_proposal(_PROPOSAL, "k.restart", None, grounding={"low_confidence": False})
    assert held["state"] == "awaiting_approval"
    assert "low_grounding_gate" not in held["rules"]


# --------------------------------------------------------------------------- #
# assemble_context grounding + rendering (DB)
# --------------------------------------------------------------------------- #
async def _seed(org, pk, kind, content, confidence):
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


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in ("actions", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


async def test_assemble_context_summarizes_grounding_and_marks_unverified():
    org, pk = str(uuid.uuid4()), "deploy"
    manifest = {"context": {"graph": False}}
    try:
        # only low-confidence knowledge exists for this process
        await _seed(org, pk, "document", "stale runbook step", 0.3)
        ctx, grounding = await assemble_context(
            org, manifest, "instructions", {"query": "q", "process_key": pk}, []
        )
        assert grounding is not None
        assert grounding["low_confidence"] is True
        assert grounding["grounding_confidence"] == pytest.approx(0.3)
        assert "UNVERIFIED" in ctx
        assert "stale runbook step" in ctx

        # a high-confidence chunk lifts OVERALL grounding above the bar, but the
        # low chunk is still marked UNVERIFIED (partition is per-chunk, not aggregate)
        await _seed(org, pk, "behaviour", "what we actually do", 0.9)
        ctx2, g2 = await assemble_context(
            org, manifest, "instructions", {"query": "q", "process_key": pk}, []
        )
        assert g2["low_confidence"] is False
        assert g2["grounding_confidence"] == pytest.approx(0.9)
        assert "what we actually do" in ctx2  # high-confidence reads as fact
        assert "UNVERIFIED" in ctx2  # the low chunk is still flagged
        assert "stale runbook step" in ctx2
    finally:
        await _cleanup(org)


async def test_named_process_with_no_chunks_acts_blind():
    org, pk = str(uuid.uuid4()), "empty-process"
    ctx, grounding = await assemble_context(
        org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
    )
    assert grounding is not None
    assert grounding["chunk_count"] == 0
    assert grounding["grounding_confidence"] == 0.0
    assert grounding["low_confidence"] is True
    assert "acting blind" in ctx  # the context mirrors the gate, not silent trust


async def test_grounding_threshold_is_strict():
    org = str(uuid.uuid4())
    manifest = {"context": {"graph": False}}
    try:
        # exactly at the bar → NOT low (strict <), renders as fact
        await _seed(org, "at", "document", "exactly at the bar", 0.5)
        ctx, g = await assemble_context(
            org, manifest, "i", {"query": "q", "process_key": "at"}, []
        )
        assert g["low_confidence"] is False
        assert g["grounding_confidence"] == pytest.approx(0.5)
        assert "UNVERIFIED" not in ctx and "exactly at the bar" in ctx

        # just below → low
        await _seed(org, "below", "document", "just below", 0.49)
        _ctx2, g2 = await assemble_context(
            org, manifest, "i", {"query": "q", "process_key": "below"}, []
        )
        assert g2["low_confidence"] is True
    finally:
        await _cleanup(org)


async def test_no_process_key_means_no_grounding():
    # the kernel's telemetry-grounded path is unchanged when no process is named
    _ctx, grounding = await assemble_context(
        str(uuid.uuid4()), {"context": {"graph": False}}, "i", {"query": "q"}, []
    )
    assert grounding is None


# --------------------------------------------------------------------------- #
# the acceptance test: agent + low grounding → gate, not auto-execute
# --------------------------------------------------------------------------- #
class _ProposeGateway:
    """Proposes the graduated tool once, then submits a report."""

    def __init__(self, tool):
        self._tool = tool
        self._proposed = False

    async def chat(self, messages, tools, model):
        names = {t["function"]["name"] for t in (tools or [])}
        if "propose_action" in names and not self._proposed:
            self._proposed = True
            return ChatResult(
                text="Proposing the fix.",
                tool_calls=[
                    ToolCall("p", "propose_action",
                             {"tool": self._tool, "params": {}, "target_ref": "svc://x"})
                ],
            )
        return ChatResult(
            text="Reporting.",
            tool_calls=[
                ToolCall("s", "submit_report",
                         {"hypothesis": "h", "confidence": "medium", "evidence": []})
            ],
        )

    async def embedding(self, texts, model):
        return [[0.0] * 1536 for _ in texts]


async def _run_with_grounding(org, pk):
    """Create a run for `org` whose trigger names `pk`, run the proposing agent,
    and return the resulting action row (state + policy_trace)."""
    from opsforge.db import scope_to_org, session_factory

    skill = {
        "id": None,
        "manifest": {**_PROPOSAL, "context": {"graph": False}, "tools": []},
        "instructions": "",
        "trust_overrides": {"k.restart": "auto_with_notify"},  # graduated
        "model": None,
    }
    trigger = {"kind": "manual", "payload": {"query": "fix it", "process_key": pk}}
    async with session_factory().begin() as s:
        run_id = (
            await s.execute(
                text(
                    "INSERT INTO runs (org_id, status, trigger) "
                    "VALUES (:o,'queued',CAST(:t AS jsonb)) RETURNING id"
                ),
                {"o": org, "t": json.dumps(trigger)},
            )
        ).scalar_one()
    await run_agent(run_id, skill, _ProposeGateway("k.restart"))
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        action = (
            await s.execute(
                text("SELECT state, policy_trace FROM actions WHERE run_id=:r"),
                {"r": str(run_id)},
            )
        ).one()
    return str(run_id), action


async def test_agent_gates_consequential_action_on_low_grounding():
    org, pk = str(uuid.uuid4()), "stale-process"
    try:
        await _seed(org, pk, "document", "a 2023 runbook nobody trusts", 0.3)  # low only
        run_id, action = await _run_with_grounding(org, pk)

        # the graduated tool would normally auto-execute; low grounding gates it
        assert action.state == "awaiting_approval"
        trace = action.policy_trace
        assert trace["grounding"]["low_confidence"] is True
        assert "low_grounding_gate" in trace["rules"]
        assert trace["grounding"]["grounding_confidence"] == pytest.approx(0.3)

        from opsforge.db import scope_to_org, session_factory

        # the grounding is also exposed to the live run stream (a 'thought' event)
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            ev = (
                await s.execute(
                    text(
                        "SELECT payload FROM run_events WHERE run_id=:r AND kind='thought' "
                        "AND payload ? 'grounding' ORDER BY seq LIMIT 1"
                    ),
                    {"r": run_id},
                )
            ).scalar_one()
        assert ev["grounding"]["low_confidence"] is True

        # and no execute job was enqueued for it
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            n = (
                await s.execute(
                    text("SELECT count(*) FROM jobs WHERE org_id=:o AND kind='execute_action'"),
                    {"o": org},
                )
            ).scalar_one()
        assert n == 0
    finally:
        await _cleanup(org)


async def test_agent_auto_executes_on_high_grounding():
    org, pk = str(uuid.uuid4()), "trusted-process"
    try:
        await _seed(org, pk, "behaviour", "what we actually do, well-corroborated", 0.9)
        _run_id, action = await _run_with_grounding(org, pk)

        # high grounding + graduated tool → auto-approved, gate not applied
        assert action.state == "approved"
        assert action.policy_trace["grounding"]["low_confidence"] is False
        assert "low_grounding_gate" not in action.policy_trace["rules"]
        assert "gated:low_grounding" not in action.policy_trace["reason"]
    finally:
        await _cleanup(org)
