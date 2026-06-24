"""Chat conversations + messages (Cursor-for-Ops, G1) — the persistence behind the chat
surface. Pure DB ops, RLS-scoped: a conversation/message is only ever read or written under
the caller's workspace (the FORCE-RLS net from migration 0018 enforces it; the explicit
org predicate is defense-in-depth). The orchestration (spawning the agent run for a turn)
lives in the API layer; this module just stores the thread.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import scope_to_org, session_factory

# A message's seq is MAX(seq)+1 per conversation; (conversation_id, seq) is UNIQUE (0019), so
# two concurrent posts that read the same MAX collide on insert. We retry a bounded number of
# times, recomputing the next seq each attempt.
_SEQ_RETRIES = 6
_INSERT_MESSAGE = (
    "INSERT INTO messages (org_id, conversation_id, role, content, run_id, seq) "
    "VALUES (:o, :c, :role, :content, :run, "
    "  (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = :c)) "
    "RETURNING id, role, content, run_id, seq, created_at"
)


def _msg_dict(row: Any) -> dict[str, Any]:
    return {"id": str(row.id), "role": row.role, "content": row.content,
            "run_id": str(row.run_id) if row.run_id else None, "seq": row.seq,
            "created_at": row.created_at}


async def create_conversation(
    org_id: Any, *, title: str = "New conversation", created_by: str | None = None
) -> dict[str, Any]:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text("INSERT INTO conversations (org_id, title, created_by) "
                     "VALUES (:o, :t, :by) RETURNING id, title, created_at"),
                {"o": str(org_id), "t": title or "New conversation",
                 "by": str(created_by) if created_by else None},
            )
        ).one()
    return {"id": str(row.id), "title": row.title, "created_at": row.created_at}


async def list_conversations(org_id: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text("SELECT id, title, created_at FROM conversations "
                     "WHERE org_id = :o ORDER BY created_at DESC LIMIT :lim"),
                {"o": str(org_id), "lim": limit},
            )
        ).all()
    return [{"id": str(r.id), "title": r.title, "created_at": r.created_at} for r in rows]


async def conversation_exists(org_id: Any, conversation_id: UUID) -> bool:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        return (
            await s.execute(
                text("SELECT 1 FROM conversations WHERE id = :id AND org_id = :o"),
                {"id": str(conversation_id), "o": str(org_id)},
            )
        ).first() is not None


async def add_message(
    org_id: Any, conversation_id: UUID, *, role: str, content: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Append a message (seq monotonic per conversation). RLS-scoped; retries on a concurrent
    seq collision (the UNIQUE (conversation_id, seq) index)."""
    last: Exception | None = None
    for _ in range(_SEQ_RETRIES):
        try:
            async with session_factory().begin() as s:
                await scope_to_org(s, org_id)
                row = (
                    await s.execute(
                        text(_INSERT_MESSAGE),
                        {"o": str(org_id), "c": str(conversation_id), "role": role,
                         "content": content, "run": run_id},
                    )
                ).one()
            return _msg_dict(row)
        except IntegrityError as exc:  # concurrent same-seq insert — recompute + retry
            last = exc
    raise last  # type: ignore[misc]


async def add_turn(
    org_id: Any, conversation_id: UUID, *, user_content: str, run_id: str,
) -> dict[str, Any]:
    """Append a user turn and its run-linked assistant turn ATOMICALLY: a dispatch/insert
    failure can never leave a half-written turn in the thread. Both rows get monotonic seq in
    one transaction (the assistant's MAX+1 sees the just-inserted user row). Returns the
    assistant message. Retries on a concurrent seq collision."""
    last: Exception | None = None
    for _ in range(_SEQ_RETRIES):
        try:
            async with session_factory().begin() as s:
                await scope_to_org(s, org_id)
                await s.execute(
                    text(_INSERT_MESSAGE),
                    {"o": str(org_id), "c": str(conversation_id), "role": "user",
                     "content": user_content, "run": None},
                )
                row = (
                    await s.execute(
                        text(_INSERT_MESSAGE),
                        {"o": str(org_id), "c": str(conversation_id), "role": "assistant",
                         "content": "", "run": run_id},
                    )
                ).one()
            return _msg_dict(row)
        except IntegrityError as exc:  # concurrent same-seq insert — recompute + retry
            last = exc
    raise last  # type: ignore[misc]


async def get_messages(org_id: Any, conversation_id: UUID) -> list[dict[str, Any]]:
    """The thread, oldest first. Assistant messages carry their run's live status + report
    (the streamed work itself lives in run_events). The run is reachable ONLY via an
    RLS-scoped message, so a caller can only ever see their own workspace's runs here."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT m.id, m.role, m.content, m.run_id, m.seq, m.created_at, "
                    "       r.status AS run_status, r.report_json, r.report_md "
                    "FROM messages m LEFT JOIN runs r ON r.id = m.run_id "
                    "WHERE m.org_id = :o AND m.conversation_id = :c ORDER BY m.seq"
                ),
                {"o": str(org_id), "c": str(conversation_id)},
            )
        ).all()
    return [
        {
            "id": str(r.id), "role": r.role, "content": r.content,
            "run_id": str(r.run_id) if r.run_id else None, "seq": r.seq,
            "created_at": r.created_at,
            "run_status": r.run_status,
            "report": r.report_json,
            "report_md": r.report_md,
        }
        for r in rows
    ]
