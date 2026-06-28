"""Add token_version to api_tokens for HMAC-SHA256 migration.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # token_version tracks the hashing algorithm used:
    #   0 = legacy SHA-256 (invalidated — lookup SQL filters WHERE token_version = 1)
    #   1 = HMAC-SHA256 keyed on OPSFORGE_TOKEN_HMAC_SECRET
    #
    # Existing tokens cannot be re-hashed (plaintext not stored), so they are
    # left at version 0 and become invalid after code is deployed. Operators
    # must re-issue all API tokens after this migration runs.
    op.add_column(
        "api_tokens",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("api_tokens", "token_version")
