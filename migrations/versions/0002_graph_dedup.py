"""graph idempotency: unique keys so re-syncs upsert instead of duplicate

Revision ID: 0002_graph_dedup
Revises: 0001_initial
Create Date: 2026-06-13

graph_sync runs every ~10 min; without these, each run would duplicate edges and
deploy changes. Edges are identified by (src, dst, kind); connector-sourced
changes by (connector, kind, ref). Webhook changes (null connector_id) are NULL
in that key, so Postgres still lets legitimately repeated events insert.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_graph_dedup"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_edges_src_dst_kind "
        "ON graph_edges (src_id, dst_id, kind);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_changes_connector_kind_ref "
        "ON changes (source_connector_id, kind, ref);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_changes_connector_kind_ref;")
    op.execute("DROP INDEX IF EXISTS uq_graph_edges_src_dst_kind;")
