"""Create org_ancestors join table (schema-only, no RLS in Phase 5a).

This table pre-materializes ancestor chains for the multi-org control plane.
It is created empty in Phase 5a and has NO ROW LEVEL SECURITY intentionally:
  - Adding FORCE RLS with no policy would default-deny all rows to opsforge_app,
    causing silent zero-row results for any future Phase 5b query.
  - The table is unqueried in Phase 5a code — it is populated and read only in
    Phase 5b when the ancestor-chain RLS policy lands alongside it.

Mitigation for no-RLS: INSERT and UPDATE are REVOKED from opsforge_app so the
table cannot be pre-poisoned via the application DB role. Phase 5b re-grants
them alongside the RLS policy. SELECT is permitted but returns nothing useful
since the table is empty — no cross-tenant data can leak from an empty table.

Phase 5b migration adds: ENABLE ROW LEVEL SECURITY + ancestor-chain policy +
GRANT INSERT, UPDATE ON org_ancestors TO opsforge_app.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE org_ancestors (
            org_id       UUID NOT NULL REFERENCES orgs(id),
            ancestor_id  UUID NOT NULL REFERENCES orgs(id),
            PRIMARY KEY (org_id, ancestor_id)
        );
        CREATE INDEX ix_org_ancestors_ancestor ON org_ancestors(ancestor_id);
        REVOKE INSERT, UPDATE ON org_ancestors FROM opsforge_app;
    """)


def downgrade() -> None:
    op.execute("""
        GRANT INSERT, UPDATE ON org_ancestors TO opsforge_app;
        DROP TABLE IF EXISTS org_ancestors CASCADE;
    """)
