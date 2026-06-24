"""M7.4 live proof — the REAL LLM detector is the production reconcile path, and
the M7.2 + M6.5 safety properties still hold with it live.

Three live checks against the Compose DB with `configured_detector()` (the actual
production entry — LLM when keyed):
  1. Multi-source reconcile: the real detector proposes relations over a mixed
     corpus; the run is RECORDED with detector='llm' (a degraded run would record
     'lexical_fallback').
  2. M7.2 live: one source restated several times — the LLM may assert agreement,
     but same-root duplication lifts confidence by ZERO.
  3. M6.5 live: a stale, low-rank corpus reconciled by the real detector still
     yields low grounding → a consequential action routes to the human gate.

Usage (from repo root):
  PYTHONPATH=server PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/m74_live_proof.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
os.environ.setdefault(
    "OPSFORGE_DATABASE_URL",
    "postgresql+psycopg://opsforge:opsforge@localhost:5432/opsforge",
)
os.environ.setdefault("OPSFORGE_FERNET_KEY", "")

from opsforge.agent import assemble_context  # noqa: E402
from opsforge.knowledge import (  # noqa: E402
    PendingChunk,
    ProvenanceEnvelope,
    get_chunks,
    store_chunks,
)
from opsforge.policy import resolve_proposal  # noqa: E402
from opsforge.reconcile import configured_detector, reconcile_process  # noqa: E402
from opsforge.reconciliations import latest_reconciliation  # noqa: E402

NOW = datetime.now(UTC)


def _env(kind, ref, age_days):
    return ProvenanceEnvelope(
        source_kind=kind, source_ref=ref,
        observed_at=NOW - timedelta(days=age_days), ingested_at=NOW,
    )


async def _store(org, pk, items):
    pending = [
        PendingChunk(content=c, envelope=_env(k, r, a), process_key=pk)
        for k, r, c, a in items
    ]
    await store_chunks(org, pending)


async def _cleanup(org):
    from sqlalchemy import text

    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("reconciliations", "findings", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def main() -> None:
    detector = await configured_detector()  # M7.6: now async (per-workspace vault lookup)
    print(f"production detector = {type(detector).__name__}  (LLM when keyed)\n")
    org = str(uuid.uuid4())
    try:
        # 1. multi-source reconcile, recorded as a real (non-degraded) run
        pk1 = "rollback"
        await _store(org, pk1, [
            ("behaviour", "obs://oncall", "drain the node then redeploy the prior image", 3),
            ("document", "doc://runbook", "drain node, redeploy image, verify health", 20),
            ("document", "doc://stale-sop", "rollback = restore last night's backup", 200),
        ])
        r1 = await reconcile_process(org, pk1, detector=detector)
        rec = await latest_reconciliation(org, pk1)
        print(f"[1] multi-source reconcile: detector recorded = {rec.detector!r}, "
              f"findings={sum(r1.findings_by_kind.values())} {dict(r1.findings_by_kind)}")

        # 2. M7.2 live — one source restated; LLM may agree, duplication lifts nothing
        pk2 = "dup"
        await _store(org, pk2, [
            ("behaviour", "obs://one-source", "we restart the worker pool in incidents", 5),
            ("behaviour", "obs://one-source", "the worker pool is restarted in incidents", 5),
            ("behaviour", "obs://one-source", "restart the worker pool every incident", 5),
        ])
        await reconcile_process(org, pk2, detector=detector)
        corr = max(c.corroborated_by or 0 for c in await get_chunks(org, pk2))
        print(f"[2] same-root duplication (LLM asserts agreement): max corroborated_by = {corr} "
              f"→ {'ZERO lift (M7.2 holds live)' if corr == 0 else 'LIFTED — BUG'}")

        # 3. M6.5 live — stale low-rank corpus → low grounding → gate fires
        pk3 = "stale"
        await _store(org, pk3, [
            ("research", "doc://old-a", "an old research hunch about restarting on failure", 400),
            ("research", "doc://old-b", "another stale note suggesting a restart", 380),
        ])
        await reconcile_process(org, pk3, detector=detector)
        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk3}, []
        )
        trace = resolve_proposal(
            {"proposals": [{"tool": "k.restart", "class": "reversible"}]},
            "k.restart", {"k.restart": "auto_with_notify"}, grounding=grounding,
        )
        gated = trace["state"] == "awaiting_approval" and "low_grounding_gate" in trace["rules"]
        print(f"[3] stale grounding (low_confidence={grounding['low_confidence']}): "
              f"action → {trace['state']} "
              f"→ {'gate FIRES (M6.5 holds live)' if gated else 'NOT gated — BUG'}")
    finally:
        await _cleanup(org)
        print("\n[cleaned up demo org]")


if __name__ == "__main__":
    asyncio.run(main())
