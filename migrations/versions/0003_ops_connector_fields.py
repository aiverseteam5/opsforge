"""ops connector fields: field_mapping + discovered_schema + new connector kinds

Revision ID: 0003_ops_connector_fields
Revises: 0002_graph_dedup
Create Date: 2026-06-14

Additive (revised v1.0): ITSM/ops connectors (ServiceNow/Jira/PagerDuty) plug in
by configuration — `field_mapping` declaratively maps their native schema to the
canonical ops model, `discovered_schema` caches their describe output.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_ops_connector_fields"
down_revision: str | None = "0002_graph_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "'aws', 'kubernetes', 'datadog', 'slack', 'custom'"
_NEW_KINDS = (
    "'aws', 'kubernetes', 'datadog', 'servicenow', 'jira', "
    "'pagerduty', 'slack', 'custom'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS field_mapping jsonb")
    op.execute("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS discovered_schema jsonb")
    op.execute("ALTER TABLE connectors DROP CONSTRAINT ck_connectors_kind")
    op.execute(
        f"ALTER TABLE connectors ADD CONSTRAINT ck_connectors_kind "
        f"CHECK (kind IN ({_NEW_KINDS}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE connectors DROP CONSTRAINT ck_connectors_kind")
    op.execute(
        f"ALTER TABLE connectors ADD CONSTRAINT ck_connectors_kind "
        f"CHECK (kind IN ({_OLD_KINDS}))"
    )
    op.execute("ALTER TABLE connectors DROP COLUMN discovered_schema")
    op.execute("ALTER TABLE connectors DROP COLUMN field_mapping")
