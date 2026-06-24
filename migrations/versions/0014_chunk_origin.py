"""M7.5: knowledge_chunks.origin — the behavioural origin of a ticket-sourced chunk

Revision ID: 0014_chunk_origin
Revises: 0013_reconciliations
Create Date: 2026-06-22

Ticket-source ingestion makes behaviour the REAL top of the trust ladder, sourced
from a large, messy, manipulable external system. `origin` captures which group /
actor / automated job a behavioural observation came from, so:

  - `provenance_root_for()` becomes origin-aware for ticket sources, closing the
    M7.2 residual: N tickets from ONE origin share one root (volume from a single
    origin is not corroboration), N from separate origins are N distinct roots;
  - the behaviour-pattern threshold counts DISTINCT origins, so a single event — or
    repetition from one origin — never reaches behaviour-rank.

NULL `origin` = not ticket-sourced (a document, or human-asserted behaviour); those
keep the existing source-ref-level root and are not pattern-gated. Additive; the
opsforge_app grant already covers the column (table grant from 0009).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_chunk_origin"
down_revision: str | None = "0013_reconciliations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS origin text")
    op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_origin ON knowledge_chunks (origin)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_origin")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN origin")
