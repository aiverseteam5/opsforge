"""Slice 1: allow connector kind 'monitoring' (the validate-the-signal ground-truth read)

Revision ID: 0023_monitoring_kind
Revises: 0022_actions_rls
Create Date: 2026-06-25

Slice 1 adds a generic 'monitoring' connector kind so the agent can read a service's live status
from a monitoring tool and validate a ticket's claim against ground truth. Widen the
connectors.kind CHECK. Drop + recreate (mirrors 0017_confluence_kind).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_monitoring_kind"
down_revision: str | None = "0022_actions_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_NEW = (
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack",
    "confluence", "monitoring", "custom",
)
_KINDS_OLD = (
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack",
    "confluence", "custom",
)


def _recreate(kinds: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{k}'" for k in kinds)
    op.execute("ALTER TABLE connectors DROP CONSTRAINT IF EXISTS ck_connectors_kind;")
    op.execute(
        f"ALTER TABLE connectors ADD CONSTRAINT ck_connectors_kind CHECK (kind IN ({allowed}));"
    )


def upgrade() -> None:
    _recreate(_KINDS_NEW)


def downgrade() -> None:
    _recreate(_KINDS_OLD)
