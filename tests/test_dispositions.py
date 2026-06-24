"""M6.2 — process disposition: human-declared descriptive/prescriptive, audited,
append-only (latest wins), org-isolated."""

from __future__ import annotations

import uuid

import pytest
from conftest import TEST_DB_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.dispositions import declare_disposition, get_disposition

pytestmark = pytest.mark.usefixtures("db_required")


async def _delete(org: str) -> None:
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(
            text("DELETE FROM process_dispositions WHERE org_id = :o"), {"o": org}
        )


async def test_declare_get_latest_wins_and_audited():
    from opsforge.db import scope_to_org, session_factory

    org = str(uuid.uuid4())
    pk = "vpn-triage"
    uid = uuid.uuid4()
    try:
        # undeclared until a human declares
        assert await get_disposition(org, pk) == "undeclared"

        await declare_disposition(
            org_id=org,
            process_key=pk,
            disposition="descriptive",
            declared_by=uid,
            rationale="how we actually triage",
        )
        assert await get_disposition(org, pk) == "descriptive"

        # re-declaration is append-only; the latest wins
        await declare_disposition(
            org_id=org, process_key=pk, disposition="prescriptive", rationale="now policy"
        )
        assert await get_disposition(org, pk) == "prescriptive"

        # declared_by + rationale actually persisted (the substance of "who/why")
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            rows = (
                await s.execute(
                    text(
                        "SELECT declared_by, rationale, disposition FROM process_dispositions "
                        "WHERE org_id = :o AND process_key = :pk ORDER BY seq"
                    ),
                    {"o": org, "pk": pk},
                )
            ).all()
        assert len(rows) == 2
        assert str(rows[0].declared_by) == str(uid)
        assert rows[0].rationale == "how we actually triage"
        assert rows[0].disposition == "descriptive"
        assert rows[1].declared_by is None
        assert rows[1].rationale == "now policy"

        # both declarations were audited with the right actor + detail
        async with session_factory().begin() as s:
            arows = (
                await s.execute(
                    text(
                        "SELECT actor, detail FROM audit_log WHERE org_id = :o "
                        "AND event = 'disposition.declared' AND subject_ref = :pk ORDER BY seq"
                    ),
                    {"o": org, "pk": pk},
                )
            ).all()
        assert len(arows) == 2
        assert arows[0].actor.startswith("user:")
        assert arows[0].detail == {
            "disposition": "descriptive",
            "rationale": "how we actually triage",
        }
        assert arows[1].actor == "system"  # declared_by was None
        assert arows[1].detail == {"disposition": "prescriptive", "rationale": "now policy"}

        # a different org is unaffected (isolation)
        assert await get_disposition(str(uuid.uuid4()), pk) == "undeclared"
    finally:
        await _delete(org)


async def test_latest_wins_tie_break_is_deterministic_by_seq():
    """Two declarations sharing an identical created_at must still resolve to a
    defined winner — the monotonic seq breaks the tie, not a random uuid."""
    from opsforge.db import scope_to_org, session_factory

    org = str(uuid.uuid4())
    pk = "proc"
    ins = text(
        "INSERT INTO process_dispositions (org_id, process_key, disposition, created_at) "
        "VALUES (:o, :pk, :d, '2026-01-01T00:00:00Z')"
    )
    try:
        # both rows get the same created_at; the later insert gets the higher seq
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(ins, {"o": org, "pk": pk, "d": "descriptive"})
            await s.execute(ins, {"o": org, "pk": pk, "d": "prescriptive"})
        assert await get_disposition(org, pk) == "prescriptive"
    finally:
        await _delete(org)


async def test_invalid_disposition_rejected():
    with pytest.raises(ValueError):
        await declare_disposition(
            org_id=str(uuid.uuid4()), process_key="x", disposition="bogus"  # type: ignore[arg-type]
        )


async def test_process_dispositions_rls_net_isolates_a_restricted_role():
    """Same DB-net proof as jobs/knowledge_chunks: a non-superuser role only sees
    and writes its own org's declarations."""
    from opsforge.db import session_factory

    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    role = f"pd_probe_{uuid.uuid4().hex[:10]}"
    pw = "probe"
    seed = text(
        "INSERT INTO process_dispositions (org_id, process_key, disposition) "
        "VALUES (:o, 'p', 'descriptive')"
    )
    async with session_factory().begin() as s:
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(
            text(f'GRANT SELECT, INSERT ON process_dispositions TO "{role}"')
        )
        # the seq default calls nextval, so the role needs sequence USAGE to even
        # reach the RLS WITH CHECK (a real grant the restricted app role needs too)
        await s.execute(
            text(f'GRANT USAGE ON SEQUENCE process_dispositions_seq TO "{role}"')
        )
        await s.execute(seed, {"o": org_a})
        await s.execute(seed, {"o": org_b})

    probe_url = make_url(TEST_DB_URL).set(username=role, password=pw)
    probe = create_async_engine(probe_url, poolclass=NullPool)
    try:
        async with probe.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            blind = (
                await conn.execute(text("SELECT count(*) FROM process_dispositions"))
            ).scalar_one()
            assert blind == 0

            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"), {"o": org_a}
            )
            visible, foreign = (
                await conn.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE org_id = :b) "
                        "FROM process_dispositions"
                    ),
                    {"b": org_b},
                )
            ).one()
            assert visible == 1
            assert foreign == 0

            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text(
                        "INSERT INTO process_dispositions (org_id, process_key, disposition) "
                        "VALUES (:b, 'p', 'descriptive')"
                    ),
                    {"b": org_b},
                )
            assert "row-level security" in str(ei.value).lower()
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM process_dispositions WHERE org_id IN (:a,:b)"),
                {"a": org_a, "b": org_b},
            )
            await s.execute(text(f'REVOKE ALL ON process_dispositions FROM "{role}"'))
            await s.execute(
                text(f'REVOKE ALL ON SEQUENCE process_dispositions_seq FROM "{role}"')
            )
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
