"""M6.3 — reconciliation engine acceptance.

The headline checks (spec M6.3): a behaviour-vs-document conflict on a
*descriptive* process proposes a doc update (drift); the same on a *prescriptive*
process emits a violation; undeclared resolves nothing (a contradiction finding);
nothing is ever silently resolved. Plus staleness supersession, gap detection,
and corroboration raising confidence.

The detector (the only LLM-shaped step) is faked deterministically so the engine
itself is exercised end to end.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from opsforge.confidence import score_confidence
from opsforge.dispositions import declare_disposition
from opsforge.findings import list_findings
from opsforge.knowledge import ProvenanceEnvelope, get_chunks, store_chunk
from opsforge.reconcile import ClaimRelation, FunctionDetector, reconcile_process

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)


async def _seed(org: str, pk: str, kind: str, observed_at: datetime, content: str) -> uuid.UUID:
    env = ProvenanceEnvelope(
        source_kind=kind,
        source_ref=f"x://{kind}/{content}",
        observed_at=observed_at,
        ingested_at=observed_at,
    )
    return await store_chunk(org_id=org, content=content, envelope=env, process_key=pk)


def _relate(relation: str) -> FunctionDetector:
    """Detector that relates the first two chunks it is given with `relation`."""

    async def fn(chunks):
        if len(chunks) < 2:
            return []
        return [ClaimRelation(chunks[0].id, chunks[1].id, relation)]

    return FunctionDetector(fn)


def _none() -> FunctionDetector:
    async def fn(chunks):
        return []

    return FunctionDetector(fn)


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in ("findings", "knowledge_chunks", "process_dispositions"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


async def test_descriptive_conflict_proposes_doc_update():
    org, pk = str(uuid.uuid4()), "deploy-flow"
    try:
        await declare_disposition(
            org_id=org, process_key=pk, disposition="descriptive", rationale="reality wins"
        )
        b = await _seed(org, pk, "behaviour", AS_OF, "what we actually do")
        d = await _seed(org, pk, "document", AS_OF, "what the runbook says")

        res = await reconcile_process(
            org, pk, detector=_relate("contradicts"), as_of=AS_OF
        )
        # resolved to drift, NOT silently and NOT left as a bare contradiction
        assert res.findings_by_kind.get("drift") == 1
        assert "contradiction" not in res.findings_by_kind

        drift = next(f for f in await list_findings(org, process_key=pk) if f.kind == "drift")
        assert drift.state == "open"
        assert drift.detail["action"] == "update_document_to_match_behaviour"
        assert drift.detail["behaviour_chunk"] == str(b)
        assert drift.detail["document_chunk"] == str(d)
        assert set(drift.evidence_refs) == {str(b), str(d)}
        # drift carries the behaviour (higher) chunk's score
        assert drift.confidence == pytest.approx(
            score_confidence(
                source_rank=3, freshness_days=0, corroborated_by=0, contradicted_by=1
            ).confidence
        )

        # both chunks were scored and carry the contradiction count
        chunks = await get_chunks(org, pk)
        assert all(c.confidence is not None for c in chunks)
        assert all(c.contradicted_by == 1 for c in chunks)
    finally:
        await _cleanup(org)


async def test_prescriptive_conflict_flags_violation():
    org, pk = str(uuid.uuid4()), "change-approval"
    try:
        await declare_disposition(
            org_id=org, process_key=pk, disposition="prescriptive", rationale="doc is law"
        )
        await _seed(org, pk, "behaviour", AS_OF, "team bypasses approval")
        await _seed(org, pk, "document", AS_OF, "approval SOP")

        res = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)
        assert res.findings_by_kind.get("violation") == 1
        assert "drift" not in res.findings_by_kind
        v = next(f for f in await list_findings(org, process_key=pk) if f.kind == "violation")
        assert v.detail["action"] == "behaviour_violates_standard"
        # violation carries the document (the standard / lower) chunk's score
        assert v.confidence == pytest.approx(
            score_confidence(
                source_rank=2, freshness_days=0, corroborated_by=0, contradicted_by=1
            ).confidence
        )
    finally:
        await _cleanup(org)


async def test_undeclared_conflict_resolves_nothing():
    org, pk = str(uuid.uuid4()), "mystery-process"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "a")
        await _seed(org, pk, "document", AS_OF, "b")
        res = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)
        # surfaced, not resolved
        assert res.findings_by_kind.get("contradiction") == 1
        assert "drift" not in res.findings_by_kind
        assert "violation" not in res.findings_by_kind
        c = next(f for f in await list_findings(org, process_key=pk) if f.kind == "contradiction")
        assert c.detail["disposition"] == "undeclared"
        assert c.detail["action"] == "declare_disposition"
    finally:
        await _cleanup(org)


async def test_staleness_supersedes_older_chunk():
    org, pk = str(uuid.uuid4()), "runbook-v"
    try:
        old = await _seed(org, pk, "document", AS_OF - timedelta(days=90), "2023 runbook")
        new = await _seed(org, pk, "document", AS_OF, "current runbook")
        res = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)

        assert res.superseded == 1
        assert res.findings_by_kind.get("stale") == 1
        # a doc-only survivor co-emits a "not_practiced" gap
        assert res.findings_by_kind.get("gap") == 1
        gap = next(f for f in await list_findings(org, process_key=pk) if f.kind == "gap")
        assert gap.detail["missing"] == "not_practiced"
        # the old chunk is no longer active knowledge; the new one survives, scored
        active = await get_chunks(org, pk)
        assert [c.id for c in active] == [new]
        assert active[0].confidence is not None
        # the stale finding carries the surviving (newer) chunk's score
        stale = next(f for f in await list_findings(org, process_key=pk) if f.kind == "stale")
        assert stale.confidence == pytest.approx(active[0].confidence)
        # it stays in the table for audit, flagged superseded
        all_rows = await get_chunks(org, pk, include_superseded=True)
        superseded = next(c for c in all_rows if c.id == old)
        assert superseded.superseded_by == new
    finally:
        await _cleanup(org)


async def test_gap_for_undocumented_behaviour():
    org, pk = str(uuid.uuid4()), "tribal-knowledge"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "only how we do it, never written down")
        res = await reconcile_process(org, pk, detector=_none(), as_of=AS_OF)
        assert res.findings_by_kind.get("gap") == 1
        gap = next(f for f in await list_findings(org, process_key=pk) if f.kind == "gap")
        assert gap.detail["missing"] == "no_documentation"
    finally:
        await _cleanup(org)


async def test_corroboration_raises_confidence():
    org, pk = str(uuid.uuid4()), "well-known"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "first witness")
        await _seed(org, pk, "behaviour", AS_OF, "second witness")
        await reconcile_process(org, pk, detector=_relate("agrees"), as_of=AS_OF)

        chunks = await get_chunks(org, pk)
        assert all(c.corroborated_by == 1 for c in chunks)
        # confidence equals the formula with one corroboration, and exceeds the solo score
        expected = score_confidence(
            source_rank=3, freshness_days=0, corroborated_by=1, contradicted_by=0
        ).confidence
        solo = score_confidence(
            source_rank=3, freshness_days=0, corroborated_by=0, contradicted_by=0
        ).confidence
        assert all(c.confidence == pytest.approx(expected) for c in chunks)
        assert expected > solo
    finally:
        await _cleanup(org)


async def test_cross_rank_conflict_is_not_silently_superseded():
    """HIGH-fix: fresh behaviour contradicting a much older document must resolve
    via disposition (drift) — NOT silently supersede the document as stale."""
    org, pk = str(uuid.uuid4()), "stale-runbook-vs-reality"
    try:
        await declare_disposition(
            org_id=org, process_key=pk, disposition="descriptive", rationale="reality wins"
        )
        doc = await _seed(org, pk, "document", AS_OF - timedelta(days=120), "old runbook")
        beh = await _seed(org, pk, "behaviour", AS_OF, "what we actually do now")
        res = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)

        assert res.superseded == 0  # nothing silently superseded
        assert res.findings_by_kind.get("drift") == 1
        assert "stale" not in res.findings_by_kind
        assert {c.id for c in await get_chunks(org, pk)} == {doc, beh}
    finally:
        await _cleanup(org)


async def test_reconcile_is_idempotent_on_rerun():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "a")
        await _seed(org, pk, "document", AS_OF, "b")
        r1 = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)
        after1 = await list_findings(org, process_key=pk)
        r2 = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)
        after2 = await list_findings(org, process_key=pk)

        # no duplicate findings; the still-open finding is not re-opened
        assert len(after2) == len(after1)
        # chunks were rescored (UPDATE) with the new run's id, not duplicated
        chunks = await get_chunks(org, pk)
        assert r2.reconciliation_id != r1.reconciliation_id
        assert all(c.reconciliation_id == r2.reconciliation_id for c in chunks)
    finally:
        await _cleanup(org)


async def test_relation_to_superseded_chunk_is_excluded():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", AS_OF - timedelta(days=90), "old doc")
        await _seed(org, pk, "document", AS_OF, "new doc")
        other = await _seed(org, pk, "behaviour", AS_OF, "behaviour note")

        async def fn(chunks):
            by = {c.content: c for c in chunks}
            return [
                ClaimRelation(by["old doc"].id, by["new doc"].id, "contradicts"),  # → stale
                ClaimRelation(by["old doc"].id, by["behaviour note"].id, "contradicts"),
            ]

        res = await reconcile_process(org, pk, detector=FunctionDetector(fn), as_of=AS_OF)
        assert res.superseded == 1  # old doc superseded by new doc (same kind)
        active = await get_chunks(org, pk)
        oth = next(c for c in active if c.id == other)
        assert oth.contradicted_by == 0  # the superseded partner is not counted
        # and no contradiction finding references the superseded chunk
        assert "contradiction" not in res.findings_by_kind
    finally:
        await _cleanup(org)


async def test_equal_rank_conflict_stays_contradiction_even_when_declared():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await declare_disposition(
            org_id=org, process_key=pk, disposition="descriptive", rationale="r"
        )
        await _seed(org, pk, "behaviour", AS_OF, "witness one")
        await _seed(org, pk, "behaviour", AS_OF, "witness two")
        res = await reconcile_process(org, pk, detector=_relate("contradicts"), as_of=AS_OF)
        # precedence cannot resolve an equal-rank conflict → contradiction, not drift
        assert res.findings_by_kind.get("contradiction") == 1
        assert "drift" not in res.findings_by_kind
        c = next(f for f in await list_findings(org, process_key=pk) if f.kind == "contradiction")
        assert c.detail["disposition"] == "descriptive"
        assert c.detail["action"] == "needs_human_review"
    finally:
        await _cleanup(org)


async def test_gap_kinds_document_only_and_research_only():
    org = str(uuid.uuid4())
    try:
        await _seed(org, "doc-only", "document", AS_OF, "a")
        await reconcile_process(org, "doc-only", detector=_none(), as_of=AS_OF)
        g1 = next(f for f in await list_findings(org, process_key="doc-only") if f.kind == "gap")
        assert g1.detail["missing"] == "not_practiced"

        await _seed(org, "research-only", "research", AS_OF, "b")
        await reconcile_process(org, "research-only", detector=_none(), as_of=AS_OF)
        g2 = next(
            f for f in await list_findings(org, process_key="research-only") if f.kind == "gap"
        )
        assert g2.detail["missing"] == "no_authoritative_source"
    finally:
        await _cleanup(org)


async def test_staleness_boundary_at_threshold():
    org = str(uuid.uuid4())
    try:
        # exactly 30 days apart → supersede
        await _seed(org, "p30", "document", AS_OF - timedelta(days=30), "old")
        await _seed(org, "p30", "document", AS_OF, "new")
        r30 = await reconcile_process(org, "p30", detector=_relate("contradicts"), as_of=AS_OF)
        assert r30.superseded == 1

        # 29 days apart → contemporaneous contradiction, not superseded
        await _seed(org, "p29", "document", AS_OF - timedelta(days=29), "old")
        await _seed(org, "p29", "document", AS_OF, "new")
        r29 = await reconcile_process(org, "p29", detector=_relate("contradicts"), as_of=AS_OF)
        assert r29.superseded == 0
        assert r29.findings_by_kind.get("contradiction") == 1
    finally:
        await _cleanup(org)


async def test_detector_reversed_and_duplicate_pairs_are_deduped():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "a")
        await _seed(org, pk, "document", AS_OF, "b")

        async def fn(chunks):
            a, b = chunks[0].id, chunks[1].id
            return [
                ClaimRelation(a, b, "contradicts"),
                ClaimRelation(b, a, "contradicts"),  # reversed
                ClaimRelation(a, b, "contradicts"),  # duplicate
            ]

        res = await reconcile_process(org, pk, detector=FunctionDetector(fn), as_of=AS_OF)
        assert res.findings_by_kind.get("contradiction") == 1
        assert all(c.contradicted_by == 1 for c in await get_chunks(org, pk))
    finally:
        await _cleanup(org)


async def test_self_pair_is_ignored():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", AS_OF, "a")

        async def fn(chunks):
            return [ClaimRelation(chunks[0].id, chunks[0].id, "contradicts")]

        res = await reconcile_process(org, pk, detector=FunctionDetector(fn), as_of=AS_OF)
        assert "contradiction" not in res.findings_by_kind
        assert (await get_chunks(org, pk))[0].contradicted_by == 0
    finally:
        await _cleanup(org)
