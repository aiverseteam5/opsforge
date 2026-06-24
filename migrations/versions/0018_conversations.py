"""G1: chat conversations + messages (Cursor-for-Ops chat agent)

Revision ID: 0018_conversations
Revises: 0017_confluence_kind
Create Date: 2026-06-24

The chat surface is a new SURFACE over the existing backend: a conversation is a thread, each
message a turn; an assistant message links to the agent run that produced it. Org-scoped under
the same FORCE-RLS + NULLIF fail-closed net as the rest of the plane (a workspace's chat is
never visible to another). IF NOT EXISTS so the from-scratch create_all + this migration agree.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_conversations"
down_revision: str | None = "0017_confluence_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    org_id      uuid NOT NULL,
    title       text NOT NULL DEFAULT 'New conversation',
    created_by  uuid
);
"""

_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    org_id          uuid NOT NULL,
    conversation_id uuid NOT NULL,
    role            varchar(20) NOT NULL,
    content         text NOT NULL DEFAULT '',
    run_id          uuid,
    seq             integer NOT NULL DEFAULT 0,
    CONSTRAINT ck_messages_role CHECK (role IN ('user', 'assistant', 'system'))
);
"""

_PREDICATE = "org_id = NULLIF(current_setting('opsforge.current_org', true), '')::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {table}_org_isolation ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def upgrade() -> None:
    op.execute(_CONVERSATIONS)
    op.execute(_MESSAGES)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_seq "
        "ON messages (conversation_id, seq);"
    )
    _rls("conversations")
    _rls("messages")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS messages_org_isolation ON messages;")
    op.execute("DROP POLICY IF EXISTS conversations_org_isolation ON conversations;")
    op.execute("DROP TABLE IF EXISTS messages;")
    op.execute("DROP TABLE IF EXISTS conversations;")
