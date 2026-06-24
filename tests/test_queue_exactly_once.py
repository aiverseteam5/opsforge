"""The headline queue tests. Require the Compose db + migrate to be up.

1. `test_exactly_once_under_three_workers` — the SKIP LOCKED queue claims each
   job exactly once under concurrent workers (M0 invariant).
2. `test_two_orgs_never_cross` — M6.0: org-pinned workers + RLS on `jobs` mean a
   worker only ever claims its own org's jobs.
3. `test_org_pinned_pool_leaves_peer_queue_untouched` — strongest isolation
   proof: a pool pinned to org A leaves org B's queue completely untouched.

Each test uses fresh, unique org ids so it is the sole consumer of its jobs and
stays hermetic even when the Compose `worker` containers (pinned to the default
org) are running.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from opsforge.db import enqueue, scope_to_org, session_factory
from opsforge.worker import process_one

pytestmark = pytest.mark.usefixtures("db_required")

TOTAL = 100
N_WORKERS = 3


async def _clear(*orgs: str) -> None:
    """Delete each org's jobs. Filter by org explicitly so this works whatever
    role/RLS posture the test connection has (the deterministic isolation proof
    must not depend on the mechanism under test)."""
    async with session_factory().begin() as s:
        for org in orgs:
            await scope_to_org(s, org)
            await s.execute(text("DELETE FROM jobs WHERE org_id = :org"), {"org": org})


async def _enqueue_n(org: str, n: int, token: str) -> None:
    async with session_factory().begin() as s:
        for _ in range(n):
            await enqueue(s, kind="noop", payload={"t": token}, org_id=org)


async def _counts(org: str, token: str) -> dict[str, int]:
    """Count this org's jobs for the token via an explicit org predicate. If the
    claim predicate ever leaked a peer org's job, `done`/`attempts` here would
    reveal it — so this proves predicate + worker-pinning isolation directly."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        row = (
            await s.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE status='done') AS done, "
                    "COALESCE(sum(attempts),0) AS attempts "
                    "FROM jobs WHERE payload->>'t' = :t AND org_id = :org"
                ),
                {"t": token, "org": org},
            )
        ).one()
    return {"total": row.total, "done": row.done, "attempts": row.attempts}


async def _drain(worker_id: str, org: str, claimed: list[dict]) -> None:
    """Drain this org's queue. Treat several consecutive empty claims as 'drained'
    — SKIP LOCKED can momentarily return nothing while peers hold row locks."""
    empty_streak = 0
    while empty_streak < 5:
        job = await process_one(worker_id, max_attempts=5, org_id=org)
        if job is None:
            empty_streak += 1
            await asyncio.sleep(0.02)
            continue
        empty_streak = 0
        claimed.append(job)


async def test_exactly_once_under_three_workers():
    token = uuid.uuid4().hex
    org = str(uuid.uuid4())  # unique org → these 3 workers are the only consumers
    await _clear(org)
    await _enqueue_n(org, TOTAL, token)

    # Three workers race to drain the queue.
    buckets: list[list[dict]] = [[] for _ in range(N_WORKERS)]
    await asyncio.gather(
        *(_drain(f"worker-{i}-{token}", org, buckets[i]) for i in range(N_WORKERS))
    )

    # Each worker's claimed ids must be disjoint from the others' (no double claim).
    all_ids = [str(j["id"]) for b in buckets for j in b]
    assert len(all_ids) == len(set(all_ids)), "a job was claimed by two workers"

    c = await _counts(org, token)
    assert c["total"] == TOTAL
    assert c["done"] == TOTAL
    assert c["attempts"] == TOTAL  # each job claimed exactly once
    assert len(all_ids) == TOTAL


async def test_two_orgs_never_cross():
    token = uuid.uuid4().hex
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    await _clear(org_a, org_b)
    await _enqueue_n(org_a, TOTAL, token)
    await _enqueue_n(org_b, TOTAL, token)

    # Three workers run concurrently: two pinned to org A, one pinned to org B.
    a1: list[dict] = []
    a2: list[dict] = []
    b1: list[dict] = []
    await asyncio.gather(
        _drain(f"a1-{token}", org_a, a1),
        _drain(f"a2-{token}", org_a, a2),
        _drain(f"b1-{token}", org_b, b1),
    )

    # No job is ever claimed across the org line: each pool's claims carry only
    # its own org_id.
    assert all(str(j["org_id"]) == org_a for j in a1 + a2), "org-A worker claimed foreign job"
    assert all(str(j["org_id"]) == org_b for j in b1), "org-B worker claimed foreign job"

    # Disjoint ids across all three workers (exactly-once still holds per org).
    all_ids = [str(j["id"]) for j in a1 + a2 + b1]
    assert len(all_ids) == len(set(all_ids))

    # Both queues fully drained, each job claimed exactly once.
    ca = await _counts(org_a, token)
    cb = await _counts(org_b, token)
    assert ca == {"total": TOTAL, "done": TOTAL, "attempts": TOTAL}
    assert cb == {"total": TOTAL, "done": TOTAL, "attempts": TOTAL}


async def test_org_pinned_pool_leaves_peer_queue_untouched():
    """The strongest isolation proof: a pool pinned to org A, run while org B has
    a full queue, must not advance a single org-B job — not claim, not even
    increment attempts."""
    token = uuid.uuid4().hex
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    await _clear(org_a, org_b)
    await _enqueue_n(org_a, TOTAL, token)
    await _enqueue_n(org_b, TOTAL, token)

    # Only org-A workers run.
    buckets: list[list[dict]] = [[] for _ in range(N_WORKERS)]
    await asyncio.gather(
        *(_drain(f"a-{i}-{token}", org_a, buckets[i]) for i in range(N_WORKERS))
    )

    ca = await _counts(org_a, token)
    cb = await _counts(org_b, token)
    assert ca["done"] == TOTAL  # org A fully drained
    # org B is pristine: nothing done, no claim attempts recorded.
    assert cb == {"total": TOTAL, "done": 0, "attempts": 0}
