"""JIT credential leases: credential_kind + oidc_config_enc on connectors, credential_leases table

Revision ID: 0022_jit_credentials
Revises: 0021_skill_review_note
Create Date: 2026-06-27

Changes:
- connectors.credential_kind VARCHAR(30) NOT NULL DEFAULT 'static'
  Values: static | oidc_aws | vault_approle
- connectors.oidc_config_enc BYTEA NULL
  Fernet-encrypted JSON blob holding the JIT provider config (role ARN, Vault
  address, etc.). Never returned by the API — write-only like credentials_enc.
- credential_leases table: append-only audit trail for every non-static
  credential issuance. The materialised credential itself is NEVER stored;
  only the metadata (which role, which run, when it expires) is recorded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0022_jit_credentials"
down_revision = "0021_skill_review_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column(
            "credential_kind",
            sa.String(30),
            nullable=False,
            server_default="static",
        ),
    )
    op.add_column(
        "connectors",
        sa.Column("oidc_config_enc", sa.LargeBinary(), nullable=True),
    )
    op.create_check_constraint(
        "ck_connectors_credential_kind",
        "connectors",
        "credential_kind IN ('static', 'oidc_aws', 'vault_approle')",
    )

    op.create_table(
        "credential_leases",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connector_id",
            UUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "lease_metadata",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_credential_leases_connector_expires",
        "credential_leases",
        ["connector_id", "expires_at"],
    )
    op.create_index(
        "ix_credential_leases_run",
        "credential_leases",
        ["run_id"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_credential_leases_run", table_name="credential_leases")
    op.drop_index(
        "ix_credential_leases_connector_expires", table_name="credential_leases"
    )
    op.drop_table("credential_leases")
    op.drop_constraint("ck_connectors_credential_kind", "connectors")
    op.drop_column("connectors", "oidc_config_enc")
    op.drop_column("connectors", "credential_kind")
