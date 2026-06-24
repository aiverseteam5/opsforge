"""M7.2 live proof — provenance-disjoint corroboration, end to end with the REAL
LLM detector.

Run against the live Compose DB (superuser DSN, localhost:5432). The real LLM
detector PROPOSES agreement among chunks; the deterministic engine DISPOSES — and
M7.2 means it grants confidence lift only for provenance-DISJOINT agreement.

  Phase 1: one runbook split into four co-chunks (one source_ref → one root). The
           model asserts they agree; the engine grants ZERO lift (corroborated_by=0,
           confidence == solo). Duplication is not corroboration.
  Phase 2: add ONE genuinely separate source (a wiki page) that also agrees. Now the
           runbook chunks gain exactly ONE distinct corroborating root and confidence
           lifts. Ten more copies of the runbook still would not.

Usage (from repo root):
  PYTHONPATH=server PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/m72_live_proof.py
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
    """Load OPENAI_API_KEY / OPSFORGE_MODEL from .env without printing secrets."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
# Tests/app point at the superuser DSN on localhost for a host-run script.
os.environ.setdefault(
    "OPSFORGE_DATABASE_URL",
    "postgresql+psycopg://opsforge:opsforge@localhost:5432/opsforge",
)
os.environ.setdefault("OPSFORGE_FERNET_KEY", "")

from opsforge.knowledge import (  # noqa: E402
    PendingChunk,
    ProvenanceEnvelope,
    get_chunks,
    store_chunks,
)
from opsforge.reconcile import configured_detector, reconcile_process  # noqa: E402

NOW = datetime.now(UTC)


def _env(kind: str, ref: str, observed: datetime) -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        source_kind=kind, source_ref=ref, observed_at=observed, ingested_at=NOW
    )


async def _show(org: str, pk: str, label: str) -> None:
    print(f"\n--- {label} ---")
    for c in sorted(await get_chunks(org, pk), key=lambda c: c.source_ref):
        conf = "—" if c.confidence is None else f"{float(c.confidence):.3f}"
        roots = ", ".join(c.corroborating_roots) or "(none)"
        print(
            f"  [{c.source_ref:>16}] root={c.provenance_root!r:>18} "
            f"corrob_by={c.corroborated_by} conf={conf}  via=[{roots}]  "
            f"“{c.content[:42]}”"
        )


async def main() -> None:
    org, pk = str(uuid.uuid4()), "deploy-rollback"
    detector = await configured_detector()  # M7.6: now async (per-workspace vault lookup)
    print(f"detector = {type(detector).__name__}  (real LLM when keyed)")

    # Phase 1 — ONE runbook, four co-chunks. All share source_ref → one root.
    runbook_ref = "doc://runbook"
    observed = NOW - timedelta(days=10)
    runbook = [
        "To roll back a deploy, first drain the node from the load balancer.",
        "After draining, redeploy the previous image tag to the node.",
        "Then re-add the node to the load balancer once health checks pass.",
        "Finally, confirm the rollback by watching error rates return to baseline.",
    ]
    await store_chunks(
        org,
        [PendingChunk(content=t, envelope=_env("document", runbook_ref, observed), process_key=pk)
         for t in runbook],
    )
    await reconcile_process(org, pk, detector=detector)
    await _show(org, pk, "Phase 1: one runbook split into 4 chunks (one provenance root)")
    p1 = await get_chunks(org, pk)
    print(
        f"  => max corroborated_by = {max(c.corroborated_by or 0 for c in p1)} "
        f"(the model may assert many agreements; duplication lifts NOTHING)"
    )

    # Phase 2 — add ONE genuinely separate source agreeing with the same process.
    await store_chunks(
        org,
        [PendingChunk(
            content="Deploy rollback: drain the node, redeploy the prior image, "
                    "rejoin the LB after health checks, and verify error rates recover.",
            envelope=_env("behaviour", "wiki://oncall-notes", NOW - timedelta(days=2)),
            process_key=pk,
        )],
    )
    await reconcile_process(org, pk, detector=detector)
    await _show(org, pk, "Phase 2: + one independent source (wiki://oncall-notes)")
    p2 = {c.source_ref: c for c in await get_chunks(org, pk)}
    doc = next(c for ref, c in p2.items() if ref == runbook_ref)
    print(
        f"  => runbook chunks now corroborated_by={doc.corroborated_by} "
        f"via {doc.corroborating_roots} — exactly ONE distinct disjoint root."
    )

    # cleanup
    from sqlalchemy import text

    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("findings", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})
    print("\n[cleaned up demo org]")


if __name__ == "__main__":
    asyncio.run(main())
