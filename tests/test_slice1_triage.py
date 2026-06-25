"""Slice 1 — learn-the-operation + validate-the-signal.

S1.0: the service-health-triage corpus is a REALISTIC TEST-DATA operation the machinery LEARNS
(domain-neutral): ingest the corpus → reconcile → generate the validated process; the agent
later consults it via kb.process for "what to check first". Real provenance (observed_at = the
file's authored date). Deterministic here (hash embedder + the mechanical OutlineDrafter); the
real LLM drafter/detector is exercised in the live proof.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fake_mcp import server_command
from sqlalchemy import text

from opsforge import knowledge_tools as kt
from opsforge.db import scope_to_org, session_factory
from opsforge.ingest import hash_embedder, ingest_directory
from opsforge.processes import OutlineDrafter, generate_process
from opsforge.reconcile import LexicalDetector, reconcile_process

pytestmark = pytest.mark.usefixtures("db_required")

_CORPUS = Path(__file__).resolve().parent.parent / "corpus" / "service-health-triage"
_PK = "service-health-triage"
RID = uuid.uuid4()


async def _cleanup(org):
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("validated_processes", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def _count_doc_chunks(org, pk):
    """Count a process's DOCUMENT-family chunks (what a commission re-ingests) — the deterministic
    signal for the idempotency assertion (independent of the embedder/drafter)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM knowledge_chunks WHERE org_id=:o AND process_key=:p "
                    "AND source_kind IN ('document','research')"
                ),
                {"o": org, "p": pk},
            )
        ).scalar_one()


async def test_corpus_learns_into_a_validated_process():
    org = str(uuid.uuid4())
    try:
        # LEARN: ingest the corpus (real provenance) → reconcile → generate validated process
        summary = await ingest_directory(_CORPUS, org_id=org, embedder=hash_embedder())
        assert summary["files"] >= 2 and summary["chunks"] >= 3

        await reconcile_process(org, _PK, detector=LexicalDetector())
        await generate_process(org, _PK, drafter=OutlineDrafter())

        # the agent's read tool returns the learned process: ordered steps + per-step confidence
        proc = await kt._process(org, {"process_key": _PK}, RID)
        assert proc["found"] is True
        assert len(proc["steps"]) >= 3
        assert all("text" in st for st in proc["steps"])
        assert any(st.get("confidence") is not None for st in proc["steps"])

        # real provenance: observed_at = the files' authored dates (not ingest time); TEST DATA
        chunks = await kt._search_knowledge(org, {"process_key": _PK}, RID)
        refs = {c["source_ref"].split("/")[-1] for c in chunks["chunks"]}
        assert "service-health-triage-runbook.md" in refs
        ages = {str(c.get("age_days")) for c in chunks["chunks"]}
        assert ages  # freshness computed from observed_at
        blob = " ".join(c["content"] for c in chunks["chunks"]).lower()
        assert "test data" in blob  # honesty bar: corpus is labelled
        # the learned process teaches the validate-the-signal step (domain content, not in code)
        assert "monitoring" in blob and ("stale" in blob or "false" in blob)
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# S1.1 — the contract-faithful FAKE monitoring connector (ground-truth read)
# --------------------------------------------------------------------------- #
async def test_fake_monitoring_connector_reads_up_and_is_reversible():
    from opsforge.config import DEFAULT_ORG_ID
    from opsforge.connectors import load_connector, open_connector

    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM connectors WHERE kind='monitoring'"))
        cid = (await s.execute(
            text("INSERT INTO connectors (org_id,name,kind,transport,endpoint,tool_allowlist,"
                 "environment,status) VALUES (:o,'monitoring (TEST)','monitoring','stdio',:e,"
                 "CAST(:a AS jsonb),'prod','healthy') RETURNING id"),
            {"o": DEFAULT_ORG_ID, "e": server_command("monitoring"),
             "a": json.dumps(["get_service_status", "set_pull_interval", "verify_credential"])},
        )).scalar_one()
    try:
        connector = await load_connector(cid, DEFAULT_ORG_ID)
        async with open_connector(connector) as cs:
            # ground-truth read: monitor reports the service UP (so a 'down' ticket = false alert)
            status = await cs.call("monitoring.get_service_status", {"service": "payment-svc"})
            assert status["status"] == "up" and "TEST DATA" in status["source"]
            # the config-change write is reversible: set then restore via the same tool
            r1 = await cs.call("monitoring.set_pull_interval",
                               {"service": "payment-svc", "seconds": 30})
            assert r1["new_interval_seconds"] == 30
            r2 = await cs.call("monitoring.set_pull_interval",
                               {"service": "payment-svc", "seconds": r1["old_interval_seconds"]})
            assert r2["new_interval_seconds"] == r1["old_interval_seconds"]  # rollback restores
    finally:
        async with session_factory().begin() as s:
            await s.execute(text("DELETE FROM connectors WHERE id=:i"), {"i": cid})


# --------------------------------------------------------------------------- #
# S1.2/S1.3a — the triage skill pack + the additive manifest extension
# --------------------------------------------------------------------------- #
def test_triage_manifest_validates_and_extension_is_additive():
    from opsforge.skills import load_skill_dir

    root = Path(__file__).resolve().parent.parent / "skills"
    m, instructions = load_skill_dir(root / "triage")
    assert m.slug == "triage" and "event" in m.triggers
    # the NEW manifest fields
    assert m.charter and "validate" in m.charter.lower()
    assert len(m.knowledge_sources) == 1
    ks = m.knowledge_sources[0]
    assert ks.kind == "local_dir" and ks.process_key == "service-health-triage"
    # the read surface + the ONE gated reversible proposal (with a rollback)
    assert {t.tool for t in m.tools} >= {"kb.process", "monitoring.get_service_status"}
    prop = next(p for p in m.proposals if p.tool == "monitoring.set_pull_interval")
    assert prop.class_ == "reversible" and prop.rollback  # reversible + declared rollback
    assert instructions  # has INSTRUCTIONS.md
    # the extension is ADDITIVE — every existing builtin skill still validates
    for child in sorted(root.iterdir()):
        if (child / "skill.yaml").exists():
            load_skill_dir(child)  # must not raise


def test_triage_proposal_gates_deterministically():
    """The safety-critical invariant (no LLM): the triage skill's ONE proposed action — a config
    change to the un-vouched (prod-default) monitoring connector — GATES for a human via the
    deterministic boundary, even with high grounding + a rollback. Slice 1 is read-heavy."""
    from opsforge.policy import resolve_proposal
    from opsforge.skills import load_skill_dir, manifest_dump

    root = Path(__file__).resolve().parent.parent / "skills"
    manifest = manifest_dump(load_skill_dir(root / "triage")[0])
    high = {"low_confidence": False, "grounding_confidence": 0.9, "chunk_count": 3}
    # production=True models the un-vouched (prod-default) monitoring connector
    t = resolve_proposal(manifest, "monitoring.set_pull_interval", None,
                         grounding=high, production=True)
    assert t["auto_execute"] is False and t["state"] == "awaiting_approval"
    assert "production_gate" in t["rules"]
    assert t["has_rollback"] is True  # reversible-with-rollback, yet production still gates


def test_rollback_params_resolve_from_forward_state():
    """The triage rollback genuinely restores the PRIOR interval: ${params.service} and
    ${result.old_interval_seconds} resolve from the forward action's params + captured result, so
    an undo reverts to the captured value instead of misfiring with empty args. Opt-in: a
    token-free rollback (every existing skill) is returned unchanged, so the resolver can't
    regress them; missing forward state resolves to NOT-ok so the executor fails/skips closed."""
    from opsforge.actions import _resolve_rollback_params

    tmpl = {"service": "${params.service}", "seconds": "${result.old_interval_seconds}"}
    resolved, ok = _resolve_rollback_params(
        tmpl, {"service": "checkout-svc", "seconds": 60}, {"old_interval_seconds": 300})
    assert ok is True and resolved == {"service": "checkout-svc", "seconds": 300}
    # missing forward state (e.g. the forward call failed) → NOT fully resolved
    _, ok_missing = _resolve_rollback_params(tmpl, {"service": "x"}, {})
    assert ok_missing is False
    # token-free rollbacks (existing roll-forward skills) are untouched — no regression
    assert _resolve_rollback_params({"note": "roll forward"}, {}, {}) == (
        {"note": "roll forward"}, True)
    assert _resolve_rollback_params({}, {}, {}) == ({}, True)


# --------------------------------------------------------------------------- #
# S1.3b — commission: install + LEARN from the manifest's declared sources (M6), in order
# --------------------------------------------------------------------------- #
async def test_commission_learns_from_declared_sources(tmp_path):
    from opsforge.config import DEFAULT_ORG_ID
    from opsforge.worker import handle_commission

    pk = f"commission-test-{uuid.uuid4().hex[:8]}"
    slug = f"ctest-{uuid.uuid4().hex[:8]}"
    (tmp_path / "doc.md").write_text(
        f"---\nprocess_key: {pk}\nobserved_at: 2026-05-01\n---\n# Test triage runbook\n\n"
        "REALISTIC TEST DATA.\n\n## Step one\nCheck the monitoring status for the service.\n\n"
        "## Step two\nIf monitoring is up but the ticket says down, suspect a stale alert.\n",
        encoding="utf-8")
    manifest = {"schema": "opsforge/skill/v1", "slug": slug, "version": "0.1.0", "name": "t",
                "knowledge_sources": [{"kind": "local_dir", "ref": str(tmp_path),
                                       "process_key": pk}],
                "report": {"format": "rca_v1"}}
    async with session_factory().begin() as s:
        await scope_to_org(s, DEFAULT_ORG_ID)
        await s.execute(
            text("INSERT INTO skills (org_id,slug,version,manifest,instructions,source,enabled) "
                 "VALUES (:o,:slug,'0.1.0',CAST(:m AS jsonb),'','org',true)"),
            {"o": DEFAULT_ORG_ID, "slug": slug, "m": json.dumps(manifest)})
    try:
        # commission ingests the declared source + reconciles → a validated process exists (LEARN)
        await handle_commission({"org_id": DEFAULT_ORG_ID, "skill_slug": slug})
        proc = await kt._process(DEFAULT_ORG_ID, {"process_key": pk}, RID)
        assert proc["found"] is True and len(proc["steps"]) >= 1
        n_chunks = await _count_doc_chunks(DEFAULT_ORG_ID, pk)
        assert n_chunks >= 1
        # idempotent: a re-commission REPLACES this process's document set — it must NOT accumulate
        # duplicate chunks (which would double the learned process's steps). Count, don't just
        # check found (the prior weak assertion missed the duplication entirely).
        await handle_commission({"org_id": DEFAULT_ORG_ID, "skill_slug": slug})
        assert (await kt._process(DEFAULT_ORG_ID, {"process_key": pk}, RID))["found"] is True
        assert await _count_doc_chunks(DEFAULT_ORG_ID, pk) == n_chunks  # replaced, not duplicated
    finally:
        async with session_factory().begin() as s:
            await scope_to_org(s, DEFAULT_ORG_ID)
            await s.execute(text("DELETE FROM skills WHERE org_id=:o AND slug=:s"),
                            {"o": DEFAULT_ORG_ID, "s": slug})
            for t in ("validated_processes", "knowledge_chunks"):
                await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o AND process_key=:p"),
                                {"o": DEFAULT_ORG_ID, "p": pk})
