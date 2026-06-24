"""A1.5 live proof — `connectors` is isolated by Postgres FORCE RLS, proven through the
RESTRICTED opsforge_app role (NOBYPASSRLS), the role the deployed app actually connects as.

Proves, live:
  1. AGENT TOOL-BINDING path works under RLS: load_connectors_by_kind(org) returns this
     workspace's connector (scope_to_org sets the GUC the policy reads).
  2. CROSS-WORKSPACE probe FAILS: scoped to org A, a raw `SELECT * FROM connectors` sees
     zero of org B's rows — DB-enforced, not app-predicate.
  3. FAIL-CLOSED: with no org / an empty-string GUC, a raw read returns nothing (never all).

Usage (as the restricted role):
  OPSFORGE_DATABASE_URL=postgresql+psycopg://opsforge_app:opsforge_app@localhost:5432/opsforge \
  PYTHONPATH=server PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/a15_live_proof.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ.setdefault(
    "OPSFORGE_DATABASE_URL",
    "postgresql+psycopg://opsforge_app:opsforge_app@localhost:5432/opsforge",
)

from sqlalchemy import text  # noqa: E402

from opsforge.connectors import load_connectors_by_kind  # noqa: E402
from opsforge.db import scope_to_org, session_factory  # noqa: E402


async def _seed(org: str, name: str) -> None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org)  # must scope to satisfy the WITH CHECK policy
        await s.execute(
            text("INSERT INTO connectors (org_id, name, kind, transport, endpoint, status) "
                 "VALUES (:o, :n, 'servicenow', 'stdio', 'stub://x', 'healthy')"),
            {"o": org, "n": name},
        )


async def _wipe(org: str) -> None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(text("DELETE FROM connectors WHERE org_id = :o"), {"o": org})


async def main() -> None:
    role = os.environ["OPSFORGE_DATABASE_URL"].split("://")[1].split(":")[0]
    print(f"[connected as role: {role}]\n")
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await _seed(org_a, "snow-A")
        await _seed(org_b, "snow-B")

        # 1. agent tool-binding read works under RLS (scope_to_org sets the GUC)
        by_kind = await load_connectors_by_kind(org_a)
        print(f"[1] agent tool-binding (load_connectors_by_kind) for org A: "
              f"kinds={sorted(by_kind)} → "
              f"{'WORKS under RLS' if 'servicenow' in by_kind else 'BROKEN'}")

        # 2 + 3. raw reads through the restricted role: scoped sees only A; unscoped → 0
        async with session_factory().begin() as s:
            await scope_to_org(s, org_a)
            seen_a = (await s.execute(
                text("SELECT count(*) FROM connectors"))).scalar_one()
            foreign = (await s.execute(
                text("SELECT count(*) FROM connectors WHERE org_id = :b"), {"b": org_b}
            )).scalar_one()
        ok2 = foreign == 0 and seen_a == 1
        print(f"[2] scoped to org A: sees {seen_a} connector(s), {foreign} of org B's "
              f"→ {'ISOLATED (no cross-workspace leak)' if ok2 else 'LEAK'}")

        # unscoped read in a fresh transaction (no scope_to_org) → fail closed
        async with session_factory().begin() as s:
            blind = (await s.execute(text("SELECT count(*) FROM connectors"))).scalar_one()
        print(f"[3] UNSCOPED read (no org GUC): sees {blind} "
              f"→ {'FAILS CLOSED (no rows)' if blind == 0 else 'FELL OPEN — LEAK'}")
    finally:
        await _wipe(org_a)
        await _wipe(org_b)
        print("\n[cleaned up]")


if __name__ == "__main__":
    asyncio.run(main())
