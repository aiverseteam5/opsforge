"""M6.7 — the operator surface: API routes + ingest/reconcile worker job kinds.

Drives the ingest-to-signoff slice through the real endpoints and exercises the
keyless dev stand-ins (hash embedder, lexical detector, outline drafter)."""

from __future__ import annotations

import uuid

import pytest
from conftest import api_client
from sqlalchemy import text

from opsforge.knowledge import (
    ProvenanceEnvelope,
    get_chunks,
    store_chunk,
)
from opsforge.reconcile import LexicalDetector
from opsforge.security import generate_token
from opsforge.worker import handle_ingest, handle_reconcile

pytestmark = pytest.mark.usefixtures("db_required")


async def _token(org: str, role: str = "operator") -> dict[str, str]:
    from opsforge.db import session_factory

    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        uid = (
            await s.execute(
                text(
                    "INSERT INTO users (org_id,email,name,role) "
                    "VALUES (:o,:e,'t',:r) RETURNING id"
                ),
                {"o": org, "e": f"{uuid.uuid4().hex}@t.local", "r": role},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id,user_id,token_hash,name) "
                "VALUES (:o,:u,:h,'t')"
            ),
            {"o": org, "u": uid, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("actions", "jobs", "findings", "validated_processes",
                  "process_dispositions", "knowledge_chunks", "api_tokens", "users"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


# --------------------------------------------------------------------------- #
# the keyless lexical detector stand-in (no DB)
# --------------------------------------------------------------------------- #
class _C:
    def __init__(self, id, kind, content):
        self.id, self.source_kind, self.content = id, kind, content


async def test_lexical_detector_flags_divergence_not_agreement():
    det = LexicalDetector()
    a, b = uuid.uuid4(), uuid.uuid4()
    # divergent content → contradicts
    diverge = await det.analyze([
        _C(a, "behaviour", "we kill stuck sessions immediately"),
        _C(b, "document", "restart the vpn concentrator hardware"),
    ])
    assert [r.relation for r in diverge] == ["contradicts"]
    # near-identical content → agrees
    agree = await det.analyze([
        _C(a, "behaviour", "restart the vpn concentrator now"),
        _C(b, "document", "restart the vpn concentrator"),
    ])
    assert [r.relation for r in agree] == ["agrees"]


async def test_llm_detector_parses_relations_and_falls_back():
    from opsforge.gateway import ChatResult
    from opsforge.reconcile import LLMDetector

    a, b = uuid.uuid4(), uuid.uuid4()
    chunks = [_C(a, "behaviour", "we kill sessions"), _C(b, "document", "restart the box")]

    class _GoodGW:
        async def chat(self, messages, tools, model):
            return ChatResult(text='ok: [{"a": 0, "b": 1, "relation": "contradicts"}]')

        async def embedding(self, texts, model):
            return []

    rels = await LLMDetector(_GoodGW(), "m").analyze(chunks)
    assert [(str(r.chunk_a), str(r.chunk_b), r.relation) for r in rels] == [
        (str(a), str(b), "contradicts")
    ]

    class _RaiseGW:
        async def chat(self, messages, tools, model):
            raise RuntimeError("provider down")

        async def embedding(self, texts, model):
            return []

    # a provider failure is contained → falls back to the lexical detector
    rels2 = await LLMDetector(_RaiseGW(), "m").analyze(chunks)
    assert [r.relation for r in rels2] == ["contradicts"]


# --------------------------------------------------------------------------- #
# the worker job kinds
# --------------------------------------------------------------------------- #
async def test_ingest_and_reconcile_worker_jobs(tmp_path):
    org, pk = str(uuid.uuid4()), "vpn-triage"
    try:
        (tmp_path / "runbook.md").write_text(
            f"---\nprocess_key: {pk}\n---\n# VPN\nRestart the vpn concentrator hardware.",
            encoding="utf-8",
        )
        # ingest job (keyless hash embedder)
        await handle_ingest({"org_id": org, "path": str(tmp_path)})
        chunks = await get_chunks(org, pk)
        assert chunks and chunks[0].source_kind == "document"

        # add divergent observed behaviour, declare descriptive, reconcile
        await store_chunk(
            org_id=org, content="On call we kill stuck sessions, never restart.",
            envelope=ProvenanceEnvelope(source_kind="behaviour", source_ref="run://x",
                                        observed_at=chunks[0].observed_at,
                                        ingested_at=chunks[0].observed_at),
            embedding=[0.0] * 1536, process_key=pk,
        )
        from opsforge.dispositions import declare_disposition

        await declare_disposition(org_id=org, process_key=pk, disposition="descriptive",
                                  rationale="reality wins")
        await handle_reconcile({"org_id": org, "process_key": pk})

        # the divergence surfaced as a drift finding, and a process was generated
        from opsforge.findings import list_findings
        from opsforge.processes import get_current_process

        kinds = {f.kind for f in await list_findings(org, process_key=pk)}
        assert "drift" in kinds
        proc = await get_current_process(org, pk)
        assert proc is not None and len(proc.steps) >= 2
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the API surface
# --------------------------------------------------------------------------- #
async def test_ingest_endpoint_enqueues_a_job():
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            r = await c.post("/api/v1/knowledge/ingest", headers=headers,
                             json={"path": "/tmp/none"})
        assert r.status_code == 202, r.text
        assert r.json()["kind"] == "ingest"
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            n = (await s.execute(
                text("SELECT count(*) FROM jobs WHERE org_id=:o AND kind='ingest'"),
                {"o": org})).scalar_one()
        assert n == 1
    finally:
        await _cleanup(org)


async def test_findings_process_signoff_endpoints():
    org, pk = str(uuid.uuid4()), "p"
    headers = await _token(org)
    try:
        # seed knowledge + reconcile via the worker handler so the API has data
        await store_chunk(
            org_id=org, content="restart the box",
            envelope=ProvenanceEnvelope(source_kind="document", source_ref="x://d",
                                        observed_at=_now(), ingested_at=_now()),
            embedding=[0.0] * 1536, process_key=pk)
        await store_chunk(
            org_id=org, content="we never restart we kill sessions",
            envelope=ProvenanceEnvelope(source_kind="behaviour", source_ref="x://b",
                                        observed_at=_now(), ingested_at=_now()),
            embedding=[0.0] * 1536, process_key=pk)

        async with api_client() as c:
            # declare disposition via the API
            r = await c.post("/api/v1/dispositions", headers=headers,
                             json={"process_key": pk, "disposition": "descriptive"})
            assert r.status_code == 201, r.text
            # reconcile (worker handler, run inline here)
            await handle_reconcile({"org_id": org, "process_key": pk})

            findings = (await c.get(f"/api/v1/findings?process_key={pk}", headers=headers)).json()
            assert any(f["kind"] == "drift" for f in findings)

            proc = (await c.get(f"/api/v1/processes/{pk}", headers=headers)).json()
            assert proc["status"] == "draft" and len(proc["steps"]) >= 2

            so = await c.post(f"/api/v1/processes/{pk}/signoff", headers=headers)
            assert so.status_code == 200, so.text
            after = (await c.get(f"/api/v1/processes/{pk}", headers=headers)).json()
            assert after["status"] == "signed_off"
    finally:
        await _cleanup(org)


async def test_writer_endpoints_require_operator_or_admin():
    org = str(uuid.uuid4())
    viewer = await _token(org, role="viewer")
    try:
        async with api_client() as c:
            r = await c.post("/api/v1/knowledge/ingest", headers=viewer, json={"path": "/x"})
        assert r.status_code == 403
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# M8 read/triage surface
# --------------------------------------------------------------------------- #
async def _seed_chunk(org, pk, kind, ref, content, origin=None, identity=None):
    await store_chunk(
        org_id=org, content=content,
        envelope=ProvenanceEnvelope(source_kind=kind, source_ref=ref,
                                    observed_at=_now(), ingested_at=_now(),
                                    origin=origin, origin_identity=identity),
        embedding=[0.0] * 1536, process_key=pk)


async def test_list_chunks_endpoint_exposes_provenance_and_flags_unverified():
    org, pk = str(uuid.uuid4()), "p"
    headers = await _token(org)
    try:
        await _seed_chunk(org, pk, "document", "doc://d", "the documented way")
        await _seed_chunk(org, pk, "behaviour", "t://1", "did it",
                          origin="team-a", identity="grp-1")
        await _seed_chunk(org, pk, "behaviour", "t://2", "did it",
                          origin="team-b", identity=None)
        async with api_client() as c:
            rows = (await c.get("/api/v1/knowledge/chunks", headers=headers)).json()
        assert len(rows) == 3
        # every chunk carries provenance the page needs
        for r in rows:
            assert {"source_kind", "source_ref", "observed_at", "ingested_at",
                    "confidence", "provenance_root", "origin"} <= set(r)
        by_ref = {r["source_ref"]: r for r in rows}
        # verified ticket → provenance_root is the directory id; unverified → None (demoted)
        assert by_ref["t://1"]["provenance_root"] == "grp-1"
        assert by_ref["t://2"]["origin"] == "team-b" and by_ref["t://2"]["provenance_root"] is None
    finally:
        await _cleanup(org)


async def test_list_chunks_is_workspace_scoped():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    headers_a = await _token(org_a)
    try:
        await _seed_chunk(org_b, "p", "document", "doc://secret", "org B private knowledge")
        async with api_client() as c:
            rows = (await c.get("/api/v1/knowledge/chunks", headers=headers_a)).json()
        assert all(r["source_ref"] != "doc://secret" for r in rows)  # A never sees B
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)


async def test_job_status_endpoint():
    org = str(uuid.uuid4())
    headers = await _token(org)
    try:
        async with api_client() as c:
            jid = (await c.post("/api/v1/knowledge/ingest", headers=headers,
                                json={"path": "/tmp/none"})).json()["job_id"]
            r = await c.get(f"/api/v1/jobs/{jid}", headers=headers)
            assert r.status_code == 200 and r.json()["status"] in (
                "queued", "running", "done", "failed", "succeeded", "dead")
            miss = await c.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=headers)
            assert miss.status_code == 404
    finally:
        await _cleanup(org)


async def test_list_processes_and_versions_endpoints():
    org, pk = str(uuid.uuid4()), "p"
    headers = await _token(org)
    try:
        await _seed_chunk(org, pk, "document", "x://d", "restart the box")
        await handle_reconcile({"org_id": org, "process_key": pk})
        await handle_reconcile({"org_id": org, "process_key": pk})  # → a second version
        async with api_client() as c:
            procs = (await c.get("/api/v1/processes", headers=headers)).json()
            assert any(p["process_key"] == pk for p in procs)
            versions = (await c.get(f"/api/v1/processes/{pk}/versions", headers=headers)).json()
            assert len(versions) >= 2 and versions[0]["version"] > versions[1]["version"]
            miss = await c.get("/api/v1/processes/nope/versions", headers=headers)
            assert miss.status_code == 404
    finally:
        await _cleanup(org)


async def test_finding_triage_endpoint():
    org, pk = str(uuid.uuid4()), "p"
    headers = await _token(org)
    try:
        await _seed_chunk(org, pk, "document", "x://d", "restart the box")
        await _seed_chunk(org, pk, "behaviour", "x://b", "we never restart we kill sessions")
        from opsforge.dispositions import declare_disposition
        await declare_disposition(org_id=org, process_key=pk,
                                  disposition="descriptive", rationale="r")
        await handle_reconcile({"org_id": org, "process_key": pk})
        async with api_client() as c:
            findings = (await c.get(f"/api/v1/findings?process_key={pk}", headers=headers)).json()
            fid = findings[0]["id"]
            r = await c.patch(f"/api/v1/findings/{fid}", headers=headers,
                              json={"state": "acknowledged"})
            assert r.status_code == 200, r.text
            ack = (await c.get(f"/api/v1/findings?process_key={pk}&state=acknowledged",
                               headers=headers)).json()
            assert any(f["id"] == fid for f in ack)
    finally:
        await _cleanup(org)


async def test_findings_all_filter_returns_every_state():
    """The 'all' tab must show findings across lifecycle states — `all` (and empty)
    map to NO filter, never a `state = ''` predicate that matches nothing."""
    org, pk = str(uuid.uuid4()), "p"
    headers = await _token(org)
    try:
        await _seed_chunk(org, pk, "document", "x://d", "restart the box")
        await _seed_chunk(org, pk, "behaviour", "x://b", "we never restart we kill sessions")
        from opsforge.dispositions import declare_disposition
        await declare_disposition(org_id=org, process_key=pk,
                                  disposition="descriptive", rationale="r")
        await handle_reconcile({"org_id": org, "process_key": pk})
        async with api_client() as c:
            open0 = (await c.get(f"/api/v1/findings?process_key={pk}", headers=headers)).json()
            fid = open0[0]["id"]
            await c.patch(f"/api/v1/findings/{fid}", headers=headers,
                          json={"state": "acknowledged"})
            # the default ('open') tab no longer shows the acknowledged one…
            open_now = (await c.get(f"/api/v1/findings?process_key={pk}", headers=headers)).json()
            assert all(f["id"] != fid for f in open_now)
            # …but 'all' does (NOT an empty mirror)
            all_now = (await c.get(
                f"/api/v1/findings?process_key={pk}&state=all", headers=headers)).json()
            assert any(f["id"] == fid for f in all_now)
            # an empty state value is treated the same (no `state = ''` match-nothing)
            empty = (await c.get(
                f"/api/v1/findings?process_key={pk}&state=", headers=headers)).json()
            assert len(empty) == len(all_now) and len(all_now) >= 1
    finally:
        await _cleanup(org)


async def test_finding_triage_requires_writer():
    org = str(uuid.uuid4())
    viewer = await _token(org, role="viewer")
    try:
        async with api_client() as c:
            r = await c.patch(f"/api/v1/findings/{uuid.uuid4()}", headers=viewer,
                              json={"state": "resolved"})
        assert r.status_code == 403
    finally:
        await _cleanup(org)


def _now():
    from datetime import UTC, datetime
    return datetime(2026, 6, 21, tzinfo=UTC)
