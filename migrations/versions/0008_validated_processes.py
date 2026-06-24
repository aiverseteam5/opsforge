"""knowledge plane M6.4: validated_processes (generated current-version) + RLS

Revision ID: 0008_validated_processes
Revises: 0007_findings
Create Date: 2026-06-21

The canonical current-version process drafted from the reconciled, scored chunks
(spec §6). Every step in `steps` carries its provenance — the source chunk ids,
their kinds, freshness, and a confidence — so the signoff screen can triage per
step and visibly flag the guesses. Versioned and append-only: regenerating mints
a new version and supersedes the prior one. Signoff reuses the kernel approval +
audit. Same FORCE RLS + opsforge.current_org GUC isolation as the other tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_validated_processes"
down_revision: str | None = "0007_findings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE = """
CREATE TABLE IF NOT EXISTS validated_processes (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    org_id         uuid NOT NULL,
    process_key    text NOT NULL,
    version        int NOT NULL,
    status         text NOT NULL DEFAULT 'draft',
    steps          jsonb NOT NULL DEFAULT '[]'::jsonb,
    reconciliation_id uuid,
    min_confidence numeric,
    superseded_by  uuid,
    signed_off_by  uuid,
    signed_off_at  timestamptz,
    seq            bigint NOT NULL DEFAULT nextval('validated_processes_seq'),
    CONSTRAINT ck_validated_processes_status
        CHECK (status IN ('draft', 'signed_off', 'superseded')),
    CONSTRAINT ck_validated_processes_min_conf
        CHECK (min_confidence IS NULL OR (min_confidence >= 0 AND min_confidence <= 1))
);
"""

_POLICY = """
CREATE POLICY validated_processes_org_isolation ON validated_processes
    USING (org_id = current_setting('opsforge.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('opsforge.current_org', true)::uuid);
"""


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS validated_processes_seq;")
    op.execute(_CREATE)
    # UNIQUE so concurrent regeneration cannot mint two rows at the same version:
    # the loser hits a serialization failure and retries, instead of silently
    # leaving two "current" versions (the single-current-version invariant).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_validated_processes_org_process "
        "ON validated_processes (org_id, process_key, version);"
    )
    op.execute("ALTER TABLE validated_processes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE validated_processes FORCE ROW LEVEL SECURITY;")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS validated_processes_org_isolation ON validated_processes;"
    )
    op.execute("DROP TABLE IF EXISTS validated_processes;")
    op.execute("DROP SEQUENCE IF EXISTS validated_processes_seq;")
