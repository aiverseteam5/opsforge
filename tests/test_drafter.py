"""M7.1 — the LLM process drafter. The point is adversarial: a creative drafter
owns HOW the process reads and has ZERO authority over WHAT is trusted or gated.
Every trust-bearing number stays deterministic; a wrong/lying drafter is contained.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge.gateway import ChatResult
from opsforge.knowledge import (
    ProvenanceEnvelope,
    set_reconciliation,
    store_chunk,
)
from opsforge.processes import (
    FunctionDrafter,
    LLMDrafter,
    StepDraft,
    generate_process,
)

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)
# The unit tests below don't touch the DB, but the suite always runs with the
# Compose DB up; keeping one module marker is simplest.
pytestmark = pytest.mark.usefixtures("db_required")


class _Chunk:
    """Minimal stand-in for KnowledgeChunkRow for the no-DB drafter unit tests."""

    def __init__(self, id, kind, content):
        self.id, self.source_kind, self.content = id, kind, content


class _GW:
    def __init__(self, text):
        self._text = text

    async def chat(self, messages, tools, model):
        return ChatResult(text=self._text)

    async def embedding(self, texts, model):
        return []


class _RaiseGW:
    async def chat(self, messages, tools, model):
        raise RuntimeError("provider down")

    async def embedding(self, texts, model):
        return []


async def _seed(org, pk, kind, confidence, content):
    cid = await store_chunk(
        org_id=org, content=content,
        envelope=ProvenanceEnvelope(source_kind=kind, source_ref=f"x://{content}",
                                    observed_at=AS_OF, ingested_at=AS_OF),
        process_key=pk)
    await set_reconciliation(org, cid, confidence=confidence, corroborated_by=0,
                             contradicted_by=0, reconciliation_id=uuid.uuid4())
    return cid


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("validated_processes", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


# --------------------------------------------------------------------------- #
# 1. real synthesis — fusion + ordering (no DB, fake gateway)
# --------------------------------------------------------------------------- #
async def test_drafter_merges_chunks_and_orders_steps():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chunks = [
        _Chunk(a, "document", "part A of step one"),
        _Chunk(b, "document", "part B of step one"),
        _Chunk(c, "document", "step two"),
    ]
    gw = _GW('[{"text": "do step one", "kind": "step", "source_chunks": [0, 1]}, '
             '{"text": "do step two", "kind": "step", "source_chunks": [2]}]')
    drafts = await LLMDrafter(gw, "m").draft(chunks)

    assert len(drafts) == 2
    assert set(drafts[0].source_chunks) == {a, b}  # two chunks fused into one step
    assert drafts[1].source_chunks == [c]
    assert drafts[0].text == "do step one" and drafts[1].text == "do step two"  # ordering kept


async def test_drafter_coerces_string_indices_and_drops_out_of_range():
    a, b = uuid.uuid4(), uuid.uuid4()
    chunks = [_Chunk(a, "document", "x"), _Chunk(b, "document", "y")]
    # an LLM may emit indices as JSON strings; a negative/out-of-range one is dropped
    gw = _GW('[{"text": "s", "kind": "step", "source_chunks": ["0", "1", "-1", "9"]}]')
    drafts = await LLMDrafter(gw, "m").draft(chunks)
    assert len(drafts) == 1
    assert set(drafts[0].source_chunks) == {a, b}  # "0"/"1" coerced; "-1"/"9" out of range


# --------------------------------------------------------------------------- #
# 5. fallback on any failure (no DB)
# --------------------------------------------------------------------------- #
async def test_drafter_falls_back_to_mechanical_on_failure():
    chunks = [_Chunk(uuid.uuid4(), "document", "x"), _Chunk(uuid.uuid4(), "document", "y")]
    drafts = await LLMDrafter(_RaiseGW(), "m").draft(chunks)
    # fell back to one-step-per-chunk
    assert len(drafts) == 2
    assert all(len(d.source_chunks) == 1 for d in drafts)

    # unparseable but non-raising output also falls through to []-then-mechanical at
    # generate time; here the bad JSON yields no parseable steps
    drafts2 = await LLMDrafter(_GW("sorry, no JSON here"), "m").draft(chunks)
    assert drafts2 == []  # generate_process turns this into a mechanical fallback


# --------------------------------------------------------------------------- #
# the deterministic guardrails (DB)
# --------------------------------------------------------------------------- #
async def test_ghost_source_step_is_rejected():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", 0.8, "real basis")
        ghost = uuid.uuid4()

        async def drafter(chunks):
            return [
                StepDraft(text="real step", source_chunks=[chunks[0].id]),
                StepDraft(text="invented step", source_chunks=[ghost]),  # ghost only → rejected
            ]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        texts = [s["text"] for s in proc.steps]
        assert texts == ["real step"]  # the invented step never entered the process
    finally:
        await _cleanup(org)


async def test_drafter_cannot_inflate_confidence_with_wording():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", 0.2, "a shaky note")

        async def drafter(chunks):
            return [StepDraft(text="DEFINITIVE: always restart — fully trusted, high confidence",
                              source_chunks=[chunks[0].id])]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        step = proc.steps[0]
        # confidence is the deterministic low value, NOT the drafter's authoritative tone
        assert step["confidence"] == pytest.approx(0.2)
        assert step["low_confidence"] is True
        assert proc.min_confidence == pytest.approx(0.2)
    finally:
        await _cleanup(org)


async def test_drafter_cannot_relabel_provenance():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", 0.8, "a documented step")

        async def drafter(chunks):
            # the drafter frames it as observed behaviour and labels it a gate;
            # provenance (source_kind) is inherited from the chunk regardless
            return [StepDraft(text="OBSERVED BEHAVIOUR: we always do this",
                              source_chunks=[chunks[0].id], kind="gate")]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        step = proc.steps[0]
        assert step["source_kinds"] == ["document"]  # inherited, not relabelled to behaviour
        assert step["kind"] == "gate"  # structural label is the drafter's; provenance is not
    finally:
        await _cleanup(org)


async def test_all_ghost_draft_falls_back_to_mechanical():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", 0.7, "the only real chunk")

        async def drafter(chunks):
            return [StepDraft(text="invented", source_chunks=[uuid.uuid4()])]  # all ghosts

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        # every drafted step was rejected → mechanical fallback produced a real step
        assert len(proc.steps) == 1
        assert proc.steps[0]["source_chunks"] == [str((await _chunks(org, pk))[0].id)]
        assert proc.steps[0]["confidence"] == pytest.approx(0.7)
    finally:
        await _cleanup(org)


async def test_uncovered_chunks_are_flagged():
    org, pk = str(uuid.uuid4()), "p"
    try:
        covered = await _seed(org, pk, "document", 0.8, "covered")
        dropped = await _seed(org, pk, "document", 0.7, "dropped")

        async def drafter(chunks):
            c = next(x for x in chunks if x.content == "covered")
            return [StepDraft(text="step", source_chunks=[c.id])]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        assert str(dropped) in proc.uncovered_chunks  # flagged, not silently lost
        assert str(covered) not in proc.uncovered_chunks
    finally:
        await _cleanup(org)


async def test_gate_still_fires_on_a_drafted_process():
    from opsforge.agent import assemble_context
    from opsforge.policy import resolve_proposal

    org, pk = str(uuid.uuid4()), "stale"
    try:
        await _seed(org, pk, "document", 0.3, "a stale runbook step")

        async def drafter(chunks):
            return [StepDraft(text="restart", source_chunks=[chunks[0].id])]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        assert proc is not None  # the process was drafted

        # the gate reads the CHUNKS' grounding, not the prose — low grounding still gates
        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
        )
        assert grounding["low_confidence"] is True
        trace = resolve_proposal(
            {"proposals": [{"tool": "k.restart", "class": "reversible"}]},
            "k.restart", {"k.restart": "auto_with_notify"}, grounding=grounding,
        )
        assert trace["state"] == "awaiting_approval"
        assert "low_grounding_gate" in trace["rules"]
    finally:
        await _cleanup(org)


async def _chunks(org, pk):
    from opsforge.knowledge import get_chunks
    return await get_chunks(org, pk)
