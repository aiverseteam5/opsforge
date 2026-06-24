"""M6.4 — validated-process generation: per-step provenance + low-confidence
flagging, versioned supersession, signoff + audit, org isolation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TEST_DB_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.knowledge import ProvenanceEnvelope, set_reconciliation, store_chunk
from opsforge.processes import (
    FunctionDrafter,
    StepDraft,
    generate_process,
    get_current_process,
    sign_off_process,
)

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)


async def _seed_scored(org, pk, kind, observed_at, content, confidence, recon) -> uuid.UUID:
    cid = await store_chunk(
        org_id=org,
        content=content,
        envelope=ProvenanceEnvelope(
            source_kind=kind, source_ref=f"x://{content}", observed_at=observed_at,
            ingested_at=observed_at,
        ),
        process_key=pk,
    )
    await set_reconciliation(
        org, cid, confidence=confidence, corroborated_by=0, contradicted_by=0,
        reconciliation_id=recon,
    )
    return cid


async def _cleanup(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for tbl in ("validated_processes", "knowledge_chunks"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE org_id = :o"), {"o": org})


async def test_generate_attaches_per_step_provenance_and_flags_low_confidence():
    org, pk = str(uuid.uuid4()), "deploy"
    recon = uuid.uuid4()
    try:
        hi = await _seed_scored(org, pk, "behaviour", AS_OF, "high step", 0.8, recon)
        lo = await _seed_scored(
            org, pk, "document", AS_OF - timedelta(days=10), "low step", 0.3, recon
        )

        async def drafter(chunks):
            by = {c.content: c for c in chunks}
            return [
                StepDraft(text="do the thing", source_chunks=[by["high step"].id], kind="step"),
                StepDraft(text="decide", source_chunks=[by["low step"].id], kind="decision"),
                StepDraft(
                    text="gate",
                    source_chunks=[by["high step"].id, by["low step"].id],
                    kind="gate",
                ),
            ]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        assert proc is not None
        assert proc.version == 1 and proc.status == "draft"
        assert proc.reconciliation_id == recon
        s0, s1, s2 = proc.steps

        # high-confidence step: not flagged, provenance attached
        assert s0["confidence"] == pytest.approx(0.8)
        assert s0["low_confidence"] is False
        assert s0["source_chunks"] == [str(hi)]
        assert s0["source_kinds"] == ["behaviour"]
        assert s0["kind"] == "step"

        # low-confidence step: flagged, freshness from its (older) source
        assert s1["confidence"] == pytest.approx(0.3)
        assert s1["low_confidence"] is True
        assert s1["freshness_days"] == 10
        assert s1["kind"] == "decision"

        # a step is only as strong as its weakest grounding → min(0.8, 0.3)
        assert s2["confidence"] == pytest.approx(0.3)
        assert s2["low_confidence"] is True
        assert set(s2["source_kinds"]) == {"behaviour", "document"}
        assert set(s2["source_chunks"]) == {str(hi), str(lo)}
        # freshness is the OLDEST source's age (max over the two sources)
        assert s2["freshness_days"] == 10

        # the process floor for triage is the weakest step
        assert proc.min_confidence == pytest.approx(0.3)
    finally:
        await _cleanup(org)


async def test_regenerate_mints_new_version_and_supersedes_prior():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed_scored(org, pk, "behaviour", AS_OF, "a", 0.7, uuid.uuid4())

        async def drafter(chunks):
            return [StepDraft(text="step", source_chunks=[chunks[0].id])]

        p1 = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        p2 = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        assert p1.version == 1 and p2.version == 2

        # only v2 is current; v1 is superseded by v2
        cur = await get_current_process(org, pk)
        assert cur.id == p2.id and cur.version == 2
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            row = (
                await s.execute(
                    text("SELECT status, superseded_by FROM validated_processes WHERE id=:id"),
                    {"id": str(p1.id)},
                )
            ).one()
        assert row.status == "superseded"
        assert str(row.superseded_by) == str(p2.id)
    finally:
        await _cleanup(org)


async def test_signoff_sets_status_and_audits():
    org, pk = str(uuid.uuid4()), "p"
    uid = uuid.uuid4()
    try:
        await _seed_scored(org, pk, "behaviour", AS_OF, "a", 0.7, uuid.uuid4())

        async def drafter(chunks):
            return [StepDraft(text="s", source_chunks=[chunks[0].id])]

        p = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        await sign_off_process(org, p.id, signed_by=uid)

        cur = await get_current_process(org, pk)
        assert cur.status == "signed_off"
        assert str(cur.signed_off_by) == str(uid)

        from opsforge.db import session_factory

        async with session_factory().begin() as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM audit_log WHERE org_id=:o "
                        "AND event='process.signed_off' AND subject_ref=:r"
                    ),
                    {"o": org, "r": str(p.id)},
                )
            ).scalar_one()
        assert n == 1

        # signing a non-draft process raises
        with pytest.raises(ValueError):
            await sign_off_process(org, p.id, signed_by=uid)
    finally:
        await _cleanup(org)


async def test_generate_returns_none_without_chunks():
    org = str(uuid.uuid4())

    async def drafter(chunks):
        return []

    assert await generate_process(org, "nope", drafter=FunctionDrafter(drafter)) is None


async def test_ungrounded_step_rejected_unscored_source_floors_to_zero():
    org, pk = str(uuid.uuid4()), "p"
    try:
        # one scored chunk, and one chunk left UNSCORED (confidence stays None)
        scored = await _seed_scored(org, pk, "behaviour", AS_OF, "scored", 0.9, uuid.uuid4())
        unscored = await store_chunk(
            org_id=org,
            content="unscored",
            envelope=ProvenanceEnvelope(
                source_kind="document", source_ref="x://u", observed_at=AS_OF, ingested_at=AS_OF
            ),
            process_key=pk,
        )

        async def drafter(chunks):
            by = {c.content: c for c in chunks}
            return [
                StepDraft(text="ungrounded", source_chunks=[]),  # no provenance → rejected
                StepDraft(text="mixed", source_chunks=[by["scored"].id, by["unscored"].id]),
            ]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        # guardrail #1: the ungrounded step never enters the process
        assert len(proc.steps) == 1
        mixed = proc.steps[0]
        # the unscored source floors the surviving step to 0 (not silently dropped)
        assert mixed["confidence"] == 0.0
        assert mixed["low_confidence"] is True
        assert set(mixed["source_chunks"]) == {str(scored), str(unscored)}
        assert proc.min_confidence == 0.0
    finally:
        await _cleanup(org)


async def test_foreign_or_absent_chunk_ids_are_filtered():
    org, pk = str(uuid.uuid4()), "p"
    try:
        real = await _seed_scored(org, pk, "behaviour", AS_OF, "real", 0.7, uuid.uuid4())
        ghost = uuid.uuid4()  # an id not in this process's chunk set

        async def drafter(chunks):
            return [StepDraft(text="s", source_chunks=[chunks[0].id, ghost])]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        step = proc.steps[0]
        assert step["source_chunks"] == [str(real)]  # ghost filtered out
        assert step["confidence"] == pytest.approx(0.7)  # only the real source counts
    finally:
        await _cleanup(org)


async def test_low_confidence_threshold_is_strict():
    org, pk = str(uuid.uuid4()), "p"
    try:
        at = await _seed_scored(org, pk, "behaviour", AS_OF, "at", 0.5, uuid.uuid4())
        below = await _seed_scored(org, pk, "document", AS_OF, "below", 0.49, uuid.uuid4())

        async def drafter(chunks):
            by = {c.content: c for c in chunks}
            return [
                StepDraft(text="at-threshold", source_chunks=[by["at"].id]),
                StepDraft(text="below", source_chunks=[by["below"].id]),
            ]

        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        s_at, s_below = proc.steps
        # confidence == threshold is NOT low (strict <)
        assert s_at["confidence"] == pytest.approx(0.5)
        assert s_at["low_confidence"] is False
        assert s_below["low_confidence"] is True
        assert {at, below}  # silence unused-var lint
    finally:
        await _cleanup(org)


async def test_regenerate_supersedes_even_a_signed_off_version():
    org, pk = str(uuid.uuid4()), "p"
    uid = uuid.uuid4()
    try:
        await _seed_scored(org, pk, "behaviour", AS_OF, "a", 0.7, uuid.uuid4())

        async def drafter(chunks):
            return [StepDraft(text="s", source_chunks=[chunks[0].id])]

        p1 = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        await sign_off_process(org, p1.id, signed_by=uid)
        p2 = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)

        assert p2.version == 2
        cur = await get_current_process(org, pk)
        assert cur.id == p2.id and cur.status == "draft"
        # signoff did NOT protect v1 from supersession (new knowledge wins)
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            row = (
                await s.execute(
                    text("SELECT status, superseded_by FROM validated_processes WHERE id=:id"),
                    {"id": str(p1.id)},
                )
            ).one()
        assert row.status == "superseded"
        assert str(row.superseded_by) == str(p2.id)
    finally:
        await _cleanup(org)


async def test_empty_draft_falls_back_to_mechanical():
    org, pk = str(uuid.uuid4()), "p"
    try:
        await _seed_scored(org, pk, "behaviour", AS_OF, "a", 0.7, uuid.uuid4())

        async def drafter(chunks):
            return []  # chunks exist, but the drafter produced nothing usable

        # guardrail #5: fall back to mechanical one-step-per-chunk, never an empty process
        proc = await generate_process(org, pk, drafter=FunctionDrafter(drafter), as_of=AS_OF)
        assert proc is not None
        assert len(proc.steps) == 1
        assert proc.steps[0]["confidence"] == pytest.approx(0.7)
        assert proc.version == 1
    finally:
        await _cleanup(org)


async def test_get_current_process_is_none_when_absent():
    assert await get_current_process(str(uuid.uuid4()), "never") is None


async def test_validated_processes_rls_net_isolates_a_restricted_role():
    from opsforge.db import session_factory

    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    role = f"vp_probe_{uuid.uuid4().hex[:10]}"
    pw = "probe"
    seed = text(
        "INSERT INTO validated_processes (org_id, process_key, version) VALUES (:o, 'p', 1)"
    )
    async with session_factory().begin() as s:
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(text(f'GRANT SELECT, INSERT ON validated_processes TO "{role}"'))
        await s.execute(text(f'GRANT USAGE ON SEQUENCE validated_processes_seq TO "{role}"'))
        await s.execute(seed, {"o": org_a})
        await s.execute(seed, {"o": org_b})

    probe = create_async_engine(
        make_url(TEST_DB_URL).set(username=role, password=pw), poolclass=NullPool
    )
    try:
        async with probe.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            assert (
                await conn.execute(text("SELECT count(*) FROM validated_processes"))
            ).scalar_one() == 0
            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"), {"o": org_a}
            )
            visible, foreign = (
                await conn.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE org_id = :b) "
                        "FROM validated_processes"
                    ),
                    {"b": org_b},
                )
            ).one()
            assert visible == 1 and foreign == 0
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text(
                        "INSERT INTO validated_processes (org_id, process_key, version) "
                        "VALUES (:b, 'p', 1)"
                    ),
                    {"b": org_b},
                )
            assert "row-level security" in str(ei.value).lower()
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM validated_processes WHERE org_id IN (:a,:b)"),
                {"a": org_a, "b": org_b},
            )
            await s.execute(text(f'REVOKE ALL ON validated_processes FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SEQUENCE validated_processes_seq FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
