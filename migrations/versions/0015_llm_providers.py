"""M7.6 Job A: llm_providers — per-workspace LLM provider/model + vault credential

Revision ID: 0015_llm_providers
Revises: 0014_chunk_origin
Create Date: 2026-06-22

The LLM becomes a per-workspace connector: {provider, model, credential} where the
credential lives in the Fernet vault (credential_enc), decrypted only at call time,
never in `.env` for production. A workspace's provider choice is a MEASURED decision —
a row is `proposed`, SCORED against the M7.3 golden sets, and only `promote`d to
`active` (the workspace's detector) if it holds the baseline. At most one `active` row
per workspace (a partial unique index); a workspace with no active provider falls back
to the keyless lexical floor — NOT a shared global key, so isolation holds for the LLM.

Same FORCE-RLS + NULLIF(org GUC) fail-closed isolation as the rest of the plane; the
opsforge_app grant is automatic via the ALTER DEFAULT PRIVILEGES from 0009.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_llm_providers"
down_revision: str | None = "0014_chunk_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE = """
CREATE TABLE IF NOT EXISTS llm_providers (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    org_id         uuid NOT NULL,
    provider       text NOT NULL,
    model          text NOT NULL,
    credential_enc bytea,
    status         text NOT NULL DEFAULT 'proposed',
    scorecard      jsonb,
    CONSTRAINT ck_llm_providers_status CHECK (status IN ('proposed', 'active', 'rejected'))
);
"""

_PREDICATE = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def upgrade() -> None:
    op.execute(_CREATE)
    # At most one ACTIVE provider per workspace.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_providers_active "
        "ON llm_providers (org_id) WHERE status = 'active';"
    )
    op.execute("ALTER TABLE llm_providers ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE llm_providers FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY llm_providers_org_isolation ON llm_providers "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS llm_providers_org_isolation ON llm_providers;")
    op.execute("DROP TABLE IF EXISTS llm_providers;")
