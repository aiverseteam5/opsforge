"""perf indexes: actions.org_id and run_events(run_id, created_at).

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trust-ladder endpoint filters actions by org_id — without this index the
    # query scans the full actions table on every request.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_actions_org ON actions(org_id);"
    )
    # Health-score fetches the last 24h of run_events ordered by created_at.
    # The existing (run_id, seq) index does not help the time-range filter.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_events_run_created "
        "ON run_events(run_id, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_run_events_run_created;")
    op.execute("DROP INDEX IF EXISTS ix_actions_org;")
