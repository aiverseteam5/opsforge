"""G1 hardening: make messages (conversation_id, seq) UNIQUE

Revision ID: 0019_messages_seq_unique
Revises: 0018_conversations
Create Date: 2026-06-25

add_message computes seq as MAX(seq)+1 per conversation — the same pattern run_events uses.
run_events is protected by a UNIQUE index (ix_run_events_run_seq) that turns a concurrent
duplicate into a hard error; messages shipped (0018) with a plain, non-unique index. Under
READ COMMITTED two concurrent posts to the SAME conversation can read the same MAX(seq) and
both insert it, silently corrupting thread order. This swaps the index for a UNIQUE one so the
collision fails loudly (add_message retries on the conflict). DROP-then-CREATE so it also heals
a DB that already has the non-unique index from 0018.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_messages_seq_unique"
down_revision: str | None = "0018_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_conversation_seq;")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_conversation_seq "
        "ON messages (conversation_id, seq);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_conversation_seq;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_seq "
        "ON messages (conversation_id, seq);"
    )
