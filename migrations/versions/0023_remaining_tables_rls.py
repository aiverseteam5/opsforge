"""Enable Row Level Security on remaining multi-tenant tables.

FORCE RLS tables (app role is also filtered):
  runs, run_events, actions, audit_log, schedules, skills,
  graph_nodes, graph_edges, changes, patterns, feedback, credential_leases

ENABLE (NOT FORCE) RLS tables (app role bypasses for auth bootstrap):
  api_tokens, users

Revision ID: 0023_remaining_tables_rls
Revises: 0022_jit_credentials
"""

from __future__ import annotations

from alembic import op

revision = "0023_remaining_tables_rls"
down_revision = "0022_jit_credentials"
branch_labels = None
depends_on = None

_PREDICATE = (
    "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"
)

_FORCE_TABLES = [
    "runs",
    "run_events",
    "actions",
    "audit_log",
    "schedules",
    "skills",
    "graph_nodes",
    "graph_edges",
    "changes",
    "patterns",
    "feedback",
    "credential_leases",
]

# Table owner (app DB role) bypasses RLS on these so the auth lookup
# (require_token) can read tokens before the principal's org_id is known.
_ENABLE_ONLY_TABLES = [
    "api_tokens",
    "users",
]


def upgrade() -> None:
    for table in _FORCE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
        )

    for table in _ENABLE_ONLY_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
        )


def downgrade() -> None:
    for table in _FORCE_TABLES + _ENABLE_ONLY_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
