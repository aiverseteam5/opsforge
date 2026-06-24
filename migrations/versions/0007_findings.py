"""knowledge plane M6.3: findings (the reconciliation mirror) + RLS

Revision ID: 0007_findings
Revises: 0006_process_dispositions
Create Date: 2026-06-21

Every unresolved contradiction, behaviour-vs-prescriptive-doc violation, drift a
descriptive process should reconcile, gap (a process with only behaviour or only
documents), or stale supersession becomes a findings row — surfaced in the
approval queue. This is the proactive value that falls out of reconciliation for
free (spec §5.4). evidence_refs holds the chunk ids that prove the finding.

Append-only by discipline + monotonic seq for stable ordering (like audit_log).
Same FORCE RLS + opsforge.current_org GUC isolation as the other tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_findings"
down_revision: str | None = "0006_process_dispositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE = """
CREATE TABLE IF NOT EXISTS findings (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    org_id         uuid NOT NULL,
    process_key    text,
    kind           text NOT NULL,
    detail         jsonb,
    evidence_refs  jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence     numeric,
    state          text NOT NULL DEFAULT 'open',
    reconciliation_id uuid,
    seq            bigint NOT NULL DEFAULT nextval('findings_seq'),
    CONSTRAINT ck_findings_kind
        CHECK (kind IN ('contradiction', 'drift', 'gap', 'violation', 'stale')),
    CONSTRAINT ck_findings_state
        CHECK (state IN ('open', 'acknowledged', 'resolved', 'dismissed')),
    CONSTRAINT ck_findings_confidence
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
"""

_POLICY = """
CREATE POLICY findings_org_isolation ON findings
    USING (org_id = current_setting('opsforge.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('opsforge.current_org', true)::uuid);
"""


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS findings_seq;")
    op.execute(_CREATE)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_findings_org_state ON findings (org_id, state, seq);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_findings_org_process ON findings (org_id, process_key);"
    )
    op.execute("ALTER TABLE findings ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE findings FORCE ROW LEVEL SECURITY;")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS findings_org_isolation ON findings;")
    op.execute("DROP TABLE IF EXISTS findings;")
    op.execute("DROP SEQUENCE IF EXISTS findings_seq;")
