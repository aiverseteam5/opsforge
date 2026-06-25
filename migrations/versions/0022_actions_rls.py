"""A-follow: bring the actions table under FORCE ROW LEVEL SECURITY

Revision ID: 0022_actions_rls
Revises: 0021_action_rolling_back
Create Date: 2026-06-25

The `actions` table was the ONE org-scoped table without RLS — its rows were reachable by raw
id, which the G4 review showed could be turned into a cross-workspace mutating call. This
brings it under the same FORCE-RLS + NULLIF fail-closed net as the rest of the plane (the A1.5
pattern used for connectors): the restricted opsforge_app role can only see/modify actions of
the workspace whose org GUC is set, and a missing GUC fails closed. Every actions query now
runs under scope_to_org (the executor threads the authoritative job org). Idempotent so the
from-scratch create_all and this migration agree.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_actions_rls"
down_revision: str | None = "0021_action_rolling_back"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATE = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE actions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE actions FORCE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS actions_org_isolation ON actions;")
    op.execute(
        f"CREATE POLICY actions_org_isolation ON actions "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS actions_org_isolation ON actions;")
    op.execute("ALTER TABLE actions NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE actions DISABLE ROW LEVEL SECURITY;")
