"""Add delegation_scope column to runs for A2A scope enforcement.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("delegation_scope", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "delegation_scope")
