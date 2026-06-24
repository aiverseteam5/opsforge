"""Phase B integration — the Confluence knowledge connector end to end on the existing
rails, against a contract-faithful fake confluence MCP server (no real endpoint):

  * connection test EXERCISES the credential — a wrong token → unhealthy (closes A2/F2);
  * real documents ingest with REAL provenance (source_ref = page URL, observed_at = the
    page's real last-modified, NOT ingest time);
  * the connector→ingest→reconcile MECHANISM produces a finding on the (FIXTURE) corpus;
  * the token never leaks into a chunk / health result.

The fixture inconsistency is a planted demonstration of the mechanism — NOT the Phase-B
"real aha" (that needs a real corpus and is honestly deferred).
"""

from __future__ import annotations

import json
import uuid

import pytest
from fake_mcp import server_command
from sqlalchemy import text

from opsforge.connectors import health_check, load_connector
from opsforge.dispositions import declare_disposition
from opsforge.findings import list_findings
from opsforge.ingest import configured_embedder
from opsforge.knowledge import get_chunks
from opsforge.knowledge_sources import ingest_knowledge_from_connector
from opsforge.reconcile import ClaimRelation, FunctionDetector, reconcile_process
from opsforge.security import encrypt

pytestmark = pytest.mark.usefixtures("db_required")

GOOD = "alice@acme.com:good-token"
ALLOW = ["list_documents", "verify_credential"]


async def _connector(org: str, token: str, extra: dict | None = None) -> dict:
    from opsforge.db import scope_to_org, session_factory

    creds = encrypt(json.dumps({"CONFLUENCE_TOKEN": token, **(extra or {})}))
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        cid = (await s.execute(
            text("INSERT INTO connectors (org_id,name,kind,transport,endpoint,"
                 "credentials_enc,tool_allowlist,status) VALUES "
                 "(:o,'conf','confluence','stdio',:e,:c,CAST(:a AS jsonb),'unknown') RETURNING id"),
            {"o": org, "e": server_command("confluence"), "c": creds, "a": json.dumps(ALLOW)},
        )).scalar_one()
    return await load_connector(cid, org)


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("reconciliations", "findings", "validated_processes",
                  "process_dispositions", "knowledge_chunks", "connectors"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


# --------------------------------------------------------------------------- #
# F2 closed against the connector: a wrong token → unhealthy, not false-green
# --------------------------------------------------------------------------- #
async def test_connection_test_exercises_the_credential():
    org = str(uuid.uuid4())
    try:
        good = await health_check(await _connector(org, GOOD))
        assert good["status"] == "healthy"
        bad = await health_check(await _connector(org, "alice@acme.com:WRONG"))
        assert bad["status"] == "unhealthy"  # reachable, but credential exercised → error
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# real documents ingest with real provenance
# --------------------------------------------------------------------------- #
async def test_documents_ingest_with_real_provenance():
    org = str(uuid.uuid4())
    try:
        connector = await _connector(org, GOOD)
        ids, complete = await ingest_knowledge_from_connector(
            connector, org_id=org, embedder=configured_embedder())
        assert complete and len(ids) == 3
        chunks = await get_chunks(org, "deploy-rollback")
        assert len(chunks) == 2  # the two rollback pages
        refs = {c.source_ref for c in chunks}
        assert all(r.startswith("https://acme.atlassian.net/wiki/") for r in refs)  # REAL urls
        # observed_at is the page's real last-modified, NOT ingest time
        obs = sorted(c.observed_at.isoformat() for c in chunks)
        assert obs[0].startswith("2026-01-10") and obs[1].startswith("2026-05-20")
        assert all(c.source_kind == "document" for c in chunks)
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the connector→ingest→reconcile mechanism surfaces a finding (FIXTURE, not real aha)
# --------------------------------------------------------------------------- #
async def test_reconcile_surfaces_a_finding_on_ingested_docs():
    org = str(uuid.uuid4())
    try:
        connector = await _connector(org, GOOD)
        await ingest_knowledge_from_connector(connector, org_id=org, embedder=configured_embedder())
        await declare_disposition(org_id=org, process_key="deploy-rollback",
                                  disposition="descriptive", rationale="t")
        # The two ingested rollback pages genuinely conflict (redeploy-image vs
        # restore-from-backup). The keyless lexical floor only compares behaviour-vs-document,
        # so we SCRIPT the detector to flag the doc-vs-doc conflict the real LLM detector
        # (M7.4) would catch — isolating the connector→ingest→reconcile MECHANISM from detector
        # quality. Reconcile then disposes using the REAL last-modified dates: the pages are
        # ~4 months apart, so it rules the newer page SUPERSEDES the older → a `stale` finding
        # ("this rollback page is out of date") — a genuine inconsistency surfaced from real
        # provenance. (Closer dates would surface it as a `contradiction` instead.)
        docs = await get_chunks(org, "deploy-rollback")
        a, b = docs[0].id, docs[1].id

        async def fn(chunks):
            present = {c.id for c in chunks}
            return [ClaimRelation(a, b, "contradicts")] if {a, b} <= present else []

        await reconcile_process(org, "deploy-rollback", detector=FunctionDetector(fn))
        findings = await list_findings(org, process_key="deploy-rollback", state=None)
        surfaced = [f for f in findings if f.kind in ("stale", "contradiction", "drift")]
        assert surfaced, f"expected a real finding, got {[f.kind for f in findings]}"
        # the finding traces back to the ingested chunks (which carry the REAL page URLs)
        evidence = {str(r) for f in surfaced for r in f.evidence_refs}
        assert evidence & {str(a), str(b)}
    finally:
        await _cleanup(org)


async def test_undatable_page_is_skipped_not_fabricated_fresh():
    """A page with real content but NO last-modified must be SKIPPED (and the pull reported
    PARTIAL) — never ingested with observed_at=now(), which would present it as falsely fresh
    and invert the staleness signal."""
    org = str(uuid.uuid4())
    try:
        connector = await _connector(org, GOOD, extra={"CONFLUENCE_DATELESS": "1"})
        ids, complete = await ingest_knowledge_from_connector(
            connector, org_id=org, embedder=configured_embedder())
        assert complete is False  # the undatable page made the pull partial
        urls = {c.source_ref for c in await get_chunks(org, "cache-flush")}
        assert all("/199/" not in u for u in urls)  # the undated page was not stored
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# credential safety on the connector path
# --------------------------------------------------------------------------- #
async def test_token_never_in_chunks_or_health():
    org = str(uuid.uuid4())
    try:
        connector = await _connector(org, GOOD)
        h = await health_check(connector)
        await ingest_knowledge_from_connector(connector, org_id=org, embedder=configured_embedder())
        chunks = await get_chunks(org, "deploy-rollback")
        assert GOOD not in str(h)
        assert all(GOOD not in c.content for c in chunks)
    finally:
        await _cleanup(org)
