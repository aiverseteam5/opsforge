"""M6.3 — findings queue: emit, list/filter, lifecycle, evidence, org isolation."""

from __future__ import annotations

import uuid

import pytest
from conftest import TEST_DB_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.findings import emit_finding, list_findings, set_finding_state

pytestmark = pytest.mark.usefixtures("db_required")


async def _delete(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(text("DELETE FROM findings WHERE org_id = :o"), {"o": org})


async def test_emit_list_filter_and_lifecycle():
    org = str(uuid.uuid4())
    pk = "vpn-triage"
    ev = [uuid.uuid4(), uuid.uuid4()]
    try:
        fid = await emit_finding(
            org_id=org,
            kind="contradiction",
            process_key=pk,
            detail={"reason": "two runbooks disagree"},
            evidence_refs=ev,
            confidence=0.42,
        )
        await emit_finding(org_id=org, kind="gap", process_key="other", detail={"missing": "x"})

        # default lists open findings, oldest first by seq
        openf = await list_findings(org)
        assert [f.kind for f in openf] == ["contradiction", "gap"]
        # evidence_refs persisted as the chunk id strings
        c = next(f for f in openf if f.kind == "contradiction")
        assert set(c.evidence_refs) == {str(ev[0]), str(ev[1])}
        assert c.confidence == pytest.approx(0.42)
        assert c.detail == {"reason": "two runbooks disagree"}

        # filter by process_key
        assert [f.kind for f in await list_findings(org, process_key=pk)] == ["contradiction"]

        # lifecycle: dismiss removes it from the open queue
        await set_finding_state(org, fid, "dismissed")
        assert [f.kind for f in await list_findings(org)] == ["gap"]
        assert [f.kind for f in await list_findings(org, state="dismissed")] == ["contradiction"]

        # another org sees none of these
        assert await list_findings(str(uuid.uuid4())) == []
    finally:
        await _delete(org)


async def test_invalid_state_rejected():
    with pytest.raises(ValueError):
        await set_finding_state(str(uuid.uuid4()), uuid.uuid4(), "bogus")  # type: ignore[arg-type]


async def test_findings_rls_net_isolates_a_restricted_role():
    from opsforge.db import session_factory

    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    role = f"f_probe_{uuid.uuid4().hex[:10]}"
    pw = "probe"
    seed = text("INSERT INTO findings (org_id, kind) VALUES (:o, 'gap')")
    async with session_factory().begin() as s:
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(text(f'GRANT SELECT, INSERT ON findings TO "{role}"'))
        await s.execute(text(f'GRANT USAGE ON SEQUENCE findings_seq TO "{role}"'))
        await s.execute(seed, {"o": org_a})
        await s.execute(seed, {"o": org_b})

    probe = create_async_engine(
        make_url(TEST_DB_URL).set(username=role, password=pw), poolclass=NullPool
    )
    try:
        async with probe.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            assert (await conn.execute(text("SELECT count(*) FROM findings"))).scalar_one() == 0
            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"), {"o": org_a}
            )
            visible, foreign = (
                await conn.execute(
                    text("SELECT count(*), count(*) FILTER (WHERE org_id = :b) FROM findings"),
                    {"b": org_b},
                )
            ).one()
            assert visible == 1
            assert foreign == 0
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text("INSERT INTO findings (org_id, kind) VALUES (:b, 'gap')"), {"b": org_b}
                )
            assert "row-level security" in str(ei.value).lower()
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM findings WHERE org_id IN (:a,:b)"), {"a": org_a, "b": org_b}
            )
            await s.execute(text(f'REVOKE ALL ON findings FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SEQUENCE findings_seq FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
