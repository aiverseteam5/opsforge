"""M7.2 — provenance-disjoint corroboration (THIS IS A SAFETY BLOCKER).

The confidence score is the single axiom the M6.5 grounding gate trusts. Its
corroboration input was spoofable: duplicating one source (a document split into
many chunks, a page restated) manufactured "agreements" that lifted the score
within the saturating bound. This milestone makes agreement count only between
PROVENANCE-DISJOINT sources — clustering corroborating chunks by `provenance_root`
and counting DISTINCT roots, never raw chunks.

Two layers of proof, per the brief:
  §3  five CALIBRATION cases — produce known-correct confidence values;
  §4  six ADVERSARIAL tests — the spoofs the milestone exists to defeat, with the
      headline being: duplication can no longer push weak grounding past the gate.

The detector (the only LLM-shaped step) is faked deterministically; the engine
and the disjointness math are exercised end to end. `provenance_root` is derived
from `source_ref`, so seeding two chunks with the same ref simulates "one document
split into two chunks" (one root); different refs are genuinely separate origins.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from opsforge.confidence import score_confidence
from opsforge.config import get_settings
from opsforge.knowledge import ProvenanceEnvelope, get_chunks, store_chunk
from opsforge.reconcile import ClaimRelation, FunctionDetector, reconcile_process

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)


def _conf(rank: int, corr: int = 0, contra: int = 0, fresh: int = 0) -> float:
    """The deterministic confidence value, for asserting known-correct numbers."""
    return score_confidence(
        source_rank=rank, freshness_days=fresh, corroborated_by=corr, contradicted_by=contra
    ).confidence


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _seed(
    org: str, pk: str, kind: str, content: str, ref: str, observed_at: datetime = AS_OF
) -> uuid.UUID:
    """Seed one chunk. `ref` IS the provenance root (M7.2): chunks sharing a ref
    share a root (a document split into chunks); distinct refs are separate
    origins."""
    env = ProvenanceEnvelope(
        source_kind=kind, source_ref=ref, observed_at=observed_at, ingested_at=observed_at
    )
    return await store_chunk(org_id=org, content=content, envelope=env, process_key=pk)


def _among(relation: str, *contents: str) -> FunctionDetector:
    """Detector relating EVERY pair among the named chunks with `relation` — a
    full agreement/contradiction clique."""
    wanted = set(contents)

    async def fn(chunks):
        sel = [c for c in chunks if c.content in wanted]
        out = []
        for i in range(len(sel)):
            for j in range(i + 1, len(sel)):
                out.append(ClaimRelation(sel[i].id, sel[j].id, relation))
        return out

    return FunctionDetector(fn)


def _pairs(relation: str, *pairs: tuple[str, str]) -> FunctionDetector:
    """Detector relating exactly the named (content_a, content_b) pairs."""

    async def fn(chunks):
        by = {c.content: c for c in chunks}
        return [ClaimRelation(by[a].id, by[b].id, relation) for a, b in pairs]

    return FunctionDetector(fn)


async def _blank_root(org: str, chunk_id: uuid.UUID) -> None:
    """Force a chunk's provenance_root to NULL — i.e. lineage that could not be
    determined. The normal store path always derives a root from source_ref; a
    future richer deriver may legitimately fail to resolve one, and this is the
    fail-safe path it must take."""
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(
            text(
                "UPDATE knowledge_chunks SET provenance_root = NULL "
                "WHERE id = :i AND org_id = :o"
            ),
            {"i": str(chunk_id), "o": org},
        )


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in ("findings", "validated_processes", "knowledge_chunks", "process_dispositions"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


def _by_id(chunks):
    return {c.id: c for c in chunks}


# --------------------------------------------------------------------------- #
# §3 — the five calibration cases (known-correct confidence values)
# --------------------------------------------------------------------------- #
async def test_calibration_disjoint_agreement_lifts_by_one_distinct_root():
    """Two chunks, genuinely separate origins, agreeing → each is corroborated by
    the one OTHER distinct root, and confidence equals the formula at corr=1."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        a = await _seed(org, pk, "behaviour", "claim-a", "root://a")
        b = await _seed(org, pk, "behaviour", "claim-b", "root://b")
        await reconcile_process(
            org, pk, detector=_among("agrees", "claim-a", "claim-b"), as_of=AS_OF
        )

        by = _by_id(await get_chunks(org, pk))
        expected = _conf(3, corr=1)
        for cid, other_root in ((a, "root://b"), (b, "root://a")):
            assert by[cid].corroborated_by == 1
            assert by[cid].corroborating_roots == [other_root]
            assert by[cid].confidence == pytest.approx(expected)
    finally:
        await _cleanup(org)


async def test_calibration_same_root_duplication_adds_nothing():
    """One document split into three agreeing chunks (one root) → ZERO distinct
    OTHER roots → confidence equals the solo (no-corroboration) value."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        for i in range(3):
            await _seed(org, pk, "behaviour", f"chunk-{i}", "root://one-doc")
        await reconcile_process(
            org, pk, detector=_among("agrees", "chunk-0", "chunk-1", "chunk-2"), as_of=AS_OF
        )

        solo = _conf(3)
        for c in await get_chunks(org, pk):
            assert c.corroborated_by == 0
            assert c.corroborating_roots == []
            assert c.confidence == pytest.approx(solo)
    finally:
        await _cleanup(org)


async def test_calibration_fabricated_agreement_zero_lift():
    """N chunks all tracing to one origin, all 'agreeing' → the new math gives the
    corroboration term nothing; the chunk scores exactly as a lone source would.
    (The old saturating bound would have lifted it.)"""
    org, pk = str(uuid.uuid4()), "p"
    try:
        for i in range(5):  # one source restated five times
            await _seed(org, pk, "behaviour", f"restated-{i}", "root://single-origin")
        await reconcile_process(
            org, pk, detector=_among("agrees", *[f"restated-{i}" for i in range(5)]), as_of=AS_OF
        )

        lone = _conf(3)
        for c in await get_chunks(org, pk):
            # each chunk genuinely had 4 agreeing partners, yet they collapse to 0 roots
            assert c.corroborated_by == 0
            assert c.confidence == pytest.approx(lone)
    finally:
        await _cleanup(org)


async def test_calibration_mixed_counts_distinct_roots_not_chunks():
    """Two disjoint roots + three same-root duplicates of one → the cluster holds
    two distinct roots, not five chunks. The lone-root chunk is corroborated by
    exactly one OTHER root (not the four chunks bearing it)."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "solo-a", "root://a")
        for i in range(4):  # root-b appears in four chunks (1 + 3 duplicates)
            await _seed(org, pk, "behaviour", f"b-{i}", "root://b")
        await reconcile_process(
            org, pk, detector=_among("agrees", "solo-a", *[f"b-{i}" for i in range(4)]), as_of=AS_OF
        )

        by = {c.content: c for c in await get_chunks(org, pk)}
        # the whole agreeing cluster contains exactly two distinct roots
        cluster_roots = {r for c in by.values() for r in c.corroborating_roots} | {"root://a"}
        assert cluster_roots == {"root://a", "root://b"}
        # solo-a saw four root-b chunks but counts ONE distinct other root
        assert by["solo-a"].corroborated_by == 1
        assert by["solo-a"].corroborating_roots == ["root://b"]
        # each root-b chunk sees one distinct other root (root-a); siblings don't count
        for i in range(4):
            assert by[f"b-{i}"].corroborated_by == 1
            assert by[f"b-{i}"].corroborating_roots == ["root://a"]
    finally:
        await _cleanup(org)


async def test_calibration_uncertain_lineage_not_counted():
    """Ambiguous shared lineage (indeterminate root) → treated as NOT disjoint:
    it does not lift a determinate chunk's confidence."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        d = await _seed(org, pk, "behaviour", "determinate", "root://d")
        u = await _seed(org, pk, "behaviour", "uncertain", "root://u")
        await _blank_root(org, u)  # lineage could not be resolved → NULL root
        await reconcile_process(
            org, pk, detector=_among("agrees", "determinate", "uncertain"), as_of=AS_OF
        )

        by = _by_id(await get_chunks(org, pk))
        solo = _conf(3)
        # the determinate chunk is NOT lifted by the ambiguous one (the safe error)
        assert by[d].corroborated_by == 0
        assert by[d].corroborating_roots == []
        assert by[d].confidence == pytest.approx(solo)
        # and the indeterminate chunk cannot lift ITSELF off the determinate one —
        # a chunk whose own lineage is unknown gets zero corroboration (center-side fail-safe)
        assert by[u].corroborated_by == 0
        assert by[u].corroborating_roots == []
        assert by[u].confidence == pytest.approx(solo)
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# §4 — the six adversarial acceptance tests
# --------------------------------------------------------------------------- #
async def test_adversarial_fabricated_agreement_gives_zero_lift():
    """1. A source duplicated to manufacture N agreements contributes nothing —
    the chunk scores identically with or without the fabricated padding."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "document", "honest", "root://doc")
        for i in range(6):  # padding: the same doc restated six more times
            await _seed(org, pk, "document", f"pad-{i}", "root://doc")
        await reconcile_process(
            org,
            pk,
            detector=_among("agrees", "honest", *[f"pad-{i}" for i in range(6)]),
            as_of=AS_OF,
        )

        unpadded = _conf(2)
        for c in await get_chunks(org, pk):
            assert c.corroborated_by == 0
            assert c.confidence == pytest.approx(unpadded)
    finally:
        await _cleanup(org)


async def test_adversarial_gate_not_bypassed_by_duplication():
    """2. THE HEADLINE. Knowledge that should be low-confidence, padded with
    same-root 'agreements', stays low — so the untouched M6.5 gate STILL forces a
    human on a consequential action. Under raw counting the padding WOULD have
    crossed the threshold; we assert that contrast explicitly."""
    from opsforge.agent import assemble_context
    from opsforge.policy import resolve_proposal

    org, pk = str(uuid.uuid4()), "stale-runbook"
    threshold = get_settings().context_grounding_threshold
    aged = AS_OF - timedelta(days=180)  # one freshness half-life

    # the contrast the milestone exists to create: a document at rank 2, one
    # half-life old, is BELOW the gate on its own, but four raw "agreements"
    # would have lifted it ABOVE the gate.
    base = _conf(2, fresh=180)
    spoofed = _conf(2, corr=4, fresh=180)
    assert base < threshold <= spoofed  # raw counting WOULD have bypassed the gate

    try:
        await _seed(org, pk, "document", "weak", "root://one", observed_at=aged)
        for i in range(4):  # one source restated → four fabricated agreements
            await _seed(org, pk, "document", f"copy-{i}", "root://one", observed_at=aged)
        await reconcile_process(
            org,
            pk,
            detector=_among("agrees", "weak", *[f"copy-{i}" for i in range(4)]),
            as_of=AS_OF,
        )

        chunks = await get_chunks(org, pk)
        assert all(c.corroborated_by == 0 for c in chunks)  # duplication added nothing
        assert max(float(c.confidence) for c in chunks) == pytest.approx(base)  # stayed weak

        # the gate reads grounding from the chunks; weak grounding still gates.
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


async def test_adversarial_genuine_independent_corroboration_still_lifts():
    """3. The over-correction guard: two genuinely disjoint sources agreeing must
    still raise confidence above the solo score."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "witness-1", "root://team-a")
        await _seed(org, pk, "behaviour", "witness-2", "root://team-b")
        await reconcile_process(
            org, pk, detector=_among("agrees", "witness-1", "witness-2"), as_of=AS_OF
        )

        solo = _conf(3)
        chunks = await get_chunks(org, pk)
        assert all(c.corroborated_by == 1 for c in chunks)
        assert all(float(c.confidence) > solo for c in chunks)  # real corroboration still works
    finally:
        await _cleanup(org)


async def test_adversarial_contradiction_symmetry_distinct_roots():
    """4. Distinct-root counting applies to contradiction too: one source split
    into many contradicting chunks cannot dominate the count."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "target", "root://t")
        for i in range(4):  # one contradicting source restated four times
            await _seed(org, pk, "behaviour", f"x-{i}", "root://x")
        # only target<->x pairs contradict (siblings don't), all contemporaneous
        await reconcile_process(
            org,
            pk,
            detector=_pairs("contradicts", *[("target", f"x-{i}") for i in range(4)]),
            as_of=AS_OF,
        )

        by = {c.content: c for c in await get_chunks(org, pk)}
        # target was contradicted by four chunks but only ONE distinct root
        assert by["target"].contradicted_by == 1
        assert by["target"].contradicting_roots == ["root://x"]
        for i in range(4):
            assert by[f"x-{i}"].contradicted_by == 1  # each sees the one root://t
        # and the SCORE the gate trusts reflects one contradiction, not four —
        # 4 same-root contradictors collapse to the 1-root value (raw would differ)
        assert by["target"].confidence == pytest.approx(_conf(3, contra=1))
        assert _conf(3, contra=4) != pytest.approx(_conf(3, contra=1))
    finally:
        await _cleanup(org)


async def test_adversarial_contradiction_failsafe_counts_same_and_null_roots():
    """4b. The contradiction fail-safe is INVERTED vs corroboration: a same-source
    or indeterminate-lineage contradictor must still LOWER confidence (dropping it
    would inflate — the unsafe direction). Both are counted; the score falls."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "subject", "root://s")
        await _seed(org, pk, "behaviour", "same-src", "root://s")  # shares subject's root
        nullc = await _seed(org, pk, "behaviour", "null-src", "root://n")
        await _blank_root(org, nullc)  # contradictor with unresolved lineage
        await reconcile_process(
            org,
            pk,
            detector=_pairs("contradicts", ("subject", "same-src"), ("subject", "null-src")),
            as_of=AS_OF,
        )

        by = {c.content: c for c in await get_chunks(org, pk)}
        # neither contradictor was dropped: one same-root bucket + one indeterminate
        assert by["subject"].contradicted_by == 2
        assert "(indeterminate-lineage)" in by["subject"].contradicting_roots
        assert by["subject"].confidence == pytest.approx(_conf(3, contra=2))
        assert float(by["subject"].confidence) < _conf(3)  # never inflated by dropping
    finally:
        await _cleanup(org)


async def test_adversarial_uncertain_lineage_fails_safe():
    """5. Ambiguous shared origin → not counted, in BOTH directions. An
    indeterminate-root chunk cannot inflate a real chunk, NOR be lifted itself."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        real = await _seed(org, pk, "behaviour", "real", "root://real")
        ghosts = []
        for i in range(3):
            cid = await _seed(org, pk, "behaviour", f"ghosty-{i}", f"root://ghost-{i}")
            await _blank_root(org, cid)  # each padding chunk has unresolved lineage
            ghosts.append(cid)
        await reconcile_process(
            org,
            pk,
            detector=_among("agrees", "real", "ghosty-0", "ghosty-1", "ghosty-2"),
            as_of=AS_OF,
        )

        by = _by_id(await get_chunks(org, pk))
        solo = _conf(3)
        assert by[real].corroborated_by == 0  # ambiguous padding gave nothing
        assert by[real].confidence == pytest.approx(solo)
        # and the least-trustworthy chunks (unknown lineage) are not lifted either —
        # the center-side fail-safe, so the gate never sees an inflated ghost
        for g in ghosts:
            assert by[g].corroborated_by == 0
            assert by[g].confidence == pytest.approx(solo)
    finally:
        await _cleanup(org)


async def test_adversarial_explainability_names_distinct_roots():
    """6. The breakdown is explainable: a chunk lifted by two independent sources
    records exactly which two distinct roots did it — human-readable, not a count."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed(org, pk, "behaviour", "center", "root://center")
        await _seed(org, pk, "document", "src-x", "root://x")
        await _seed(org, pk, "research", "src-y", "root://y")
        # center agrees with both x and y; x and y also share root://x duplicate noise
        await _seed(org, pk, "document", "x-dupe", "root://x")
        await reconcile_process(
            org,
            pk,
            detector=_pairs(
                "agrees",
                ("center", "src-x"),
                ("center", "src-y"),
                ("center", "x-dupe"),
            ),
            as_of=AS_OF,
        )

        center = next(c for c in await get_chunks(org, pk) if c.content == "center")
        # two distinct roots lifted it (root://x once, despite x + x-dupe; root://y)
        assert center.corroborated_by == 2
        assert center.corroborating_roots == ["root://x", "root://y"]  # sorted, named, distinct
    finally:
        await _cleanup(org)


async def test_adversarial_null_root_center_not_lifted():
    """1b. The chunk being SCORED has indeterminate lineage yet two genuinely-
    distinct determinate sources agree with it. It must NOT be lifted — its own
    origin is unknowable, so it cannot be trusted as corroborated (the center-side
    of the fail-safe). The over-correction guard: the determinate pair still lifts."""
    org, pk = str(uuid.uuid4()), "p"
    try:
        center = await _seed(org, pk, "behaviour", "center", "root://c")
        await _blank_root(org, center)  # the SCORED chunk's lineage is indeterminate
        await _seed(org, pk, "behaviour", "p1", "root://p1")
        await _seed(org, pk, "behaviour", "p2", "root://p2")
        await reconcile_process(
            org, pk, detector=_among("agrees", "center", "p1", "p2"), as_of=AS_OF
        )

        by = {c.content: c for c in await get_chunks(org, pk)}
        assert by["center"].corroborated_by == 0  # two determinate agreers, still zero
        assert by["center"].corroborating_roots == []
        assert by["center"].confidence == pytest.approx(_conf(3))
        # sanity: the fix did not over-correct — the determinate pair corroborates
        assert by["p1"].corroborated_by == 1
        assert by["p2"].corroborated_by == 1
    finally:
        await _cleanup(org)
