"""Deterministic confidence scoring — the formula half of the Truth Plane.

Doctrine #5: confidence is computed from observable evidence, never asserted by
the model. This module is pure (no I/O, no LLM), mirroring `policy.py`: the LLM
may propose reconciliations, but this formula scores them. Every score comes with
a breakdown so a human reading "0.42" can ask *why* and get a real answer — which
source, how fresh, how corroborated, how contradicted.

    confidence = clamp01(
        w_source      * (source_rank / 3)              # precedence ladder
      + w_fresh       * freshness_factor(age_days)     # decays with age
      + w_corroborate * saturating(corroborated_by)    # agreement, with diminishing returns
      - w_contradict  * saturating(contradicted_by)    # conflict, with diminishing returns
    )

Weights and the decay/saturation constants live in `config.py`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import get_settings

_MAX_SOURCE_RANK = 3  # behaviour=3 is the top of the precedence ladder


@dataclass(frozen=True)
class ConfidenceWeights:
    w_source: float
    w_fresh: float
    w_corroborate: float
    w_contradict: float
    freshness_halflife_days: float
    saturation_k: float

    @classmethod
    def from_settings(cls) -> ConfidenceWeights:
        s = get_settings()
        return cls(
            w_source=s.confidence_w_source,
            w_fresh=s.confidence_w_fresh,
            w_corroborate=s.confidence_w_corroborate,
            w_contradict=s.confidence_w_contradict,
            freshness_halflife_days=float(s.confidence_freshness_halflife_days),
            saturation_k=float(s.confidence_saturation_k),
        )


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """A score plus the contribution of each term, so it is fully explainable."""

    confidence: float
    source_term: float
    freshness_term: float
    corroboration_term: float
    contradiction_term: float  # already negative (it subtracts)
    inputs: dict[str, float]


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def freshness_factor(age_days: float, halflife_days: float) -> float:
    """1.0 at age 0, 0.5 at one half-life, → 0 as age grows. Negative ages
    (future-dated knowledge) are treated as fresh (1.0)."""
    if halflife_days <= 0:
        return 1.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / halflife_days)


def saturating(count: int | float, k: float) -> float:
    """0 at count 0, 0.5 at count k, → 1 as count grows. Diminishing returns so a
    flood of weak corroboration can't dominate. Negative counts clamp to 0."""
    if count <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    return 1.0 - 0.5 ** (count / k)


def score_confidence(
    *,
    source_rank: int,
    freshness_days: int,
    corroborated_by: int = 0,
    contradicted_by: int = 0,
    weights: ConfidenceWeights | None = None,
) -> ConfidenceBreakdown:
    """Score one chunk's confidence in [0,1] from its evidence, with a breakdown.
    Pure and deterministic — same inputs always give the same score."""
    w = weights or ConfidenceWeights.from_settings()

    source_term = w.w_source * (source_rank / _MAX_SOURCE_RANK)
    freshness_term = w.w_fresh * freshness_factor(freshness_days, w.freshness_halflife_days)
    corroboration_term = w.w_corroborate * saturating(corroborated_by, w.saturation_k)
    contradiction_term = -w.w_contradict * saturating(contradicted_by, w.saturation_k)

    raw = source_term + freshness_term + corroboration_term + contradiction_term
    return ConfidenceBreakdown(
        confidence=_clamp01(raw),
        source_term=source_term,
        freshness_term=freshness_term,
        corroboration_term=corroboration_term,
        contradiction_term=contradiction_term,
        inputs={
            "source_rank": float(source_rank),
            "freshness_days": float(freshness_days),
            "corroborated_by": float(corroborated_by),
            "contradicted_by": float(contradicted_by),
        },
    )
