"""M7.2: provenance-disjoint corroboration (the un-spoofable confidence axiom)

Revision ID: 0012_provenance_root
Revises: 0011_process_coverage
Create Date: 2026-06-21

Confidence is the single axiom the M6.5 gate trusts. Its corroboration input was
spoofable: duplicating one source (a document split into many chunks, a page
restated) manufactured "agreements" that lifted the score. This milestone makes
agreement count only between PROVENANCE-DISJOINT sources.

- `provenance_root` (nullable) is the origin a chunk's disjointness is judged on.
  Today it IS the source_ref (one document = one ref = one root); the single seam
  to refine later when connectors capture a richer origin/author signal. NULL =
  indeterminate lineage, which fails safe (counts as NOT disjoint). Backfilled
  from source_ref for existing chunks.
- `corroborating_roots` / `contradicting_roots` (jsonb) record the DISTINCT roots
  that actually lifted/lowered a chunk's confidence, so the score stays
  explainable: a human can ask "which two sources?" and see two distinct roots.
  Written by reconciliation; never authored by a model.

Additive; the restricted opsforge_app role's table grant covers new columns.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_provenance_root"
down_revision: str | None = "0011_process_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS provenance_root text")
    # Backfill: the root of an existing chunk IS its source_ref (co-chunks of one
    # document already share a source_ref, so a runbook split into five chunks
    # backfills to one root).
    op.execute(
        "UPDATE knowledge_chunks SET provenance_root = source_ref "
        "WHERE provenance_root IS NULL"
    )
    op.execute(
        "ALTER TABLE knowledge_chunks "
        "ADD COLUMN IF NOT EXISTS corroborating_roots jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE knowledge_chunks "
        "ADD COLUMN IF NOT EXISTS contradicting_roots jsonb NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN contradicting_roots")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN corroborating_roots")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN provenance_root")
