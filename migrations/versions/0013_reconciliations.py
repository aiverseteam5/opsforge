"""M7.4: reconciliations record — make a degraded (LLM→lexical fallback) run visible

Revision ID: 0013_reconciliations
Revises: 0012_provenance_root
Create Date: 2026-06-22

With the real LLM detector now the production reconcile path, its failures fall
back to the lexical floor. A degraded run must not present as a normal one, so
every reconcile_process run writes one row here recording which detector actually
ran (llm / lexical / lexical_fallback / scripted) plus the run's scored/superseded/
findings counts. So a human (or a scorecard) can see a reconciliation ran degraded.

Same FORCE-RLS + NULLIF(org GUC) fail-closed isolation as the other M6/M7 tables;
the opsforge_app grant is automatic via the ALTER DEFAULT PRIVILEGES set in 0009.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_reconciliations"
down_revision: str | None = "0012_provenance_root"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE = """
CREATE TABLE IF NOT EXISTS reconciliations (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    org_id       uuid NOT NULL,
    process_key  text NOT NULL,
    detector     text NOT NULL,
    scored       int  NOT NULL,
    superseded   int  NOT NULL,
    findings     int  NOT NULL,
    CONSTRAINT ck_reconciliations_detector CHECK (
        detector IN ('llm', 'lexical', 'lexical_fallback', 'scripted', 'unknown')
    )
);
"""

_PREDICATE = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def upgrade() -> None:
    op.execute(_CREATE)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reconciliations_org_process "
        "ON reconciliations (org_id, process_key, created_at DESC);"
    )
    op.execute("ALTER TABLE reconciliations ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE reconciliations FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY reconciliations_org_isolation ON reconciliations "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS reconciliations_org_isolation ON reconciliations;")
    op.execute("DROP TABLE IF EXISTS reconciliations;")
