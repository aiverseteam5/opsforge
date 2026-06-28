"""Verify that the per-transaction GUC used by RLS does not leak across pooled
connections.

`scope_to_org` calls `set_config('opsforge.current_org', ..., true)` — the
third argument `is_local=true` scopes the value to the current transaction and
resets it to the session default when the transaction ends (commit or rollback).
This is the guarantee that makes connection-pooling safe: a connection returned
to the pool carries no residual org identity for the next borrower.

These tests hit the real DB to verify the Postgres behaviour (not just the
SQLAlchemy call).  They require the Compose db to be running.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from conftest import TEST_DB_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from opsforge.db import scope_to_org, session_factory

pytestmark = pytest.mark.usefixtures("db_required")

_READ_GUC = text("SELECT current_setting('opsforge.current_org', true)")


@pytest_asyncio.fixture
async def null_pool_engine():
    """Async engine with NullPool — each connect() is a distinct physical connection."""
    eng = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    yield eng
    await eng.dispose()


async def test_guc_resets_to_empty_after_transaction_commit(null_pool_engine):
    """After a transaction that set is_local GUC commits, the same physical
    connection must return an empty string — not the previous org."""
    org = str(uuid.uuid4())

    async with null_pool_engine.connect() as conn:
        # Transaction 1: set the GUC with is_local=true (same as scope_to_org).
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('opsforge.current_org', :o, true)"),
                {"o": org},
            )
            inside = (await conn.execute(_READ_GUC)).scalar_one()
            assert inside == org, "GUC not visible inside the transaction"

        # Transaction committed; still the same underlying connection.
        # is_local=true must have reverted the GUC to the session default ('').
        outside = (await conn.execute(_READ_GUC)).scalar_one()
        assert outside in (None, ""), (
            f"GUC leaked across transaction boundary: got {outside!r}; "
            "pooled connections would carry stale org identity"
        )


async def test_guc_resets_to_empty_after_transaction_rollback(null_pool_engine):
    """Same guarantee holds when the transaction is rolled back (e.g. on error)."""
    org = str(uuid.uuid4())

    async with null_pool_engine.connect() as conn:
        try:
            async with conn.begin():
                await conn.execute(
                    text("SELECT set_config('opsforge.current_org', :o, true)"),
                    {"o": org},
                )
                raise RuntimeError("simulated error — forces rollback")
        except RuntimeError:
            pass

        outside = (await conn.execute(_READ_GUC)).scalar_one()
        assert outside in (None, ""), (
            f"GUC leaked after rollback: got {outside!r}"
        )


async def test_concurrent_sessions_do_not_bleed_guc():
    """Two concurrent transactions scoped to different orgs must each see only
    their own GUC value — no cross-contamination through a shared pool slot."""
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())

    async def _read_guc_inside_txn(org: str) -> str:
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            val = (await s.execute(_READ_GUC)).scalar_one()
            return val or ""

    # Run both sessions truly concurrently.
    seen_a, seen_b = await asyncio.gather(
        _read_guc_inside_txn(org_a),
        _read_guc_inside_txn(org_b),
    )

    assert seen_a == org_a, f"session A saw wrong org: {seen_a!r}"
    assert seen_b == org_b, f"session B saw wrong org: {seen_b!r}"
    assert seen_a != seen_b, "sessions returned the same GUC value — possible bleed"


async def test_scope_to_org_helper_uses_is_local(null_pool_engine):
    """scope_to_org must use is_local=true; confirmed by verifying the GUC resets
    after the transaction that called it commits (regression guard against changing
    is_local to false)."""
    org = str(uuid.uuid4())

    async with AsyncSession(null_pool_engine) as session:
        async with session.begin():
            await scope_to_org(session, org)

        # GUC must be gone now that the transaction has committed.
        val = (await session.execute(_READ_GUC)).scalar_one()
        assert val in (None, ""), (
            f"scope_to_org's is_local=true contract broken: GUC still {val!r} "
            "after transaction end"
        )
