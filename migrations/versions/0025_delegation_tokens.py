"""delegation_tokens: A2A short-lived JWTs with org RLS and revocation index.

Each row is an issued delegation token (jti = JWT ID). The table is FORCE RLS
so the app role can only see tokens belonging to the current-transaction org.
The partial index on revoked_at IS NULL keeps revocation hot-path lookups fast.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_PRED = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "delegation_tokens",
        sa.Column("jti", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("iss", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("sub", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("ALTER TABLE delegation_tokens ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE delegation_tokens FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY delegation_tokens_org_isolation ON delegation_tokens "
        f"USING ({_PRED}) WITH CHECK ({_PRED});"
    )
    # Partial index: revocation check only scans active tokens (revoked_at IS NULL).
    op.execute(
        "CREATE INDEX idx_delegation_tokens_jti_active "
        "ON delegation_tokens(jti) WHERE revoked_at IS NULL;"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS delegation_tokens_org_isolation ON delegation_tokens;"
    )
    op.execute("ALTER TABLE delegation_tokens NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE delegation_tokens DISABLE ROW LEVEL SECURITY;")
    op.drop_table("delegation_tokens")
