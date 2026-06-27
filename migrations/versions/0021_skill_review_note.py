"""Skill review feedback: add review_note column to skills

Revision ID: 0021_skill_review_note
Revises: 0020_token_expiry
Create Date: 2026-06-27

Changes:
- skills.review_note TEXT NULL: operator note left when approving or rejecting a
  proposed codified skill. Fed back into future codify_skill LLM prompts so the
  model learns what makes a good skill for this org.
"""

from __future__ import annotations

from alembic import op

revision: str = "0021_skill_review_note"
down_revision: str | None = "0020_token_expiry"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS review_note TEXT DEFAULT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS review_note")
