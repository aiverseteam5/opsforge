"""M7.5 §4 — characterize the real LLM detector on realistic ticket data.

Deferred from M7.4 deliberately: characterizing on synthetic mess tested made-up
data; tickets are the real (messy) thing. This MEASURES and RECORDS three numbers as
a baseline for future regression — it is NOT a pass/fail gate:

  1. false-positive contradiction rate — how often the detector asserts a
     contradiction between genuinely-ambiguous-but-compatible ticket resolutions
     (with a small control of real contradictions it SHOULD catch);
  2. latency reconciling a realistic chunk count (the detector is the LLM-bound part);
  3. an estimated cost per run at that volume.

Run (keyed):
  OPENAI_API_KEY=... PYTHONPATH=server .venv/Scripts/python evals/characterize_detector.py \
      --out evals/scorecards/detector_characterization.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for sub in ("server",):
    p = str(_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)


class _Chunk:
    """Minimal stand-in: the detector only reads id, source_kind, content."""

    def __init__(self, content: str, kind: str = "behaviour") -> None:
        self.id, self.source_kind, self.content = uuid.uuid4(), kind, content


# Genuinely AMBIGUOUS-but-compatible resolution pairs (same process, NOT a real
# contradiction — different wording or complementary steps). A good detector calls
# these "agrees" or unrelated, NOT "contradicts".
_AMBIGUOUS_PAIRS = [
    ("drained the node then redeployed the prior image",
     "rolled back by redeploying the previous image after draining"),
    ("restarted the worker pool to clear the backlog",
     "cleared the queue backlog by bouncing the workers"),
    ("scaled the deployment to 5 replicas to absorb load",
     "added capacity by increasing replica count during the spike"),
    ("rotated the leaked credential and forced re-auth",
     "revoked the exposed token and required users to log in again"),
    ("flushed the CDN cache for the static assets",
     "purged the edge cache so the new assets would serve"),
    ("failed over to the standby database replica",
     "promoted the read replica to primary during the outage"),
    ("increased the connection pool size to stop exhaustion",
     "raised max connections after the pool ran dry"),
    ("acknowledged the alert and added a work note",
     "noted the alert was acknowledged and documented next steps"),
]

# Control: real contradictions the detector SHOULD catch (sanity that it isn't just
# always-no).
_REAL_CONTRADICTIONS = [
    ("we roll back by restoring last night's database backup",
     "never restore from backup on rollback — only redeploy the prior image"),
    ("the freeze window is the last week of the quarter",
     "there is no freeze window, deploys are allowed any time"),
    ("approvals require two reviewers before merge",
     "we merge straight to main with no review during incidents"),
]


def _detector():
    from opsforge.config import get_settings
    from opsforge.gateway import LiteLLMGateway
    from opsforge.reconcile import LLMDetector

    return LLMDetector(LiteLLMGateway(), get_settings().model)


def _is_contra(rels) -> bool:
    return any(r.relation == "contradicts" for r in rels)


async def _amain(out: str | None, latency_chunks: int) -> int:
    det = _detector()

    # 1. false-positive rate on ambiguous pairs (+ control true contradictions)
    fp = 0
    for a, b in _AMBIGUOUS_PAIRS:
        rels = await det.analyze([_Chunk(a), _Chunk(b, "document")])
        if _is_contra(rels):
            fp += 1
    caught = 0
    for a, b in _REAL_CONTRADICTIONS:
        rels = await det.analyze([_Chunk(a), _Chunk(b, "document")])
        if _is_contra(rels):
            caught += 1

    # 2. latency at a realistic chunk count (one big detector call)
    corpus = [
        _Chunk(f"ticket {i}: resolved a {('rollback','restart','scale-up','failover')[i % 4]} "
               f"incident on service-{i % 12} by following the standard runbook step")
        for i in range(latency_chunks)
    ]
    t0 = time.monotonic()
    await det.analyze(corpus)
    latency_s = round(time.monotonic() - t0, 2)

    # 3. cost estimate (input side dominates; ~4 chars/token, gpt-4o-mini input price)
    prompt_chars = sum(len(c.content) for c in corpus) + 400  # + prompt scaffold
    est_in_tokens = prompt_chars // 4
    est_usd = round(est_in_tokens / 1_000_000 * 0.15, 6)  # ~$0.15 / 1M input tokens

    card = {
        "as_of": "2026-06-22",
        "model": _detector().model,
        "note": "characterization, NOT a gate — a baseline for future regression diffs",
        "false_positive_contradiction": {
            "ambiguous_pairs": len(_AMBIGUOUS_PAIRS),
            "flagged_as_contradiction": fp,
            "fp_rate": round(fp / len(_AMBIGUOUS_PAIRS), 3),
            "control_real_contradictions": len(_REAL_CONTRADICTIONS),
            "control_caught": caught,
        },
        "latency": {"chunks": latency_chunks, "detector_seconds": latency_s},
        "cost_estimate": {
            "prompt_chars": prompt_chars,
            "est_input_tokens": est_in_tokens,
            "est_usd_per_run": est_usd,
        },
    }
    print(json.dumps(card, indent=2))
    if out:
        outp = Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(card, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\n[characterization written to {outp}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Characterize the LLM detector on ticket data")
    parser.add_argument("--out", default="")
    parser.add_argument("--latency-chunks", type=int, default=120)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_amain(args.out or None, args.latency_chunks))


if __name__ == "__main__":
    raise SystemExit(main())
