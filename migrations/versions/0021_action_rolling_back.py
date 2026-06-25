"""G4: add the 'rolling_back' action state (undo claim state)

Revision ID: 0021_action_rolling_back
Revises: 0020_connector_environment
Create Date: 2026-06-25

G4 "reversible" adds an operator-initiated UNDO of a SUCCEEDED reversible action (run its
declared rollback). The undo claims the action atomically (succeeded -> rolling_back) so a
double-click can never run the rollback twice; on success -> rolled_back, on failure ->
succeeded (still in effect). 'rolling_back' is a new state, so widen the CHECK. Drop+recreate
so the from-scratch create_all and this migration agree.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_action_rolling_back"
down_revision: str | None = "0020_connector_environment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES_NEW = (
    "proposed", "denied", "awaiting_approval", "approved", "dry_run_done",
    "executing", "succeeded", "failed", "rolling_back", "rolled_back",
)
_STATES_OLD = (
    "proposed", "denied", "awaiting_approval", "approved", "dry_run_done",
    "executing", "succeeded", "failed", "rolled_back",
)


def _recreate(states: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{s}'" for s in states)
    op.execute("ALTER TABLE actions DROP CONSTRAINT IF EXISTS ck_actions_state;")
    op.execute(
        f"ALTER TABLE actions ADD CONSTRAINT ck_actions_state CHECK (state IN ({allowed}));"
    )


def upgrade() -> None:
    _recreate(_STATES_NEW)


def downgrade() -> None:
    _recreate(_STATES_OLD)
