"""A1.5: bring the credential-bearing `connectors` table under FORCE RLS

Revision ID: 0016_connectors_rls
Revises: 0015_llm_providers
Create Date: 2026-06-23

`connectors` holds `credentials_enc` (Fernet-encrypted secrets) but — unlike every other
org-scoped table (jobs/knowledge_chunks/findings/validated_processes/process_dispositions/
reconciliations/llm_providers) — was never brought under row-level security. Its isolation
rested on app-level `org_id` predicates alone, with no DB backstop. A1's adversarial review
flagged this (HIGH); A2 is about to WRITE real credentials into this table, so the DB-enforced
net must exist FIRST (mirroring M6.6's "make isolation live before the surface depends on it").

Same FORCE-RLS + NULLIF(org GUC) fail-closed isolation as the rest of the plane: an empty or
absent `opsforge.current_org` GUC → NULLIF → NULL → `org_id = NULL` is never true → the query
returns NO rows (true fail-closed), never all rows. The opsforge_app grant on connectors is
already in place (0009 granted ALL TABLES + the default-privileges path).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_connectors_rls"
down_revision: str | None = "0015_llm_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATE = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE connectors ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE connectors FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY connectors_org_isolation ON connectors "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS connectors_org_isolation ON connectors;")
    op.execute("ALTER TABLE connectors NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE connectors DISABLE ROW LEVEL SECURITY;")
