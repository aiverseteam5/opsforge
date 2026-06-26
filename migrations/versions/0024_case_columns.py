"""Slice 2: case-lineage columns on runs (an iterative-remediation case = a chain of runs)

Revision ID: 0024_case_columns
Revises: 0023_monitoring_kind
Create Date: 2026-06-27

Slice 2 works a ticket ITERATIVELY: after an executed action a follow-up run is chained on. A CASE
is that chain of runs. Add nullable `case_id` (the case a run belongs to) + `case_step` (0 = root)
so the case is directly queryable, plus a CHECK (case_step >= 0) and an index on case_id. Additive
+ nullable — NULL means a legacy/standalone run, no backfill needed. This is also the honest home
for the future Slice-3 customer_id column.

Idempotent (ADD COLUMN IF NOT EXISTS / DROP+ADD constraint / CREATE INDEX IF NOT EXISTS): on a
from-scratch DB 0001's create_all already builds these from models.py, so this migration is a
no-op there and an ALTER on an already-migrated DB (the project's standard pattern).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_case_columns"
down_revision: str | None = "0023_monitoring_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS case_id uuid;")
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS case_step integer;")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_case_step;")
    op.execute("ALTER TABLE runs ADD CONSTRAINT ck_runs_case_step CHECK (case_step >= 0);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_case_id ON runs (case_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_case_id;")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_case_step;")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS case_step;")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS case_id;")
