"""M6.6 hardening: RLS policies fail closed (NULLIF) on an empty org GUC

Revision ID: 0010_rls_nullif
Revises: 0009_app_role
Create Date: 2026-06-21

Surfaced by running live as the restricted role over a pooled connection: after
scope_to_org's `set_config(..., is_local=true)` resets at transaction end, the
custom GUC reverts to '' (empty string), not NULL. The original policies cast
`current_setting('opsforge.current_org', true)::uuid`, so an UNSCOPED query then
errors with `invalid input syntax for type uuid: ""` instead of failing closed.

Wrapping in NULLIF(..., '') makes an empty/absent GUC behave as NULL → the
`org_id = NULL` predicate is never true → the query returns nothing (true
fail-closed), which is what every unscoped path should get. Recreates all five
org-isolation policies with the hardened predicate.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_rls_nullif"
down_revision: str | None = "0009_app_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, policy_name)
_POLICIES = [
    ("jobs", "jobs_org_isolation"),
    ("knowledge_chunks", "knowledge_chunks_org_isolation"),
    ("findings", "findings_org_isolation"),
    ("validated_processes", "validated_processes_org_isolation"),
    ("process_dispositions", "process_dispositions_org_isolation"),
]

_NEW = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"
_OLD = "org_id = current_setting('opsforge.current_org', true)::uuid"


def _recreate(predicate: str) -> None:
    for table, policy in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table};")
        op.execute(
            f"CREATE POLICY {policy} ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate});"
        )


def upgrade() -> None:
    _recreate(_NEW)


def downgrade() -> None:
    _recreate(_OLD)
