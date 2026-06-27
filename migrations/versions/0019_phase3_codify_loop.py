"""Phase 3: Codify Loop — rejected_at column, codify indexes, HNSW index

Revision ID: 0019_phase3_codify_loop
Revises: 0018_conversations
Create Date: 2026-06-27

Changes:
- skills.rejected_at TIMESTAMPTZ: human rejection of a proposed codified skill
- Partial unique index: one codified skill per run_id (prevent double-codify)
- Partial unique index: one active codify_skill job per run_id (idempotent enqueue)
- DROP ivfflat index on patterns.embedding, CREATE HNSW (better ANN recall)
- skills.updated_at TIMESTAMPTZ: track last approval/rejection time
"""

from __future__ import annotations

from alembic import op

revision: str = "0019_phase3_codify_loop"
down_revision: str | None = "0018_conversations"


def upgrade() -> None:
    # 1. Add rejected_at to skills (NULL = not rejected; timestamp = rejected).
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ DEFAULT NULL"
    )

    # 2. Add updated_at to skills for tracking approval/rejection timestamps.
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NULL"
    )

    # 3. Unique partial index: one codified skill per run_id, while not rejected.
    #    Prevents _finalize() from enqueueing a second codify job for the same run.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS skills_codified_run_id_uniq
        ON skills (org_id, (manifest->>'run_id'))
        WHERE source = 'codified' AND rejected_at IS NULL
        """
    )

    # 4. Unique partial index: deduplicates codify_skill jobs in the queue.
    #    ON CONFLICT DO NOTHING in enqueue_idempotent() relies on this index.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_codify_skill_run_uniq
        ON jobs (org_id, (payload->>'run_id'))
        WHERE kind = 'codify_skill' AND status IN ('queued', 'running')
        """
    )

    # 5. Replace ivfflat with HNSW on patterns.embedding.
    #    DROP first to avoid double write overhead during the transition.
    op.execute("DROP INDEX IF EXISTS ix_patterns_embedding")
    op.execute(
        "CREATE INDEX IF NOT EXISTS patterns_embedding_hnsw "
        "ON patterns USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS patterns_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_patterns_embedding ON patterns "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute("DROP INDEX IF EXISTS jobs_codify_skill_run_uniq")
    op.execute("DROP INDEX IF EXISTS skills_codified_run_id_uniq")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS rejected_at")
