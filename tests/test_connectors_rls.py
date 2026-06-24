"""A1.5: the Postgres RLS net on `connectors` isolates orgs even on a raw query,
independent of the app's `org_id` predicate — the DB-enforced backstop on the
credential-bearing table that A2 will write secrets into.

RLS is bypassed for superuser / BYPASSRLS roles, so (like test_jobs_rls) this provisions
an ephemeral *restricted* role (NOSUPERUSER, NOBYPASSRLS) — the kind a real deployment
connects as — and asserts the net through it:
  * scoped to org A, a bare `SELECT * FROM connectors` sees only org A's rows;
  * with no org declared (and with an EMPTY-string GUC), it sees nothing (fail-closed);
  * INSERT of a foreign-org row is rejected by the policy's WITH CHECK.

Requires the Compose db + migrate (through 0016) to be up.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import TEST_DB_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.db import session_factory

pytestmark = pytest.mark.usefixtures("db_required")

_INSERT = (
    "INSERT INTO connectors (org_id, name, kind, transport, endpoint, status) "
    "VALUES (:o, 'c', 'servicenow', 'stdio', 'stub://x', 'unknown')"
)


async def test_connectors_rls_net_isolates_a_restricted_role():
    ORG_A = str(uuid.uuid4())
    ORG_B = str(uuid.uuid4())
    role = f"rls_conn_probe_{uuid.uuid4().hex[:10]}"
    pw = "probe"

    async with session_factory().begin() as s:
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON connectors TO "{role}"'))
        await s.execute(text(_INSERT), {"o": ORG_A})
        await s.execute(text(_INSERT), {"o": ORG_B})

    probe_url = make_url(TEST_DB_URL).set(username=role, password=pw)
    probe = create_async_engine(probe_url, poolclass=NullPool)
    try:
        async with probe.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")

            # Fail-closed: no org declared → no rows visible (DB-enforced, not app predicate).
            blind = (await conn.execute(text("SELECT count(*) FROM connectors"))).scalar_one()
            assert blind == 0, "restricted role saw connectors without declaring an org"

            # Scoped to org A → sees only org A, never org B.
            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"), {"o": ORG_A}
            )
            visible, foreign = (
                await conn.execute(
                    text("SELECT count(*), count(*) FILTER (WHERE org_id = :b) FROM connectors"),
                    {"b": ORG_B},
                )
            ).one()
            assert visible == 1
            assert foreign == 0, "RLS leaked a foreign org's connector"

            # WITH CHECK blocks writing a foreign-org row while scoped to A.
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(text(_INSERT), {"o": ORG_B})
            assert "row-level security" in str(ei.value).lower()

            # Empty-string GUC (the pooled-reset case) must fail CLOSED → no rows, not all.
            await conn.execute(text("SELECT set_config('opsforge.current_org', '', false)"))
            blind2 = (await conn.execute(text("SELECT count(*) FROM connectors"))).scalar_one()
            assert blind2 == 0
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM connectors WHERE org_id IN (:a,:b)"),
                {"a": ORG_A, "b": ORG_B},
            )
            await s.execute(text(f'REVOKE ALL ON connectors FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
