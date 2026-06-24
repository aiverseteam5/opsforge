"""Phase B: allow connector kind 'confluence' (first real knowledge source)

Revision ID: 0017_confluence_kind
Revises: 0016_connectors_rls
Create Date: 2026-06-24

The connectors.kind CHECK was an SRE-era enum. Phase B adds the first real knowledge-source
connector (Confluence), so widen the allowed set. Drop + recreate the CHECK with 'confluence'.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_confluence_kind"
down_revision: str | None = "0016_connectors_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_NEW = (
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack",
    "confluence", "custom",
)
_KINDS_OLD = (
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack", "custom",
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
