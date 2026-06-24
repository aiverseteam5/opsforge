"""knowledge plane M6.2: process_dispositions (descriptive vs prescriptive) + RLS

Revision ID: 0006_process_dispositions
Revises: 0005_knowledge_chunks
Create Date: 2026-06-21

A small, human-owned table answering "what wins when behaviour and the document
disagree", per process_key. It cannot be guessed — it is a policy decision a
human declares, and the declaration is itself signed-off knowledge (doctrine #4).

Append-only by discipline (doctrine #7): re-declaring inserts a new row and the
latest wins, so the table also IS the history of who declared what, when, and
why. Same FORCE RLS + opsforge.current_org GUC isolation as the other tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_process_dispositions"
down_revision: str | None = "0005_knowledge_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE = """
CREATE TABLE IF NOT EXISTS process_dispositions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    org_id       uuid NOT NULL,
    process_key  text NOT NULL,
    disposition  text NOT NULL,
    declared_by  uuid,
    declared_at  timestamptz NOT NULL DEFAULT now(),
    rationale    text,
    -- monotonic insertion order so "latest wins" is unambiguous even when two
    -- declarations share a created_at (mirrors audit_log.seq). id is a random
    -- uuid, so it cannot be the tie-break.
    seq          bigint NOT NULL DEFAULT nextval('process_dispositions_seq'),
    CONSTRAINT ck_process_dispositions_disposition
        CHECK (disposition IN ('descriptive', 'prescriptive'))
);
"""

_POLICY = """
CREATE POLICY process_dispositions_org_isolation ON process_dispositions
    USING (org_id = current_setting('opsforge.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('opsforge.current_org', true)::uuid);
"""


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS process_dispositions_seq;")
    op.execute(_CREATE)
    # (org, process_key, seq) serves the latest-wins lookup directly.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_process_dispositions_org_process "
        "ON process_dispositions (org_id, process_key, seq);"
    )
    op.execute("ALTER TABLE process_dispositions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE process_dispositions FORCE ROW LEVEL SECURITY;")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS process_dispositions_org_isolation "
        "ON process_dispositions;"
    )
    op.execute("DROP TABLE IF EXISTS process_dispositions;")
    op.execute("DROP SEQUENCE IF EXISTS process_dispositions_seq;")
