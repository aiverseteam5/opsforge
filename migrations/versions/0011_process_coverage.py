"""M7.1: validated_processes.uncovered_chunks (the drafter coverage check)

Revision ID: 0011_process_coverage
Revises: 0010_rls_nullif
Create Date: 2026-06-21

The LLM drafter may merge/drop chunks while synthesizing steps. Guardrail #4: a
surviving reconciled chunk that ends up represented in NO step must be flagged,
not silently lost. `uncovered_chunks` records those chunk ids on the generated
process so the signoff screen can surface "N chunks were not represented".
Computed deterministically after drafting; never authored by the drafter.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_process_coverage"
down_revision: str | None = "0010_rls_nullif"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE validated_processes "
        "ADD COLUMN IF NOT EXISTS uncovered_chunks jsonb NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE validated_processes DROP COLUMN uncovered_chunks")
