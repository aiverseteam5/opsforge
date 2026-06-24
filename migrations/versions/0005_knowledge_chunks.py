"""knowledge plane M6.1: knowledge_chunks (provenance envelope) + RLS

Revision ID: 0005_knowledge_chunks
Revises: 0004_jobs_org_rls
Create Date: 2026-06-21

The first table of the Knowledge & Truth Plane. Every chunk carries the full
provenance envelope (source_kind/ref/rank, observed_at≠ingested_at) at ingest;
the reconciliation-output columns (confidence, corroboration/contradiction
counts, superseded_by, reconciliation_id) are NULL until a reconciliation run
scores them (M6.2/M6.3). `process_key` is NULL until clustering assigns it.

Isolation-ready from birth: same FORCE RLS + `opsforge.current_org` GUC pattern
as `jobs` (0004), so a future restricted app role isolates knowledge per org with
no further migration. Reuses pgvector (extension enabled in 0001) for embeddings.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_knowledge_chunks"
down_revision: str | None = "0004_jobs_org_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE = """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    org_id        uuid NOT NULL,

    process_key   text,
    content       text NOT NULL,
    embedding     vector(1536),

    -- provenance envelope (assigned at ingest) --
    source_kind   text NOT NULL,
    source_ref    text NOT NULL,
    source_rank   int  NOT NULL,
    observed_at   timestamptz NOT NULL,
    ingested_at   timestamptz NOT NULL,

    -- reconciliation output (NULL until reconciled, M6.2/M6.3) --
    confidence       numeric,
    corroborated_by  int,
    contradicted_by  int,
    superseded_by    uuid,
    process_disposition text NOT NULL DEFAULT 'undeclared',
    reconciliation_id   uuid,

    CONSTRAINT ck_knowledge_chunks_source_kind
        CHECK (source_kind IN ('behaviour', 'document', 'research')),
    CONSTRAINT ck_knowledge_chunks_process_disposition
        CHECK (process_disposition IN ('descriptive', 'prescriptive', 'undeclared')),
    CONSTRAINT ck_knowledge_chunks_confidence
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
"""

_POLICY = """
CREATE POLICY knowledge_chunks_org_isolation ON knowledge_chunks
    USING (org_id = current_setting('opsforge.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('opsforge.current_org', true)::uuid);
"""


def upgrade() -> None:
    op.execute(_CREATE)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_process_key "
        "ON knowledge_chunks (process_key);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_source_kind "
        "ON knowledge_chunks (source_kind);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_confidence "
        "ON knowledge_chunks (confidence);"
    )
    # Vector similarity index (not expressible in ORM metadata), mirrors patterns.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding ON knowledge_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
    )
    # Org isolation net (dormant until the app connects as a non-superuser role).
    op.execute("ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE knowledge_chunks FORCE ROW LEVEL SECURITY;")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS knowledge_chunks_org_isolation ON knowledge_chunks;")
    op.execute("DROP TABLE IF EXISTS knowledge_chunks;")
