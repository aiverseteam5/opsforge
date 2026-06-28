"""Create orgs table and backfill from all org_id-carrying tables.

Phase 5a: establishes the orgs table that gives org_id UUIDs a real home.
Prior to this migration, org_id was a plain UUID constant with no FK and no
orgs table. This migration:
  1. Creates orgs with FORCE RLS so opsforge_app can only see its own org.
  2. Backfills from every table that carries org_id (26 tables total) via
     UNION ALL — covers orgs that appear only in data tables with no users.
  3. Does NOT add FK constraints from existing tables to orgs (deferred —
     Phase 5a is non-destructive; FK addition would require a full table scan
     and is a separate migration concern).
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE orgs (
            id                  UUID PRIMARY KEY,
            name                TEXT NOT NULL DEFAULT 'default',
            parent_org_id       UUID REFERENCES orgs(id),
            is_control_plane_org BOOLEAN NOT NULL DEFAULT false,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
        CREATE POLICY orgs_org_isolation ON orgs
            USING (
                id = nullif(current_setting('opsforge.current_org', true), '')::uuid
            );
    """)

    # Backfill: collect every distinct org_id from all 26 tables.
    # ON CONFLICT DO NOTHING makes this idempotent (safe to re-run on retry).
    op.execute("""
        INSERT INTO orgs (id)
        SELECT DISTINCT org_id FROM (
            SELECT org_id FROM users
            UNION ALL SELECT org_id FROM api_tokens
            UNION ALL SELECT org_id FROM connectors
            UNION ALL SELECT org_id FROM credential_leases
            UNION ALL SELECT org_id FROM skills
            UNION ALL SELECT org_id FROM schedules
            UNION ALL SELECT org_id FROM runs
            UNION ALL SELECT org_id FROM run_events
            UNION ALL SELECT org_id FROM jobs
            UNION ALL SELECT org_id FROM actions
            UNION ALL SELECT org_id FROM audit_log
            UNION ALL SELECT org_id FROM graph_nodes
            UNION ALL SELECT org_id FROM graph_edges
            UNION ALL SELECT org_id FROM changes
            UNION ALL SELECT org_id FROM patterns
            UNION ALL SELECT org_id FROM feedback
            UNION ALL SELECT org_id FROM knowledge_chunks
            UNION ALL SELECT org_id FROM process_dispositions
            UNION ALL SELECT org_id FROM reconciliations
            UNION ALL SELECT org_id FROM findings
            UNION ALL SELECT org_id FROM validated_processes
            UNION ALL SELECT org_id FROM llm_providers
            UNION ALL SELECT org_id FROM conversations
            UNION ALL SELECT org_id FROM messages
            UNION ALL SELECT org_id FROM delegation_tokens
            UNION ALL SELECT org_id FROM jit_credentials
        ) sub
        WHERE org_id IS NOT NULL
        ON CONFLICT DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orgs CASCADE;")
