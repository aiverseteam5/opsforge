"""M7.3 — the eval harness must be PROVEN to catch regressions.

An eval harness that always says "pass" is worse than none (false assurance — the
same failure family as the M7.2 spoof and the test-that-passed-while-demonstrating-
the-bug). So beyond a green baseline, these tests inject KNOWINGLY-broken behaviour
and confirm the scorecard DROPS on the right set and NAMES the failing cases:

  - a reconciler that resolves a prescriptive conflict the wrong way,
  - a confidence function that ignores contradiction,
  - a "finding-for-everything" detector that must fail the no-conflict case.

The harness lives in evals/ (the kernel's golden-eval home); pytest puts it on the
path. These run against the Compose DB like the rest of the reconciliation suite.
"""

from __future__ import annotations

import uuid

import pytest
from run_knowledge_evals import _cleanup, run_all, score_set

import opsforge.reconcile as recon

pytestmark = pytest.mark.usefixtures("db_required")


# --------------------------------------------------------------------------- #
# baseline — the current system scores 100% on every deterministic set
# --------------------------------------------------------------------------- #
async def test_baseline_scripted_sets_all_pass():
    scorecard = await run_all(modes=("scripted",))
    for set_name, modes in scorecard["sets"].items():
        card = modes["scripted"]
        assert card["accuracy"] == 1.0, f"{set_name} regressed: {card['failed_cases']}"


# --------------------------------------------------------------------------- #
# the meta-requirement — the harness must be able to FAIL
# --------------------------------------------------------------------------- #
async def test_harness_catches_broken_precedence(monkeypatch):
    """Inject a reconciler that mishandles disposition (always reads 'descriptive'):
    a prescriptive conflict now resolves to drift, not a violation. The precedence
    scorecard must drop and name the prescriptive case."""

    async def always_descriptive(org_id, process_key):
        return "descriptive"

    monkeypatch.setattr(recon, "get_disposition", always_descriptive)

    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "precedence", mode="scripted")
    finally:
        await _cleanup(org)

    assert card["accuracy"] < 1.0  # the harness noticed
    # narrowly scoped: EXACTLY the prescriptive case fails, nothing else
    assert card["failed_cases"] == ["document_over_behaviour_prescriptive"]


async def test_harness_catches_broken_confidence(monkeypatch):
    """Inject a confidence function that ignores contradiction. Confidence no longer
    decays with conflict, so the contradiction-monotonic calibration case must fail."""
    real = recon.score_confidence

    def ignores_contradiction(*, source_rank, freshness_days, corroborated_by=0, contradicted_by=0):
        return real(
            source_rank=source_rank,
            freshness_days=freshness_days,
            corroborated_by=corroborated_by,
            contradicted_by=0,  # the bug: conflict is discarded
        )

    monkeypatch.setattr(recon, "score_confidence", ignores_contradiction)

    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "confidence", mode="scripted")
    finally:
        await _cleanup(org)

    assert card["accuracy"] < 1.0
    assert "contradiction_monotonic" in card["failed_cases"]


async def test_harness_rejects_finding_for_everything(monkeypatch):
    """No-false-pass: a detector that contradicts EVERYTHING must NOT score well on
    the no-conflict case — the golden answers aren't trivially satisfiable."""

    async def contradict_all(chunks):
        return [
            recon.ClaimRelation(chunks[i].id, chunks[j].id, "contradicts")
            for i in range(len(chunks))
            for j in range(i + 1, len(chunks))
        ]

    detector = recon.FunctionDetector(contradict_all)

    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "contradiction", detector=detector)
    finally:
        await _cleanup(org)

    assert "no_conflict" in card["failed_cases"]  # spurious finding caught
    # and for the RIGHT reason — the strict "no un-declared finding" check flipped,
    # not some unrelated error.
    no_conflict = next(c for c in card["cases"] if c["id"] == "no_conflict")
    assert no_conflict["checks"]["no_unexpected_findings"] is False


async def test_harness_catches_missing_findings():
    """The false-NEGATIVE direction (the largest block of golden answers): a detector
    that proposes NO relations strips every positive contradiction case of its
    finding. The harness must drop and NAME them — while cases needing no relation
    (no_conflict, the gap cases) still pass."""

    async def no_relations(_chunks):
        return []

    detector = recon.FunctionDetector(no_relations)
    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "contradiction", detector=detector)
    finally:
        await _cleanup(org)

    for cid in (
        "descriptive_drift",
        "prescriptive_violation",
        "undeclared_contradiction",
        "staleness_supersede",
        "contemporaneous_contradiction",
    ):
        assert cid in card["failed_cases"], cid
    assert "no_conflict" not in card["failed_cases"]  # agreement needs no relation
    assert "behaviour_only_gap" not in card["failed_cases"]  # gaps need no relation


async def test_harness_catches_dropped_gap(monkeypatch):
    """`gap` is a real finding kind — prove it is MEASURED, not invisible. Suppress
    gap emission in the engine; the dedicated gap cases must fail and be named."""
    monkeypatch.setattr(recon, "_gap_missing", lambda kinds: None)

    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "contradiction", mode="scripted")
    finally:
        await _cleanup(org)

    for cid in ("behaviour_only_gap", "document_only_gap", "research_only_gap"):
        assert cid in card["failed_cases"], cid


async def test_harness_is_not_a_blanket_failer():
    """Sanity dual to the above: with the REAL system, the no-conflict case PASSES —
    the harness distinguishes a correct reconciler from a finding-for-everything one."""
    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "contradiction", mode="scripted")
    finally:
        await _cleanup(org)
    assert "no_conflict" not in card["failed_cases"]
    assert card["accuracy"] == 1.0
