"""API token expiry: add expires_at column to api_tokens

Revision ID: 0020_token_expiry
Revises: 0019_phase3_codify_loop
Create Date: 2026-06-27

Changes:
- api_tokens.expires_at TIMESTAMPTZ NULL: when set, token is rejected after this time.
  NULL = no expiry (preserves existing tokens without disruption).
"""

from __future__ import annotations

from alembic import op

revision: str = "0020_token_expiry"
down_revision: str | None = "0019_phase3_codify_loop"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ DEFAULT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE api_tokens DROP COLUMN IF EXISTS expires_at")
