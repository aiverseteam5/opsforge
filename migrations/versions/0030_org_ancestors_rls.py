"""Phase 5b C8: enable FORCE RLS on org_ancestors + ancestor-chain policy.

Migration 0029 created org_ancestors with no RLS and revoked INSERT/UPDATE from
opsforge_app until this policy was ready. This migration:
  1. Enables FORCE ROW LEVEL SECURITY on org_ancestors.
  2. Creates an ancestor-chain isolation policy — a session can see:
       - Rows where org_id = current_org  (my ancestor chain)
       - Rows where ancestor_id = current_org  (orgs that inherit from me)
  3. Re-grants INSERT and UPDATE to opsforge_app (safe now that RLS is in place).
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('opsforge.current_org', true), '')::uuid"
_PREDICATE = f"(org_id = {_GUC} OR ancestor_id = {_GUC})"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE org_ancestors ENABLE ROW LEVEL SECURITY;
        ALTER TABLE org_ancestors FORCE ROW LEVEL SECURITY;
        CREATE POLICY org_ancestors_isolation ON org_ancestors
            USING ({_PREDICATE})
            WITH CHECK ({_PREDICATE});
        GRANT INSERT, UPDATE ON org_ancestors TO opsforge_app;
    """)


def downgrade() -> None:
    op.execute("""
        REVOKE INSERT, UPDATE ON org_ancestors FROM opsforge_app;
        DROP POLICY IF EXISTS org_ancestors_isolation ON org_ancestors;
        ALTER TABLE org_ancestors DISABLE ROW LEVEL SECURITY;
    """)
