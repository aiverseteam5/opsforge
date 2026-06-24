"""worker-queue org isolation: FORCE row-level security on jobs

Revision ID: 0004_jobs_org_rls
Revises: 0003_ops_connector_fields
Create Date: 2026-06-21

M6.0. Every job already carries org_id at enqueue, but the SKIP LOCKED claim had
no org predicate — any worker could claim any org's job. The whole product thesis
is multi-team in one deployment, so that is a correctness bug the moment a second
org exists.

Defense in depth, mirroring the kernel's append-only triggers (which enforce
immutability at the DB, not in app code): we ENABLE + FORCE row-level security on
`jobs` and add a policy keyed to a per-transaction GUC, `opsforge.current_org`.
The app connects as the table owner, so FORCE is required — without it the owner
bypasses RLS and the net does nothing.

The policy is fail-closed: `current_setting(..., true)` is NULL when the GUC was
never set, and `org_id = NULL` is never true, so a transaction that forgets to
declare its org sees and writes zero rows. Every job-touching path therefore
calls db.scope_to_org() first (set_config with is_local=true → transaction
scoped, never leaks across pooled connections). The same GUC mechanism is reused
by the M6 knowledge tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_jobs_org_rls"
down_revision: str | None = "0003_ops_connector_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_POLICY = """
CREATE POLICY jobs_org_isolation ON jobs
    USING (org_id = current_setting('opsforge.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('opsforge.current_org', true)::uuid);
"""


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;")
    # FORCE so the policy also applies to the table owner (the app role).
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY;")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS jobs_org_isolation ON jobs;")
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE jobs DISABLE ROW LEVEL SECURITY;")
