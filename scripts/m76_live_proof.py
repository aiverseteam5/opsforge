"""M7.6 consolidated live proof — run against the live Compose DB as the RESTRICTED
opsforge_app role (point OPSFORGE_DATABASE_URL at opsforge_app). Proves both jobs end to
end on the real schema, under real RLS:

  JOB B — identity-backed origin (the M7.5 residual, closed at the root):
    B1. a genuine pattern of 3 DISTINCT connector-VERIFIED identities overrides a document
        → real drift (behaviour-rank earned by verified, provenance-disjoint identity).
    B2. an unverified origin (the connector could not resolve it → no identity) is DEMOTED
        — it does not override the document ("seen, not yet a verified pattern").
    B3. the fail-OPEN hole (review F-A) is closed: a raw ticket that self-asserts an
        `origin_identity` field, with no connector-stamped id, is NOT verified → demoted.

  JOB A — the LLM as a per-workspace, vault-credentialed connector:
    A1. an ACTIVE vault binding resolves to an LLMDetector carrying THAT workspace's model
        + vaulted key (no `.env` involved) — multi-provider at the routing level.
    A2. a workspace with NO binding (production) falls to the keyless LEXICAL FLOOR — never
        a shared global key (per-workspace isolation).
    A3. the fail-CLOSED guard (review F-B): an active binding whose vaulted credential will
        not decrypt falls to the floor, NOT the ambient env key.
    A4. the MEASURED promotion gate: a non-holding scorecard is refused; a holding one
        promotes — provider choice is by the numbers, never a vibe.

Usage (restricted role):
  OPSFORGE_DATABASE_URL=postgresql+psycopg://opsforge_app:opsforge_app@localhost:5432/opsforge \
  PYTHONPATH=server PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/m76_live_proof.py

The OpenAI key + Fernet key are read from .env into the vault; the key VALUE is never
printed (only its presence is asserted).
"""

from __future__ import annotations

# ruff: noqa: E501  (a demo script with long ticket fixtures and print lines)
import asyncio
import os
import sys
import uuid
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
os.environ.setdefault(
    "OPSFORGE_DATABASE_URL", "postgresql+psycopg://opsforge_app:opsforge_app@localhost:5432/opsforge"
)

from sqlalchemy import text  # noqa: E402

from opsforge.config import get_settings  # noqa: E402
from opsforge.db import scope_to_org, session_factory  # noqa: E402
from opsforge.dispositions import declare_disposition  # noqa: E402
from opsforge.ingest import configured_embedder  # noqa: E402
from opsforge.knowledge import ProvenanceEnvelope, get_chunks, store_chunk  # noqa: E402
from opsforge.llm_providers import (  # noqa: E402
    active_config,
    promote_if_holds,
    propose_provider,
    set_active,
    store_scorecard,
)
from opsforge.reconcile import (  # noqa: E402
    ClaimRelation,
    FunctionDetector,
    LexicalDetector,
    LLMDetector,
    configured_detector,
    reconcile_process,
)
from opsforge.tickets import ingest_tickets  # noqa: E402

EMB = configured_embedder()


def _ticket(num, pk, origin, resolution, identity=None):
    """A resolved ticket. `identity` = the connector-VERIFIED directory id (None = the
    connector could not resolve it). In production the connector stamps assignment_group_id."""
    return {"number": num, "process_key": pk, "assignment_group": origin,
            "assignment_group_id": identity, "resolution": resolution,
            "resolved_at": "2026-06-10T00:00:00Z"}


async def _seed_doc(org, pk, content):
    env = ProvenanceEnvelope(source_kind="document", source_ref=f"doc://{pk}",
                             observed_at="2026-06-01T00:00:00Z", ingested_at="2026-06-01T00:00:00Z")
    return await store_chunk(org_id=org, content=content, envelope=env, process_key=pk)


def _pattern_vs_doc(beh_ids, doc_id):
    """Scripted detector: every behaviour chunk agrees; the first contradicts the document.
    The detector only PROPOSES relations — the deterministic identity gate disposes, which
    is exactly what this proof isolates (M7.4 already proved the real LLM detector live)."""
    want = set(beh_ids)

    async def fn(chunks):
        present = {c.id for c in chunks}
        bids = [c.id for c in chunks if c.id in want]
        rels = [ClaimRelation(bids[i], bids[j], "agrees")
                for i in range(len(bids)) for j in range(i + 1, len(bids))]
        if bids and doc_id in present:
            rels.append(ClaimRelation(bids[0], doc_id, "contradicts"))
        return rels

    return FunctionDetector(fn)


async def _wipe(org):
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("reconciliations", "findings", "validated_processes",
                  "knowledge_chunks", "process_dispositions", "llm_providers"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def prove_job_b() -> None:
    print("=== JOB B — identity-backed origin ===")
    org = str(uuid.uuid4())
    try:
        # B1. genuine pattern: 3 DISTINCT verified identities overriding a document
        pk = "rollback"
        ids = await ingest_tickets(org, [
            _ticket("INC1", pk, "sre-payments", "drain the node then redeploy the prior image", identity="grp-0001"),
            _ticket("INC2", pk, "sre-checkout", "drain the node then redeploy the prior image", identity="grp-0002"),
            _ticket("INC3", pk, "platform-oncall", "drain the node then redeploy the prior image", identity="grp-0003"),
        ], embedder=EMB)
        doc = await _seed_doc(org, pk, "rollback = restore last night's backup")
        await declare_disposition(org_id=org, process_key=pk, disposition="descriptive", rationale="r")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc))
        roots = {c.provenance_root for c in await get_chunks(org, pk) if c.source_kind == "behaviour"}
        print(f"  B1 verified 3-identity pattern: distinct verified roots={len(roots)} "
              f"drift={res.findings_by_kind.get('drift', 0)} "
              f"→ {'OVERRIDES the document (behaviour-rank earned)' if res.findings_by_kind.get('drift', 0) else 'UNEXPECTED'}")

        # B2. unverified origin (no connector identity) → demoted, no override
        pk2 = "cache"
        ids2 = await ingest_tickets(org, [
            _ticket("U1", pk2, "ghost-a", "flush the cache automatically", identity=None),
            _ticket("U2", pk2, "ghost-b", "flush the cache automatically", identity=None),
        ], embedder=EMB)
        doc2 = await _seed_doc(org, pk2, "cache flush must be a manual change ticket")
        await declare_disposition(org_id=org, process_key=pk2, disposition="descriptive", rationale="r")
        res2 = await reconcile_process(org, pk2, detector=_pattern_vs_doc(ids2, doc2))
        conf2 = [float(c.confidence) for c in await get_chunks(org, pk2) if c.source_kind == "behaviour"]
        print(f"  B2 unverified origin: drift={res2.findings_by_kind.get('drift', 0)} "
              f"max_conf={max(conf2):.2f} → {'DEMOTED, document NOT overridden' if not res2.findings_by_kind.get('drift', 0) and max(conf2) < 0.5 else 'UNEXPECTED'}")

        # B3. fail-OPEN closed (F-A): self-asserted origin_identity field is ignored
        pk3 = "deploy"
        raw = [
            {"number": "ATK1", "process_key": pk3, "assignment_group": "ghost-x", "origin_identity": "x1",
             "resolution": "drain the node then redeploy the prior image", "resolved_at": "2026-06-10T00:00:00Z"},
            {"number": "ATK2", "process_key": pk3, "assignment_group": "ghost-y", "origin_identity": "x2",
             "resolution": "drain the node then redeploy the prior image", "resolved_at": "2026-06-10T00:00:00Z"},
        ]
        ids3 = await ingest_tickets(org, raw, embedder=EMB)
        doc3 = await _seed_doc(org, pk3, "rollback = restore last night's backup")
        await declare_disposition(org_id=org, process_key=pk3, disposition="descriptive", rationale="r")
        res3 = await reconcile_process(org, pk3, detector=_pattern_vs_doc(ids3, doc3))
        roots3 = {c.provenance_root for c in await get_chunks(org, pk3) if c.source_kind == "behaviour"}
        print(f"  B3 self-asserted origin_identity (F-A): verified roots={roots3} "
              f"drift={res3.findings_by_kind.get('drift', 0)} "
              f"→ {'IGNORED — root None, demoted, no override' if roots3 == {None} and not res3.findings_by_kind.get('drift', 0) else 'UNEXPECTED'}")
    finally:
        await _wipe(org)


async def prove_job_a() -> None:
    print("\n=== JOB A — vault-credentialed per-workspace LLM connector ===")
    fernet_ok = bool(get_settings().fernet_key)
    key = os.environ.get("OPENAI_API_KEY")
    if not (fernet_ok and key):
        print(f"  [skip] needs OPSFORGE_FERNET_KEY ({'set' if fernet_ok else 'MISSING'}) and "
              f"OPENAI_API_KEY ({'set' if key else 'MISSING'}) in .env")
        return
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        # A1. active vault binding → LLMDetector with THAT workspace's model + vaulted key
        pid = await propose_provider(org_a, provider="openai", model="gpt-4o-mini",
                                     credential={"api_key": key})
        await set_active(org_a, pid)
        det_a = await configured_detector(org_a)
        ok_a = isinstance(det_a, LLMDetector) and det_a.model == "gpt-4o-mini" and det_a.gateway.api_key == key
        print(f"  A1 active binding resolves: {type(det_a).__name__} model={getattr(det_a, 'model', None)!r} "
              f"key_from_vault={det_a.gateway.api_key is not None if isinstance(det_a, LLMDetector) else False} "
              f"→ {'RESOLVED from vault (no .env)' if ok_a else 'UNEXPECTED'}")

        # A2. no binding under production → lexical floor (isolation, not a shared key)
        prev = get_settings().dev_llm_fallback
        object.__setattr__(get_settings(), "dev_llm_fallback", False)
        try:
            det_b = await configured_detector(org_b)
        finally:
            object.__setattr__(get_settings(), "dev_llm_fallback", prev)
        print(f"  A2 no-binding workspace (prod): {type(det_b).__name__} "
              f"→ {'LEXICAL FLOOR — A’s key did not leak to B' if isinstance(det_b, LexicalDetector) else 'UNEXPECTED'}")

        # A3. fail-CLOSED (F-B): active binding whose credential will not decrypt → floor
        org_c = str(uuid.uuid4())
        async with session_factory().begin() as s:
            await scope_to_org(s, org_c)
            await s.execute(text(
                "INSERT INTO llm_providers (org_id, provider, model, credential_enc, status) "
                "VALUES (:o, 'openai', 'gpt-4o-mini', :bad, 'active')"),
                {"o": org_c, "bad": b"not-a-valid-fernet-token"})
        object.__setattr__(get_settings(), "dev_llm_fallback", False)
        try:
            det_c = await configured_detector(org_c)
        finally:
            object.__setattr__(get_settings(), "dev_llm_fallback", prev)
        await _wipe(org_c)
        print(f"  A3 unresolvable credential (F-B): {type(det_c).__name__} "
              f"→ {'FAILS CLOSED to floor, not the ambient env key' if isinstance(det_c, LexicalDetector) else 'UNEXPECTED'}")

        # A4. the measured promotion gate
        org_g = str(uuid.uuid4())
        try:
            gid = await propose_provider(org_g, provider="openai", model="gpt-4o-mini",
                                         credential={"api_key": key})
            await store_scorecard(org_g, gid, {"contradiction_accuracy": 0.5, "baseline": 1.0, "holds": False})
            refused = await promote_if_holds(org_g, gid)
            none_active = await active_config(org_g) is None
            await store_scorecard(org_g, gid, {"contradiction_accuracy": 1.0, "baseline": 1.0, "holds": True})
            promoted = await promote_if_holds(org_g, gid)
            print(f"  A4 measured gate: failing-card promote={refused} (active={not none_active}); "
                  f"holding-card promote={promoted} "
                  f"→ {'GATED — only a holding measurement promotes' if (not refused and none_active and promoted) else 'UNEXPECTED'}")
        finally:
            await _wipe(org_g)
    finally:
        await _wipe(org_a)
        await _wipe(org_b)


async def main() -> None:
    print(f"[connected as role in DSN: {os.environ['OPSFORGE_DATABASE_URL'].split('://')[1].split(':')[0]}]\n")
    await prove_job_b()
    await prove_job_a()
    print("\n[done]")


if __name__ == "__main__":
    asyncio.run(main())
