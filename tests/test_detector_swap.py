"""M7.4 — the real LLM detector is the PRODUCTION reconcile path.

Detector ACCURACY is already measured by the M7.3 harness (llm mode 9/9) and is
NOT re-tested here. These target the actual risk per the brief: the production
default wiring, the fallback contract under real failures, the determinism of the
safe-fallback path, that a degraded run is RECORDED (not silent), and that the
M7.2 (no manufactured confidence) and M6.5 (gate) safety properties still hold
with the detector live.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from opsforge.gateway import ChatResult
from opsforge.knowledge import ProvenanceEnvelope, freshness_days, get_chunks, store_chunk
from opsforge.reconcile import (
    FunctionDetector,
    LexicalDetector,
    LLMDetector,
    configured_detector,
    reconcile_process,
)
from opsforge.reconciliations import latest_reconciliation

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)
_KEYED = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


class _GoodGW:
    def __init__(self, text: str) -> None:
        self._t = text

    async def chat(self, messages, tools, model):
        return ChatResult(text=self._t)

    async def embedding(self, texts, model):
        return []


class _RaiseGW:
    async def chat(self, messages, tools, model):
        raise RuntimeError("provider down")

    async def embedding(self, texts, model):
        return []


class _Chunk:
    """Minimal stand-in for KnowledgeChunkRow for the no-DB detector unit tests."""

    def __init__(self, kind: str, content: str) -> None:
        self.id, self.source_kind, self.content = uuid.uuid4(), kind, content


# --------------------------------------------------------------------------- #
# production default + effective_mode (no DB)
# --------------------------------------------------------------------------- #
async def test_configured_detector_dev_fallback_when_keyed(monkeypatch):
    # No org provider configured → the LOCAL-DEV env fallback (dev_llm_fallback on) builds
    # the LLM detector from the env key. This is NOT the production path.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(await configured_detector(), LLMDetector)


async def test_configured_detector_is_lexical_floor_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(await configured_detector(), LexicalDetector)


async def test_effective_mode_reports_llm_fallback_and_floor():
    chunks = [
        _Chunk("behaviour", "we skip the review step"),
        _Chunk("document", "review is required"),
    ]

    good = LLMDetector(_GoodGW('[{"a": 0, "b": 1, "relation": "contradicts"}]'), "m")
    await good.analyze(chunks)
    assert good.effective_mode() == "llm"

    empty = LLMDetector(_GoodGW("[]"), "m")  # a VALID "nothing found" stays llm, not fallback
    await empty.analyze(chunks)
    assert empty.effective_mode() == "llm"

    raised = LLMDetector(_RaiseGW(), "m")  # provider error → fall back to the floor
    rels = await raised.analyze(chunks)
    assert raised.effective_mode() == "lexical_fallback"
    assert [r.relation for r in rels] == ["contradicts"]  # the lexical floor produced it

    prose = LLMDetector(_GoodGW("sorry, I cannot help with that"), "m")  # unusable → fallback
    await prose.analyze(chunks)
    assert prose.effective_mode() == "lexical_fallback"

    assert LexicalDetector().effective_mode() == "lexical"

    async def _empty(_c):
        return []

    assert FunctionDetector(_empty).effective_mode() == "scripted"


# --------------------------------------------------------------------------- #
# the run record (DB) — a degraded run is visible, not silent
# --------------------------------------------------------------------------- #
async def _seed(org, pk, kind, content, ref, age=0):
    env = ProvenanceEnvelope(
        source_kind=kind, source_ref=ref, observed_at=AS_OF - timedelta(days=age), ingested_at=AS_OF
    )
    return await store_chunk(org_id=org, content=content, envelope=env, process_key=pk)


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in (
            "reconciliations",
            "findings",
            "validated_processes",
            "knowledge_chunks",
            "process_dispositions",
        ):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def test_fallback_reconciliation_is_recorded_degraded(db_required: None):
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "we skip the review step entirely", "obs://x")
        await _seed(org, pk, "document", "peer review is mandatory before merge", "doc://y")
        res = await reconcile_process(org, pk, detector=LLMDetector(_RaiseGW(), "m"), as_of=AS_OF)
        assert res.detector == "lexical_fallback"
        rec = await latest_reconciliation(org, pk)
        assert rec is not None and rec.detector == "lexical_fallback"
    finally:
        await _cleanup(org)


async def test_healthy_llm_reconciliation_records_llm(db_required: None):
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "we kill sessions", "obs://a")
        await _seed(org, pk, "document", "restart the box", "doc://b")
        gw = _GoodGW('[{"a": 0, "b": 1, "relation": "contradicts"}]')
        res = await reconcile_process(org, pk, detector=LLMDetector(gw, "m"), as_of=AS_OF)
        assert res.detector == "llm"
        rec = await latest_reconciliation(org, pk)
        assert rec is not None and rec.detector == "llm"
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the safety invariants still hold with the detector live / degraded
# --------------------------------------------------------------------------- #
async def test_gate_fires_under_fallback_on_weak_grounding(db_required: None):
    """M6.5 holds even when reconciliation fell back: weak grounding still routes a
    consequential action to the human gate. The gate's firing depends on rank/
    freshness, NOT on contradiction detection, so fallback under-detection can't
    rescue weak grounding past it."""
    from opsforge.agent import assemble_context
    from opsforge.policy import resolve_proposal

    org, pk = str(uuid.uuid4()), "stale"
    try:
        await _seed(org, pk, "research", "an old uncorroborated note", "doc://old-a", age=400)
        await _seed(org, pk, "research", "another old uncorroborated note", "doc://old-b", age=380)
        res = await reconcile_process(org, pk, detector=LLMDetector(_RaiseGW(), "m"), as_of=AS_OF)
        assert res.detector == "lexical_fallback"  # the run was degraded

        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
        )
        assert grounding["low_confidence"] is True
        trace = resolve_proposal(
            {"proposals": [{"tool": "k.restart", "class": "reversible"}]},
            "k.restart",
            {"k.restart": "auto_with_notify"},
            grounding=grounding,
        )
        assert trace["state"] == "awaiting_approval"
        assert "low_grounding_gate" in trace["rules"]
    finally:
        await _cleanup(org)


async def test_fallback_agree_cannot_lift_weak_chunk_past_gate(db_required: None):
    """The lexical floor proposes 'agrees' on token overlap, and a behaviour/document
    pair always has DISTINCT roots — so without a guard M7.2 would count it as a
    legitimate corroboration lift. A weak chunk reconciled under fallback must STAY
    weak: the floor's agreement is dropped, confidence is not lifted, the gate fires."""
    from opsforge.agent import assemble_context
    from opsforge.confidence import score_confidence

    org, pk = str(uuid.uuid4()), "weak"
    try:
        # a stale behaviour chunk (base < 0.5) + a token-SIMILAR document (distinct root)
        # the lexical floor would call 'agrees'.
        beh = await _seed(
            org, pk, "behaviour", "drain the node then redeploy the prior image", "obs://b", age=400
        )
        await _seed(
            org, pk, "document", "drain the node redeploy the prior image now", "doc://d", age=20
        )
        res = await reconcile_process(org, pk, detector=LLMDetector(_RaiseGW(), "m"), as_of=AS_OF)
        assert res.detector == "lexical_fallback"

        row = {x.id: x for x in await get_chunks(org, pk)}[beh]
        base = score_confidence(
            source_rank=3,
            freshness_days=freshness_days(row.observed_at, AS_OF),
            corroborated_by=0,
            contradicted_by=0,
        ).confidence
        assert base < 0.5  # genuinely weak
        assert row.corroborated_by == 0  # the floor's 'agrees' was dropped
        assert float(row.confidence) == pytest.approx(base)  # not lifted

        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
        )
        assert grounding["low_confidence"] is True  # gate still fires under fallback
    finally:
        await _cleanup(org)


async def test_fallback_missed_contradiction_cannot_raise_stored_confidence(db_required: None):
    """A healthy LLM run finds a contradiction and scores a chunk below the gate; a
    later degraded run that is BLIND to it (doc↔doc, which the lexical floor can't
    see) must not overwrite that with a higher score. The fallback write is
    non-regressive — confidence may stay or drop, never rise."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        a = await _seed(org, pk, "document", "the freeze window is the last week", "doc://a", 5)
        await _seed(org, pk, "document", "there is no freeze window at all", "doc://b", 5)

        good = _GoodGW('[{"a": 0, "b": 1, "relation": "contradicts"}]')
        await reconcile_process(org, pk, detector=LLMDetector(good, "m"), as_of=AS_OF)  # mode 'llm'
        after1 = float({x.id: x for x in await get_chunks(org, pk)}[a].confidence)

        res2 = await reconcile_process(org, pk, detector=LLMDetector(_RaiseGW(), "m"), as_of=AS_OF)
        assert res2.detector == "lexical_fallback"  # blind to the doc↔doc contradiction
        after2 = float({x.id: x for x in await get_chunks(org, pk)}[a].confidence)

        assert after2 <= after1  # the degraded run did NOT raise it
        assert after2 == pytest.approx(after1)  # the truthful low score is preserved
    finally:
        await _cleanup(org)


async def test_same_root_duplication_zero_lift_with_llm_asserting_agreement(db_required: None):
    """M7.2 holds with the REAL detector as the asserter: the LLM densely asserts
    agreement across three restatements of ONE source, yet same-root duplication
    lifts nothing — the engine's distinct-root counting zeroes it."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        gw = _GoodGW(
            '[{"a": 0, "b": 1, "relation": "agrees"}, {"a": 0, "b": 2, "relation": "agrees"}, '
            '{"a": 1, "b": 2, "relation": "agrees"}]'
        )
        for i in range(3):
            await _seed(org, pk, "behaviour", f"restated {i}", "obs://one-source")
        res = await reconcile_process(org, pk, detector=LLMDetector(gw, "m"), as_of=AS_OF)
        assert res.detector == "llm"  # the LLM ran and asserted agreement
        for x in await get_chunks(org, pk):
            assert x.corroborated_by == 0  # zero lift despite the asserted agreement
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the measured-ship gate — production path holds the saved baseline (keyed only)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _KEYED, reason="no provider key; production mode falls to lexical floor")
async def test_production_path_holds_saved_baseline():
    """The milestone's whole point: the production-wired system (configured_detector)
    must HOLD the saved real-LLM contradiction baseline, with confidence + precedence
    UNCHANGED. Hits the real provider; skipped without a key."""
    import json
    from pathlib import Path

    from run_knowledge_evals import run_all, ship_verdict

    bpath = Path("evals/scorecards/knowledge_baseline.json")
    baseline = json.loads(bpath.read_text(encoding="utf-8"))
    current = await run_all(modes=("scripted", "production"))
    verdict = ship_verdict(current, baseline)
    assert verdict["contradiction"]["holds"], verdict["rationale"]
    assert verdict["confidence_unchanged"], verdict["rationale"]
    assert verdict["precedence_unchanged"], verdict["rationale"]
    assert verdict["ship"], verdict["rationale"]
