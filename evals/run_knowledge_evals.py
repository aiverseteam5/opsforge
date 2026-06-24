"""Knowledge-plane eval harness (M7.3) — THE MEASUREMENT GATE.

M7.2 made corroboration un-spoofable (safety). This makes the plane's intelligence
MEASURABLE: three golden sets with KNOWN-correct answers, a runner that scores the
*current* reconciliation / confidence / precedence path against them, and a durable
scorecard so a before/after diff across an intelligence change is mechanical —
"detector swap moved contradiction accuracy 0.83 -> 0.91, calibration unchanged."

Golden sets live in evals/knowledge/*.yaml:
  - contradiction.yaml : known reconciliation outcome per resolution path.
  - confidence.yaml    : known confidence band + monotonic ordering (folds in the
                         M7.2 disjointness/corroboration calibration cases).
  - precedence.yaml     : the precedence ladder + disposition directions.

Detector modes (selectable, so a swap is scored both ways):
  - scripted : replay the relations the fixture declares -> scores the deterministic
               ENGINE in isolation (confidence + precedence are pure, so EXACT).
  - lexical  : the keyless LexicalDetector -> a deterministic detector FLOOR.
  - llm      : the keyed LLMDetector -> the real-LLM RATE (needs a provider key).

Run:
  python evals/run_knowledge_evals.py            # scripted + lexical
  python evals/run_knowledge_evals.py --llm      # also the keyed real-LLM rate
  python evals/run_knowledge_evals.py --out evals/scorecards/knowledge_baseline.json

Needs the Compose DB up (reconciliation is DB-backed): docker compose up -d db migrate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for sub in ("server", "tests"):
    p = str(_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from opsforge.confidence import score_confidence  # noqa: E402
from opsforge.dispositions import declare_disposition  # noqa: E402
from opsforge.findings import list_findings  # noqa: E402
from opsforge.knowledge import (  # noqa: E402
    PendingChunk,
    ProvenanceEnvelope,
    freshness_days,
    get_chunks,
)
from opsforge.knowledge import store_chunks as _store_chunks  # noqa: E402
from opsforge.reconcile import (  # noqa: E402
    ClaimRelation,
    FunctionDetector,
    LexicalDetector,
    reconcile_process,
)

# Fixed clock so freshness (and therefore every score) is deterministic.
AS_OF = datetime(2026, 6, 21, tzinfo=UTC)

# Confidence bands. LOW is below the M6.5 gate (0.5); HIGH is clear of it. The
# golden cases are constructed to land unambiguously inside a band, not on an edge.
LOW_MAX = 0.45
HIGH_MIN = 0.60

GOLDEN_DIR = _ROOT / "evals" / "knowledge"
SETS = ("contradiction", "confidence", "precedence")


def _scoring_config() -> dict[str, Any]:
    """The pure-scoring constants the engine read at run time. Recorded in the
    scorecard so a before/after diff fails LOUDLY if a weight, the freshness
    half-life, the saturation constant, the staleness window, or a source rank
    drifted — otherwise a config change could silently move a band-edge golden
    case while the scorecard reports 'unchanged'."""
    from opsforge.config import get_settings
    from opsforge.knowledge import SOURCE_RANK

    s = get_settings()
    return {
        "w_source": s.confidence_w_source,
        "w_fresh": s.confidence_w_fresh,
        "w_corroborate": s.confidence_w_corroborate,
        "w_contradict": s.confidence_w_contradict,
        "freshness_halflife_days": s.confidence_freshness_halflife_days,
        "saturation_k": s.confidence_saturation_k,
        "reconcile_staleness_days": s.reconcile_staleness_days,
        "source_ranks": dict(sorted(SOURCE_RANK.items())),
    }


def _band(conf: float) -> str:
    if conf < LOW_MAX:
        return "low"
    if conf >= HIGH_MIN:
        return "high"
    return "medium"


def _scripted_detector(relations: list[dict], ids: list[uuid.UUID]) -> FunctionDetector:
    """Replay the fixture's declared relations — isolates the engine from detection."""
    rels = [ClaimRelation(ids[r["a"]], ids[r["b"]], r["rel"]) for r in relations]

    async def fn(_chunks):
        return rels

    return FunctionDetector(fn)


def _llm_detector():
    from opsforge.config import get_settings
    from opsforge.gateway import LiteLLMGateway
    from opsforge.reconcile import LLMDetector

    return LLMDetector(LiteLLMGateway(), get_settings().model)


def _detector(mode: str, relations: list[dict], ids: list[uuid.UUID]):
    if mode == "scripted":
        return _scripted_detector(relations, ids)
    if mode == "lexical":
        return LexicalDetector()
    if mode == "llm":
        return _llm_detector()
    raise ValueError(f"unknown detector mode: {mode}")  # 'production' resolved async in run_case


async def _seed(org: str, pk: str, case: dict, as_of: datetime) -> list[uuid.UUID]:
    pending = []
    for spec in case["chunks"]:
        observed = as_of - timedelta(days=int(spec.get("age_days", 0)))
        env = ProvenanceEnvelope(
            source_kind=spec["kind"],
            source_ref=spec["ref"],
            observed_at=observed,
            ingested_at=as_of,
        )
        pending.append(PendingChunk(content=spec["content"], envelope=env, process_key=pk))
    ids = await _store_chunks(org, pending)
    disposition = case.get("disposition")
    if disposition in ("descriptive", "prescriptive"):
        await declare_disposition(
            org_id=org, process_key=pk, disposition=disposition, rationale="eval fixture"
        )
    return ids


def _check_findings(result, expect: dict) -> dict[str, bool]:
    # A resolution case declares its COMPLETE expected finding multiset under
    # `findings` (gap included where the engine emits it). We check each declared
    # count AND, strictly, that NO un-declared finding kind appears — so an
    # over-emitting regression (a spurious gap/stale/etc.) cannot pass unseen.
    if "findings" not in expect:
        return {}
    checks: dict[str, bool] = {}
    expected = expect["findings"] or {}
    for kind, want in expected.items():
        checks[f"finding:{kind}=={want}"] = result.findings_by_kind.get(kind, 0) == want
    extra = sorted(k for k, n in result.findings_by_kind.items() if n and k not in expected)
    checks["no_unexpected_findings"] = not extra
    if "superseded" in expect:
        checks["superseded"] = result.superseded == expect["superseded"]
    if "scored" in expect:
        checks["scored"] = result.scored == expect["scored"]
    return checks


def _check_winner(findings, expect: dict, ids: list[uuid.UUID]) -> dict[str, bool]:
    if "winner" not in expect:
        return {}
    want = str(ids[expect["winner"]])
    role = {"drift": "winner_chunk", "violation": "standard_chunk"}
    got = None
    for f in findings:
        if f.kind in role:
            got = f.detail.get(role[f.kind])
            break
    return {"winner": got == want}


def _check_confidence(
    chunks_by_idx: dict[int, Any], expect: dict, as_of: datetime
) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    for idx, want_band in (expect.get("confidence_bands") or {}).items():
        c = chunks_by_idx.get(int(idx))
        ok = c is not None and _band(float(c.confidence)) == want_band
        checks[f"band[{idx}]={want_band}"] = ok
    for idx in expect.get("equals_solo") or []:
        c = chunks_by_idx.get(int(idx))
        solo = score_confidence(
            source_rank=c.source_rank,
            freshness_days=freshness_days(c.observed_at, as_of),
            corroborated_by=0,
            contradicted_by=0,
        ).confidence
        checks[f"equals_solo[{idx}]"] = c is not None and abs(float(c.confidence) - solo) < 1e-9
    if "monotonic_desc" in expect:
        seq = [float(chunks_by_idx[int(i)].confidence) for i in expect["monotonic_desc"]]
        checks["monotonic_desc"] = all(a > b for a, b in zip(seq, seq[1:], strict=False))
    if "monotonic_asc" in expect:
        seq = [float(chunks_by_idx[int(i)].confidence) for i in expect["monotonic_asc"]]
        checks["monotonic_asc"] = all(a < b for a, b in zip(seq, seq[1:], strict=False))
    return checks


async def run_case(
    org: str, set_name: str, case: dict, *, mode: str = "scripted", detector=None, as_of=AS_OF
) -> dict[str, Any]:
    """Seed one golden case, reconcile it with the chosen detector, and score the
    outcome against the case's known-correct answer. Returns per-check booleans."""
    pk = f"{set_name}:{case['id']}"
    ids = await _seed(org, pk, case, as_of)
    if detector is not None:
        det = detector
    elif mode == "production":
        # The EXACT production entry point — resolves the workspace's active vault provider
        # (or the dev-env fallback / lexical floor). This is what the measured-ship gate scores.
        from opsforge.reconcile import configured_detector

        det = await configured_detector(org)
    else:
        det = _detector(mode, case.get("relations", []), ids)
    result = await reconcile_process(org, pk, detector=det, as_of=as_of)

    expect = case.get("expect", {})
    checks: dict[str, bool] = {}
    checks.update(_check_findings(result, expect))
    if "winner" in expect or "gap_missing" in expect:
        findings = await list_findings(org, process_key=pk)
        if "winner" in expect:
            checks.update(_check_winner(findings, expect, ids))
        if "gap_missing" in expect:
            gap = next((f for f in findings if f.kind == "gap"), None)
            want_missing = expect["gap_missing"]
            checks["gap_missing"] = gap is not None and gap.detail.get("missing") == want_missing
    _conf_keys = ("confidence_bands", "equals_solo", "monotonic_desc", "monotonic_asc")
    if any(k in expect for k in _conf_keys):
        active = {c.id: c for c in await get_chunks(org, pk)}
        by_idx = {i: active[cid] for i, cid in enumerate(ids) if cid in active}
        checks.update(_check_confidence(by_idx, expect, as_of))

    # A case that produced ZERO checks scored nothing — that is a malformed fixture,
    # not a pass. Guard against vacuous all([]) == True (the harness's own no-false-pass).
    return {"id": case["id"], "checks": checks, "passed": bool(checks) and all(checks.values())}


def _load_set(set_name: str) -> list[dict]:
    data = yaml.safe_load((GOLDEN_DIR / f"{set_name}.yaml").read_text(encoding="utf-8"))
    return data["cases"]


async def score_set(
    org: str, set_name: str, *, mode: str = "scripted", detector=None, as_of=AS_OF
) -> dict[str, Any]:
    cases = _load_set(set_name)
    results = [
        await run_case(org, set_name, c, mode=mode, detector=detector, as_of=as_of)
        for c in cases
    ]
    passed = [r for r in results if r["passed"]]
    failed = [r["id"] for r in results if not r["passed"]]
    return {
        "set": set_name,
        "mode": mode,
        "total": len(results),
        "passed": len(passed),
        "accuracy": round(len(passed) / len(results), 4) if results else 0.0,
        "failed_cases": failed,
        "cases": results,
    }


async def score_provider(
    *, provider: str, model: str, api_key: str | None = None, api_base: str | None = None,
    baseline_path: str = "evals/scorecards/knowledge_baseline.json", as_of=AS_OF,
) -> dict[str, Any]:
    """The MEASURED PROMOTION GATE (M7.6 Job A): score a candidate {provider, model,
    credential} against the M7.3 contradiction golden set and report whether it HOLDS the
    saved real-LLM baseline. A workspace may only promote a provider to its detector if
    `holds` is true — provider choice is a measured decision, not a vibe. Runs in a
    throwaway org so it never disturbs real workspace knowledge."""
    from opsforge.gateway import LiteLLMGateway
    from opsforge.reconcile import LLMDetector

    detector = LLMDetector(LiteLLMGateway(api_key=api_key, api_base=api_base), model)
    org = str(uuid.uuid4())
    try:
        card = await score_set(org, "contradiction", detector=detector, as_of=as_of)
    finally:
        await _cleanup(org)
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    # Missing baseline → demand a perfect score (conservative); but a genuinely-recorded
    # 0.0 must stay 0.0, not be collapsed to 1.0 by `or` (0.0 is falsy). Distinguish them.
    recorded = _acc(baseline, "contradiction", "llm")
    base = 1.0 if recorded is None else recorded
    return {
        "provider": provider, "model": model,
        "contradiction_accuracy": card["accuracy"], "baseline": base,
        "holds": card["accuracy"] >= base, "failed_cases": card["failed_cases"],
    }


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in (
            "reconciliations",
            "findings",
            "validated_processes",
            "knowledge_chunks",
            "process_dispositions",
        ):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


async def run_all(
    *, modes: tuple[str, ...] = ("scripted", "lexical"), as_of=AS_OF
) -> dict[str, Any]:
    """Score every golden set under every requested detector mode. Each set runs in
    a throwaway org that is cleaned up afterward. Returns the full scorecard."""
    scorecard: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "bands": {"low_max": LOW_MAX, "high_min": HIGH_MIN},
        "scoring_config": _scoring_config(),
        "sets": {},
    }
    for set_name in SETS:
        # confidence & precedence are pure-engine metrics: only the scripted
        # (deterministic) mode is meaningful. The contradiction set is detector-
        # bearing, so it is scored under every requested mode.
        set_modes = modes if set_name == "contradiction" else ("scripted",)
        scorecard["sets"][set_name] = {}
        for mode in set_modes:
            org = str(uuid.uuid4())
            try:
                card = await score_set(org, set_name, mode=mode, as_of=as_of)
                scorecard["sets"][set_name][mode] = card
            finally:
                await _cleanup(org)
    return scorecard


def _render_md(scorecard: dict[str, Any]) -> str:
    lines = ["# Knowledge-plane eval scorecard", "", f"as_of: `{scorecard['as_of']}`", ""]
    lines.append("| set | mode | accuracy | passed/total | failed cases |")
    lines.append("|---|---|---|---|---|")
    for set_name, modes in scorecard["sets"].items():
        for mode, r in modes.items():
            failed = ", ".join(r["failed_cases"]) or "—"
            score = f"{r['accuracy']:.4f}"
            count = f"{r['passed']}/{r['total']}"
            lines.append(f"| {set_name} | {mode} | {score} | {count} | {failed} |")
    return "\n".join(lines) + "\n"


def _acc(card: dict[str, Any], set_name: str, mode: str):
    return card.get("sets", {}).get(set_name, {}).get(mode, {}).get("accuracy")


def ship_verdict(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """The measured-ship decision (M7.4 §3): the production-wired contradiction
    score must HOLD the saved real-LLM baseline, and the deterministic confidence +
    precedence sets must be UNCHANGED (if they move, something leaked out of the
    propose/dispose boundary)."""
    base_contra = _acc(baseline, "contradiction", "llm")
    cur_contra = _acc(current, "contradiction", "production")
    if cur_contra is None:  # no production run (no key) → fall back to the llm column
        cur_contra = _acc(current, "contradiction", "llm")
    contra_ok = base_contra is not None and cur_contra is not None and cur_contra >= base_contra
    conf_ok = _acc(current, "confidence", "scripted") == _acc(baseline, "confidence", "scripted")
    prec_ok = _acc(current, "precedence", "scripted") == _acc(baseline, "precedence", "scripted")
    ship = bool(contra_ok and conf_ok and prec_ok)
    rationale = (
        f"contradiction {'HELD' if contra_ok else 'REGRESSED'} "
        f"(production {cur_contra} vs baseline llm {base_contra}), "
        f"confidence {'unchanged' if conf_ok else 'CHANGED'}, "
        f"precedence {'unchanged' if prec_ok else 'CHANGED'} "
        f"→ {'SHIP' if ship else 'HOLD — diagnose the wiring gap'}"
    )
    return {
        "ship": ship,
        "contradiction": {
            "baseline_llm": base_contra,
            "production": cur_contra,
            "holds": contra_ok,
        },
        "confidence_unchanged": conf_ok,
        "precedence_unchanged": prec_ok,
        "rationale": rationale,
    }


def _render(scorecard: dict[str, Any]) -> str:
    md = _render_md(scorecard)
    if "ship_decision" in scorecard:
        md += "\n## Ship decision\n\n" + scorecard["ship_decision"]["rationale"] + "\n"
    return md


async def _amain(use_llm: bool, use_production: bool, out: str | None, compare: str | None) -> int:
    modes = ("scripted", "lexical")
    if use_llm:
        modes += ("llm",)
    if use_production:
        modes += ("production",)
    scorecard = await run_all(modes=modes)

    if compare:
        baseline = json.loads(Path(compare).read_text(encoding="utf-8"))
        scorecard["ship_decision"] = ship_verdict(scorecard, baseline)

    print(_render(scorecard))
    if out:
        outp = Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(scorecard, indent=2, sort_keys=True), encoding="utf-8")
        outp.with_suffix(".md").write_text(_render(scorecard), encoding="utf-8")
        print(f"[scorecard written to {outp} and {outp.with_suffix('.md')}]")

    # Exit non-zero if a DETERMINISTIC (scripted) set regressed, or a compare was
    # requested and the ship gate did not hold.
    scripted_ok = all(
        modes_["scripted"]["accuracy"] == 1.0
        for modes_ in scorecard["sets"].values()
        if "scripted" in modes_
    )
    ship_ok = scorecard.get("ship_decision", {}).get("ship", True)
    return 0 if (scripted_ok and ship_ok) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run knowledge-plane golden evals")
    parser.add_argument("--llm", action="store_true", help="also score the keyed real-LLM rate")
    parser.add_argument(
        "--production",
        action="store_true",
        help="also score the production-wired path (configured_detector)",
    )
    parser.add_argument(
        "--compare", default="", help="baseline scorecard JSON to diff for the ship decision"
    )
    parser.add_argument(
        "--out", default="", help="write the scorecard JSON (+ .md) to this path"
    )
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_amain(args.llm, args.production, args.out or None, args.compare or None))


if __name__ == "__main__":
    raise SystemExit(main())
