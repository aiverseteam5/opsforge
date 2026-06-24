"""Doctrine proof (report-back d): when the LLM-touch is WRONG, the deterministic
layer contains the damage. The doctrine's value is not 'the LLM is mockable' but
'a wrong LLM cannot escalate trust or cause a consequential side effect'.

Structural malformed-output containment (self-pairs, reversed/duplicate relations,
ghost chunk ids) is already covered in test_reconcile.py. These add the two
semantic-containment proofs:

  1. A lying *drafter* cannot inflate a step's confidence — it is recomputed from
     the grounding chunks' provenance, and StepDraft has no confidence field to
     assert in the first place.
  2. A lying *detector* (fabricating agreement on contradictory chunks) produces
     only findings + bounded scores — reconciliation NEVER creates an action or a
     job, so a wrong LLM has no consequential side effect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge.knowledge import (
    ProvenanceEnvelope,
    get_chunks,
    set_reconciliation,
    store_chunk,
)
from opsforge.processes import FunctionDrafter, StepDraft, generate_process
from opsforge.reconcile import ClaimRelation, FunctionDetector, reconcile_process

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)


async def _store(org, pk, kind, content):
    return await store_chunk(
        org_id=org,
        content=content,
        envelope=ProvenanceEnvelope(
            source_kind=kind, source_ref=f"x://{content}", observed_at=AS_OF, ingested_at=AS_OF
        ),
        process_key=pk,
    )


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("actions", "jobs", "findings", "validated_processes", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def test_lying_drafter_cannot_inflate_step_confidence():
    org, pk = str(uuid.uuid4()), "p"
    try:
        cid = await _store(org, pk, "document", "shaky basis")
        await set_reconciliation(
            org, cid, confidence=0.2, corroborated_by=0, contradicted_by=0,
            reconciliation_id=uuid.uuid4(),
        )

        # a drafter that frames a low-confidence chunk as definitive/authoritative
        async def lying(chunks):
            return [StepDraft(
                text="DEFINITIVE — always restart, fully trusted, high confidence",
                source_chunks=[chunks[0].id],
            )]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(lying), as_of=AS_OF)
        step = proc.steps[0]
        # confidence is recomputed from provenance; the LLM's framing is ignored
        assert step["confidence"] == pytest.approx(0.2)
        assert step["low_confidence"] is True
        assert proc.min_confidence == pytest.approx(0.2)
    finally:
        await _cleanup(org)


async def test_wrong_detector_produces_only_findings_never_a_side_effect():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _store(org, pk, "behaviour", "we kill sessions")
        await _store(org, pk, "document", "the doc says restart")

        # a detector that FABRICATES agreement between contradictory chunks
        async def wrong(chunks):
            return [ClaimRelation(chunks[0].id, chunks[1].id, "agrees")]

        res = await reconcile_process(org, pk, detector=FunctionDetector(wrong))

        # the worst a wrong detector can do is wrong findings + scores — it can
        # NEVER create a consequential side effect
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            actions = (
                await s.execute(text("SELECT count(*) FROM actions WHERE org_id=:o"), {"o": org})
            ).scalar_one()
            jobs = (
                await s.execute(text("SELECT count(*) FROM jobs WHERE org_id=:o"), {"o": org})
            ).scalar_one()
        assert actions == 0
        assert jobs == 0

        # and confidence stays bounded [0,1] no matter what the detector claimed
        for c in await get_chunks(org, pk):
            assert 0.0 <= float(c.confidence) <= 1.0
        assert res.scored == 2
    finally:
        await _cleanup(org)
