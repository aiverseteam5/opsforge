"""M6.1 — provenance envelope + local-markdown ingest.

Pure-unit tests (no DB) cover the envelope contract, the precedence-ladder
rank, freshness recomputation, front-matter/observed_at, and the chunker. The
DB-backed test proves a full directory ingest lands every chunk with a complete
envelope and is org-isolated.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from conftest import TEST_DB_URL
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.ingest import (
    chunk_markdown,
    extract_observed_at,
    ingest_directory,
    ingest_markdown_file,
    parse_front_matter,
)
from opsforge.knowledge import (
    PendingChunk,
    ProvenanceEnvelope,
    count_chunks,
    freshness_days,
    get_chunks,
    store_chunks,
)

_NOW = datetime(2026, 6, 21, tzinfo=UTC)


async def _fake_embedder(texts: list[str]) -> list[list[float]]:
    return [[0.01] * 1536 for _ in texts]


async def _delete_org_chunks(*orgs: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        for org in orgs:
            await scope_to_org(s, org)
            await s.execute(
                text("DELETE FROM knowledge_chunks WHERE org_id = :o"), {"o": org}
            )


# --------------------------------------------------------------------------- #
# envelope contract (doctrine #2: no fact without provenance)
# --------------------------------------------------------------------------- #
def test_envelope_rejects_incomplete_provenance():
    # missing observed_at + ingested_at
    with pytest.raises(ValidationError):
        ProvenanceEnvelope(source_kind="document", source_ref="file://x")
    # bad source_kind
    with pytest.raises(ValidationError):
        ProvenanceEnvelope(
            source_kind="rumour", source_ref="x", observed_at=_NOW, ingested_at=_NOW
        )
    # empty source_ref
    with pytest.raises(ValidationError):
        ProvenanceEnvelope(
            source_kind="document", source_ref="", observed_at=_NOW, ingested_at=_NOW
        )


def test_source_rank_is_the_precedence_ladder():
    def rank(kind: str) -> int:
        return ProvenanceEnvelope(
            source_kind=kind, source_ref="x", observed_at=_NOW, ingested_at=_NOW
        ).source_rank

    assert rank("behaviour") == 3
    assert rank("document") == 2
    assert rank("research") == 1


def test_freshness_recomputed_from_observed_at():
    as_of = datetime(2026, 1, 11, tzinfo=UTC)
    assert freshness_days(datetime(2026, 1, 1, tzinfo=UTC), as_of) == 10
    # future observed_at floors at 0 rather than going negative
    assert freshness_days(datetime(2027, 1, 1, tzinfo=UTC), as_of) == 0
    # naive datetimes are treated as UTC
    assert freshness_days(datetime(2026, 1, 1), as_of) == 10


# --------------------------------------------------------------------------- #
# normalize: front-matter + observed_at
# --------------------------------------------------------------------------- #
def test_front_matter_observed_at_beats_mtime(tmp_path):
    p = tmp_path / "runbook.md"
    p.write_text(
        "---\nobserved_at: 2023-05-01\nsource_kind: document\n---\n# Title\nbody\n",
        encoding="utf-8",
    )
    fm, body = parse_front_matter(p.read_text(encoding="utf-8"))
    assert fm["source_kind"] == "document"
    assert body.startswith("# Title")
    obs = extract_observed_at(fm, p)
    assert (obs.year, obs.month) == (2023, 5)


def test_observed_at_falls_back_to_mtime(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# X\nbody", encoding="utf-8")
    fm, _ = parse_front_matter(p.read_text(encoding="utf-8"))
    assert fm == {}
    obs = extract_observed_at(fm, p)
    assert obs.tzinfo is not None  # mtime, tz-aware, recent


# --------------------------------------------------------------------------- #
# chunker
# --------------------------------------------------------------------------- #
def test_chunker_splits_by_heading_and_size():
    body = "# Triage\nstep. \n\n## Big\n" + "x" * 3000
    chunks = chunk_markdown(body, max_chars=500)
    assert len(chunks) >= 2
    # the heading prefix is counted against the bound, so it holds exactly
    assert all(len(c.text) <= 500 for c in chunks)
    # headings are tracked and prefixed for retrieval context
    assert any(c.heading == "## Big" for c in chunks)


def test_chunker_empty_body_yields_nothing():
    assert chunk_markdown("   \n\n  ") == []


def test_chunker_emits_heading_only_chunks():
    # a title-only file is never silently dropped
    assert [c.text for c in chunk_markdown("# Only a heading\n")] == ["# Only a heading"]
    # several headings with no body each become their own chunk
    assert [c.text for c in chunk_markdown("# A\n## B\n### C\n")] == ["# A", "## B", "### C"]


async def test_embedder_count_mismatch_raises(tmp_path):
    p = tmp_path / "multi.md"
    p.write_text("# A\nbody a\n\n## B\n" + "y" * 3000, encoding="utf-8")

    async def undersized(texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1536]  # one vector for many chunks

    with pytest.raises(ValueError):
        await ingest_markdown_file(p, org_id=str(uuid.uuid4()), embedder=undersized)


# --------------------------------------------------------------------------- #
# full ingest (DB)
# --------------------------------------------------------------------------- #
async def test_ingest_directory_lands_full_envelope(db_required, tmp_path):
    from opsforge.db import scope_to_org, session_factory
    from opsforge.ingest import ingest_directory
    from opsforge.knowledge import count_chunks, get_chunks

    org = str(uuid.uuid4())
    other = str(uuid.uuid4())
    (tmp_path / "runbook.md").write_text(
        "---\nobserved_at: 2023-01-15\n---\n# VPN triage\n" + "step one. " * 200,
        encoding="utf-8",
    )
    (tmp_path / "notes.md").write_text("# Notes\nshort note here.", encoding="utf-8")

    async def fake_embedder(texts: list[str]) -> list[list[float]]:
        return [[0.01] * 1536 for _ in texts]

    try:
        summary = await ingest_directory(tmp_path, org_id=org, embedder=fake_embedder)
        assert summary["files"] == 2
        assert summary["chunks"] >= 2
        assert await count_chunks(org) == summary["chunks"]

        rows = await get_chunks(org)
        for r in rows:
            # full provenance envelope present on every chunk
            assert r.source_kind == "document"
            assert r.source_rank == 2
            assert r.observed_at is not None
            assert r.ingested_at >= r.observed_at
            # reconciliation output not yet assigned
            assert r.process_disposition == "undeclared"
            assert r.confidence is None
        # the runbook chunks carry the front-matter observed_at, not ingest time
        assert any(r.observed_at.year == 2023 for r in rows)

        # org isolation: a different org sees none of these chunks
        assert await count_chunks(other) == 0
    finally:
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(
                text("DELETE FROM knowledge_chunks WHERE org_id = :o"), {"o": org}
            )


async def test_front_matter_cannot_self_promote_to_behaviour(db_required, tmp_path):
    """A markdown file declaring source_kind: behaviour is clamped to document
    (it cannot self-assign top-of-ladder rank); research is allowed to demote."""
    org = str(uuid.uuid4())
    (tmp_path / "sneaky.md").write_text(
        "---\nsource_kind: behaviour\n---\n# X\nbody here", encoding="utf-8"
    )
    (tmp_path / "demoted.md").write_text(
        "---\nsource_kind: research\n---\n# Y\nother body", encoding="utf-8"
    )
    try:
        await ingest_directory(tmp_path, org_id=org, embedder=_fake_embedder)
        rows = await get_chunks(org)
        assert {r.source_kind for r in rows} == {"document", "research"}
        for r in rows:
            assert r.source_kind in ("document", "research")
            assert r.source_rank == (1 if r.source_kind == "research" else 2)
    finally:
        await _delete_org_chunks(org)


async def test_source_ref_default_and_override(db_required, tmp_path):
    org = str(uuid.uuid4())
    (tmp_path / "a.md").write_text("# A\nbody", encoding="utf-8")
    (tmp_path / "b.md").write_text(
        "---\nsource_ref: confluence://page/42\n---\n# B\nbody", encoding="utf-8"
    )
    try:
        await ingest_directory(tmp_path, org_id=org, embedder=_fake_embedder)
        refs = {r.source_ref for r in await get_chunks(org)}
        assert any(r.startswith("file://") and r.endswith("a.md") for r in refs)
        assert "confluence://page/42" in refs
    finally:
        await _delete_org_chunks(org)


async def test_store_chunks_roundtrips_kinds_and_is_atomic(db_required):
    org = str(uuid.uuid4())
    now = datetime.now(UTC)

    def env(kind: str) -> ProvenanceEnvelope:
        return ProvenanceEnvelope(
            source_kind=kind, source_ref=f"x://{kind}", observed_at=now, ingested_at=now
        )

    try:
        ids = await store_chunks(
            org,
            [
                PendingChunk(content="b", envelope=env("behaviour"), embedding=[0.0] * 1536),
                PendingChunk(content="r", envelope=env("research"), embedding=[0.0] * 1536),
            ],
        )
        assert len(ids) == 2
        rank = {r.source_kind: r.source_rank for r in await get_chunks(org)}
        assert rank == {"behaviour": 3, "research": 1}  # CHECK + rank persist for all kinds

        # Atomicity: a batch whose 2nd row has a bad-dimension embedding writes
        # NOTHING — the first row must not survive a mid-batch failure.
        before = await count_chunks(org)
        with pytest.raises(DBAPIError):
            await store_chunks(
                org,
                [
                    PendingChunk(content="ok", envelope=env("document"), embedding=[0.0] * 1536),
                    PendingChunk(content="bad", envelope=env("document"), embedding=[0.1, 0.2]),
                ],
            )
        assert await count_chunks(org) == before
    finally:
        await _delete_org_chunks(org)


async def test_process_key_persists_and_filters(db_required, tmp_path):
    org = str(uuid.uuid4())
    (tmp_path / "vpn.md").write_text(
        "---\nprocess_key: vpn-triage\n---\n# VPN\nbody about vpn", encoding="utf-8"
    )
    try:
        await ingest_directory(tmp_path, org_id=org, embedder=_fake_embedder)
        assert await count_chunks(org, process_key="vpn-triage") >= 1
        assert await count_chunks(org, process_key="does-not-exist") == 0
        rows = await get_chunks(org, process_key="vpn-triage")
        assert rows and all(r.process_key == "vpn-triage" for r in rows)
    finally:
        await _delete_org_chunks(org)


async def test_ingest_redacts_secrets_before_embed_and_store(db_required, tmp_path):
    org = str(uuid.uuid4())
    token = "gAAAAA" + "B" * 40  # Fernet-shaped secret
    (tmp_path / "secret.md").write_text(
        f"# Runbook\nThe service key is {token} — keep it safe.", encoding="utf-8"
    )
    captured: list[str] = []

    async def capturing(texts: list[str]) -> list[list[float]]:
        captured.extend(texts)
        return [[0.0] * 1536 for _ in texts]

    try:
        await ingest_directory(tmp_path, org_id=org, embedder=capturing)
        # the secret never reached the third-party embedder
        assert captured and all(token not in t for t in captured)
        # and was never persisted
        rows = await get_chunks(org)
        assert rows and all(token not in r.content for r in rows)
        assert any("REDACTED" in r.content for r in rows)
    finally:
        await _delete_org_chunks(org)


async def test_knowledge_chunks_rls_net_isolates_a_restricted_role(db_required):
    """The knowledge_chunks RLS net isolates orgs for a non-superuser role, the
    same proof as test_jobs_rls (the dev superuser bypasses RLS, so the policy
    must be exercised through a restricted role)."""
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    role = f"kc_probe_{uuid.uuid4().hex[:10]}"
    pw = "probe"
    now = datetime.now(UTC)
    from opsforge.db import session_factory

    seed = text(
        "INSERT INTO knowledge_chunks "
        "(org_id, content, source_kind, source_ref, source_rank, observed_at, ingested_at) "
        "VALUES (:o, 'c', 'document', 'x://r', 2, :n, :n)"
    )
    async with session_factory().begin() as s:
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(text(f'GRANT SELECT, INSERT ON knowledge_chunks TO "{role}"'))
        await s.execute(seed, {"o": org_a, "n": now})
        await s.execute(seed, {"o": org_b, "n": now})

    probe_url = make_url(TEST_DB_URL).set(username=role, password=pw)
    probe = create_async_engine(probe_url, poolclass=NullPool)
    try:
        async with probe.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            blind = (
                await conn.execute(text("SELECT count(*) FROM knowledge_chunks"))
            ).scalar_one()
            assert blind == 0, "restricted role saw rows without declaring an org"

            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"), {"o": org_a}
            )
            visible, foreign = (
                await conn.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE org_id = :b) "
                        "FROM knowledge_chunks"
                    ),
                    {"b": org_b},
                )
            ).one()
            assert visible == 1
            assert foreign == 0, "RLS leaked a foreign org's chunk"

            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_chunks (org_id, content, source_kind, "
                        "source_ref, source_rank, observed_at, ingested_at) "
                        "VALUES (:b, 'c', 'document', 'x://r', 2, now(), now())"
                    ),
                    {"b": org_b},
                )
            assert "row-level security" in str(ei.value).lower()
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM knowledge_chunks WHERE org_id IN (:a,:b)"),
                {"a": org_a, "b": org_b},
            )
            await s.execute(text(f'REVOKE ALL ON knowledge_chunks FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
