"""G3: connector environment tag (prod | non_prod) for the consequential boundary

Revision ID: 0020_connector_environment
Revises: 0019_messages_seq_unique
Create Date: 2026-06-25

The trust ladder's consequential boundary (G3) auto-executes a reversible action only when it
is NON-production (plus high grounding + a rollback). Production is detected from this connector
tag (prod | non_prod) with a target_ref glob backstop. Default 'prod' so a freshly-configured
connector is treated as production (gated) until an operator explicitly marks it non_prod —
safe-error. IF NOT EXISTS / guarded constraint so the from-scratch create_all and this migration
agree.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_connector_environment"
down_revision: str | None = "0019_messages_seq_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS environment "
        "varchar(10) NOT NULL DEFAULT 'prod';"
    )
    op.execute("ALTER TABLE connectors DROP CONSTRAINT IF EXISTS ck_connectors_environment;")
    op.execute(
        "ALTER TABLE connectors ADD CONSTRAINT ck_connectors_environment "
        "CHECK (environment IN ('prod', 'non_prod'));"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE connectors DROP CONSTRAINT IF EXISTS ck_connectors_environment;")
    op.execute("ALTER TABLE connectors DROP COLUMN IF EXISTS environment;")
