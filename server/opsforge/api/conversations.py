"""C1+C2: Conversations API — chat-first interface over the agent loop.

Conversations are threads of user+assistant messages. Posting a user message
triggers NL dispatch (resolve_nl) and creates an assistant reply that either
links to the dispatched run or surfaces disambiguation candidates.

All routes are org-scoped via the token principal; the underlying DB calls set
the RLS GUC so a workspace can never read another workspace's conversations.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from ..db import scope_to_org, session_factory
from ..dispatch import resolve_nl
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str = "New conversation"


class MessageCreate(BaseModel):
    content: str


class ConversationOut(BaseModel):
    id: UUID
    org_id: UUID
    title: str
    created_at: datetime
    created_by: UUID | None = None


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    run_id: UUID | None = None
    seq: int
    created_at: datetime


@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation(
    body: ConversationCreate,
    principal: Principal = Depends(require_token),
):
    """Open a new conversation thread."""
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        row = (
            await s.execute(
                text(
                    "INSERT INTO conversations (org_id, title, created_by) "
                    "VALUES (:org, :title, :by) RETURNING id, org_id, title, created_at, created_by"
                ),
                {
                    "org": principal.org_id,
                    "title": body.title,
                    "by": principal.user_id,
                },
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=500, detail="conversation creation failed")
    return dict(row._mapping)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(principal: Principal = Depends(require_token)):
    """List conversations for this org (newest first, limit 100)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT id, org_id, title, created_at, created_by "
                    "FROM conversations WHERE org_id = :org "
                    "ORDER BY created_at DESC LIMIT 100"
                ),
                {"org": principal.org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: UUID, principal: Principal = Depends(require_token)
):
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        row = (
            await s.execute(
                text(
                    "SELECT id, org_id, title, created_at, created_by "
                    "FROM conversations WHERE id = :id AND org_id = :org"
                ),
                {"id": conversation_id, "org": principal.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return dict(row._mapping)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: UUID, principal: Principal = Depends(require_token)
):
    """Return all messages in a conversation in order."""
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        # Verify the conversation belongs to this org.
        conv = (
            await s.execute(
                text("SELECT id FROM conversations WHERE id = :id AND org_id = :org"),
                {"id": conversation_id, "org": principal.org_id},
            )
        ).first()
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        rows = (
            await s.execute(
                text(
                    "SELECT id, conversation_id, org_id, role, content, run_id, seq, created_at "
                    "FROM messages WHERE conversation_id = :cid AND org_id = :org "
                    "ORDER BY seq"
                ),
                {"cid": conversation_id, "org": principal.org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.post("/{conversation_id}/messages", status_code=201)
async def post_message(
    conversation_id: UUID,
    body: MessageCreate,
    principal: Principal = Depends(require_token),
):
    """Send a user message and trigger NL dispatch.

    Returns the user message, an assistant reply message, and (when dispatch
    succeeds) a run_id the client can poll or stream via GET /runs/{id}/events.
    """
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")

    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        conv = (
            await s.execute(
                text("SELECT id FROM conversations WHERE id = :id AND org_id = :org"),
                {"id": conversation_id, "org": principal.org_id},
            )
        ).first()
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")

        # Get next seq number for this conversation.
        last_seq = (
            await s.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) FROM messages "
                    "WHERE conversation_id = :cid AND org_id = :org"
                ),
                {"cid": conversation_id, "org": principal.org_id},
            )
        ).scalar_one()

        # Insert user message.
        user_row = (
            await s.execute(
                text(
                    "INSERT INTO messages (org_id, conversation_id, role, content, seq) "
                    "VALUES (:org, :cid, 'user', :content, :seq) "
                    "RETURNING id, conversation_id, org_id, role, content, run_id, seq, created_at"
                ),
                {
                    "org": principal.org_id,
                    "cid": conversation_id,
                    "content": content,
                    "seq": last_seq + 1,
                },
            )
        ).first()

    # Dispatch via NL resolver (outside the DB transaction — this is an async
    # operation that may call LiteLLM; we don't want to hold a DB connection).
    dispatch_result = await resolve_nl(
        content,
        surface="chat",
        channel=str(conversation_id),
        user_id=principal.user_id,
    )

    run_id: str | None = dispatch_result.get("run_id")
    status = dispatch_result.get("status", "unknown")

    # Build the assistant reply content.
    if run_id:
        assistant_content = json.dumps({
            "type": "run_dispatched",
            "run_id": run_id,
            "status": status,
        })
    elif status == "ambiguous":
        candidates = dispatch_result.get("candidates", [])
        assistant_content = json.dumps({
            "type": "ambiguous",
            "candidates": candidates,
        })
    else:
        assistant_content = json.dumps({
            "type": "error",
            "detail": "Could not resolve a skill for that request.",
        })

    # Insert assistant reply.
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        assistant_row = (
            await s.execute(
                text(
                    "INSERT INTO messages "
                    "(org_id, conversation_id, role, content, run_id, seq) "
                    "VALUES (:org, :cid, 'assistant', :content, :run_id, :seq) "
                    "RETURNING id, conversation_id, org_id, role, content, run_id, seq, created_at"
                ),
                {
                    "org": principal.org_id,
                    "cid": conversation_id,
                    "content": assistant_content,
                    "run_id": run_id,
                    "seq": last_seq + 2,
                },
            )
        ).first()

    return {
        "user_message": dict(user_row._mapping),
        "assistant_message": dict(assistant_row._mapping),
        "run_id": run_id,
        "dispatch_status": status,
        "candidates": dispatch_result.get("candidates"),
    }
