"""M6.0 defense-in-depth: the Postgres RLS net on `jobs` isolates orgs even on a
raw query, independent of the app's `org_id` claim predicate.

Critically, RLS is bypassed for superuser / BYPASSRLS roles. The app's default
dev role (`opsforge`) is a superuser, so this test provisions an ephemeral
*restricted* role (NOSUPERUSER, NOBYPASSRLS) — the kind a real deployment must
connect as — and asserts the net through it:

  * scoped to org A, a bare `SELECT * FROM jobs` sees only org A's rows;
  * with no org declared, it sees nothing (fail-closed);
  * `INSERT` of a foreign-org row is rejected by the policy's WITH CHECK.

Requires the Compose db + migrate (through 0004) to be up.
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


async def test_rls_net_isolates_a_restricted_role():
    # Dedicated, per-run orgs so concurrent/leftover rows from other tests in the
    # same DB can't perturb the exact counts this test asserts.
    ORG_A = str(uuid.uuid4())
    ORG_B = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:10]
    role = f"rls_probe_{suffix}"
    pw = "probe"

    # Provision the restricted role and seed both orgs' jobs as the owner.
    async with session_factory().begin() as s:
        # CREATE ROLE is a utility statement: no bind params. role/pw are
        # test-controlled constants (role is a uuid suffix), so inlining is safe.
        await s.execute(
            text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await s.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await s.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO "{role}"')
        )
        await s.execute(
            text(
                "INSERT INTO jobs (org_id, kind, payload, status) VALUES "
                "(:a,'noop','{}','queued'), (:b,'noop','{}','queued')"
            ),
            {"a": ORG_A, "b": ORG_B},
        )

    probe_url = make_url(TEST_DB_URL).set(username=role, password=pw)
    probe = create_async_engine(probe_url, poolclass=NullPool)
    try:
        async with probe.connect() as raw:
            # AUTOCOMMIT: each statement is its own txn, so a deliberate RLS
            # violation can't poison a transaction. set_config must be
            # session-level (is_local=false) to survive across statements here.
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")

            # Fail-closed: no org declared → no rows visible.
            blind = (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
            assert blind == 0, "restricted role saw rows without declaring an org"

            # Scoped to org A → sees only org A, never org B.
            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, false)"),
                {"o": ORG_A},
            )
            visible, foreign = (
                await conn.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE org_id = :b) FROM jobs"
                    ),
                    {"b": ORG_B},
                )
            ).one()
            assert visible == 1
            assert foreign == 0, "RLS leaked a foreign org's row"

            # WITH CHECK blocks writing a foreign-org row while scoped to A.
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text(
                        "INSERT INTO jobs (org_id, kind, payload, status) "
                        "VALUES (:b,'noop','{}','queued')"
                    ),
                    {"b": ORG_B},
                )
            assert "row-level security" in str(ei.value).lower()

            # An EMPTY-string org GUC (the pooled-connection reset case, where a
            # prior is_local set_config reverts to '') must fail CLOSED — return
            # no rows, never raise invalid-uuid (the bug the live M6.6 run found).
            await conn.execute(text("SELECT set_config('opsforge.current_org', '', false)"))
            blind2 = (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
            assert blind2 == 0
    finally:
        await probe.dispose()
        async with session_factory().begin() as s:
            await s.execute(
                text("DELETE FROM jobs WHERE org_id IN (:a,:b)"),
                {"a": ORG_A, "b": ORG_B},
            )
            await s.execute(text(f'REVOKE ALL ON jobs FROM "{role}"'))
            await s.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
            await s.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
