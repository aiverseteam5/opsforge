"""M6.2 — the deterministic confidence formula (pure, no DB, no LLM)."""

from __future__ import annotations

import pytest

from opsforge.confidence import (
    ConfidenceWeights,
    freshness_factor,
    saturating,
    score_confidence,
)

W = ConfidenceWeights(
    w_source=0.40,
    w_fresh=0.25,
    w_corroborate=0.25,
    w_contradict=0.30,
    freshness_halflife_days=180.0,
    saturation_k=3.0,
)


def test_freshness_factor_decays_by_halflife():
    assert freshness_factor(0, 180) == 1.0
    assert freshness_factor(180, 180) == pytest.approx(0.5)
    assert freshness_factor(360, 180) == pytest.approx(0.25)
    assert freshness_factor(-5, 180) == 1.0  # future-dated treated as fresh
    assert freshness_factor(100, 0) == 1.0  # degenerate half-life guarded


def test_saturating_has_diminishing_returns():
    assert saturating(0, 3) == 0.0
    assert saturating(3, 3) == pytest.approx(0.5)
    assert saturating(9, 3) == pytest.approx(0.875)
    assert saturating(-2, 3) == 0.0
    assert saturating(5, 0) == 1.0


def test_confidence_is_clamped_and_explainable():
    b = score_confidence(
        source_rank=3, freshness_days=0, corroborated_by=5, contradicted_by=0, weights=W
    )
    assert 0.0 <= b.confidence <= 1.0
    raw = b.source_term + b.freshness_term + b.corroboration_term + b.contradiction_term
    assert b.confidence == pytest.approx(max(0.0, min(1.0, raw)))

    # each labelled term equals its independent spec contribution (not just the sum)
    assert b.source_term == pytest.approx(W.w_source * 3 / 3)
    assert b.freshness_term == pytest.approx(
        W.w_fresh * freshness_factor(0, W.freshness_halflife_days)
    )
    assert b.corroboration_term == pytest.approx(
        W.w_corroborate * saturating(5, W.saturation_k)
    )
    assert b.contradiction_term == pytest.approx(
        -W.w_contradict * saturating(0, W.saturation_k)
    )
    assert b.inputs == {
        "source_rank": 3.0,
        "freshness_days": 0.0,
        "corroborated_by": 5.0,
        "contradicted_by": 0.0,
    }


def test_source_term_pins_ladder_at_extremes():
    # rank 3 → full source weight; rank 0 → zero. Pins the /3 normalization.
    top = score_confidence(source_rank=3, freshness_days=10_000_000, weights=W)
    bottom = score_confidence(source_rank=0, freshness_days=10_000_000, weights=W)
    assert top.source_term == pytest.approx(W.w_source)
    assert bottom.source_term == 0.0


def test_clamp_holds_at_both_edges():
    # ceiling: weights whose positive terms sum above 1 must clamp to exactly 1.0
    ceil_w = ConfidenceWeights(
        w_source=0.6,
        w_fresh=0.5,
        w_corroborate=0.5,
        w_contradict=0.3,
        freshness_halflife_days=180.0,
        saturation_k=3.0,
    )
    hi = score_confidence(
        source_rank=3, freshness_days=0, corroborated_by=50, weights=ceil_w
    )
    raw = hi.source_term + hi.freshness_term + hi.corroboration_term + hi.contradiction_term
    assert raw > 1.0
    assert hi.confidence == 1.0

    # floor: heavy contradiction with a zero-rank, stale chunk must clamp to 0.0
    lo = score_confidence(
        source_rank=0, freshness_days=10_000_000, contradicted_by=50, weights=ceil_w
    )
    raw_lo = lo.source_term + lo.freshness_term + lo.corroboration_term + lo.contradiction_term
    assert raw_lo < 0.0
    assert lo.confidence == 0.0


def test_confidence_monotonic_in_each_input():
    base = dict(
        source_rank=2, freshness_days=30, corroborated_by=1, contradicted_by=1, weights=W
    )
    c0 = score_confidence(**base).confidence
    assert score_confidence(**{**base, "source_rank": 3}).confidence > c0
    assert score_confidence(**{**base, "freshness_days": 365}).confidence < c0
    assert score_confidence(**{**base, "corroborated_by": 6}).confidence > c0
    assert score_confidence(**{**base, "contradicted_by": 6}).confidence < c0


def test_fresh_corroborated_behaviour_beats_stale_contradicted_research():
    strong = score_confidence(
        source_rank=3, freshness_days=5, corroborated_by=5, contradicted_by=0, weights=W
    ).confidence
    weak = score_confidence(
        source_rank=1, freshness_days=900, corroborated_by=0, contradicted_by=5, weights=W
    ).confidence
    assert strong > 0.6
    assert weak < 0.2
    assert strong > weak


def test_deterministic_scoring():
    a = score_confidence(source_rank=2, freshness_days=10, weights=W).confidence
    b = score_confidence(source_rank=2, freshness_days=10, weights=W).confidence
    assert a == b


def test_from_settings_reads_each_config_weight():
    """The default-weights path must pull every named weight from config (doctrine
    #5: weights are config, not magic numbers) — not happen to match by luck."""
    from opsforge.config import get_settings

    s = get_settings()
    expected = ConfidenceWeights(
        w_source=s.confidence_w_source,
        w_fresh=s.confidence_w_fresh,
        w_corroborate=s.confidence_w_corroborate,
        w_contradict=s.confidence_w_contradict,
        freshness_halflife_days=float(s.confidence_freshness_halflife_days),
        saturation_k=float(s.confidence_saturation_k),
    )
    assert ConfidenceWeights.from_settings() == expected
    # and the no-weights call really uses them
    default = score_confidence(source_rank=2, freshness_days=10).confidence
    explicit = score_confidence(source_rank=2, freshness_days=10, weights=expected).confidence
    assert default == pytest.approx(explicit)
