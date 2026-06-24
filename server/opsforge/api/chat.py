"""Chat API (Cursor-for-Ops, G1) — the conversational front door.

A turn: POST a user message → spawn an `ops-assistant` agent run (the EXISTING agent loop +
M7.6 vault gateway) → record an assistant message linked to that run. The agent's work
streams over the existing run-events SSE (GET /api/v1/runs/{run_id}/events). Every route is
workspace-scoped by the token principal; conversations/messages are FORCE-RLS isolated, so a
caller only ever sees their own workspace's threads (and, through them, their own runs).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import chat
from ..dispatch import create_run
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

_ASSISTANT_SKILL = "ops-assistant"


class NewConversation(BaseModel):
    title: str | None = None


class NewMessage(BaseModel):
    content: str


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: NewConversation, principal: Principal = Depends(require_token)
):
    return await chat.create_conversation(
        principal.org_id, title=body.title or "New conversation", created_by=principal.user_id
    )


@router.get("/conversations")
async def list_conversations(principal: Principal = Depends(require_token)):
    return await chat.list_conversations(principal.org_id)


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: UUID, principal: Principal = Depends(require_token)
):
    if not await chat.conversation_exists(principal.org_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return await chat.get_messages(principal.org_id, conversation_id)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def post_message(
    conversation_id: UUID, body: NewMessage, principal: Principal = Depends(require_token)
):
    """Record the user's turn, spawn the ops-assistant run for it, and record the assistant
    turn linked to that run. Returns the run id so the client can stream it."""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="empty message")
    if not await chat.conversation_exists(principal.org_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")

    # Dispatch the EXISTING agent loop FIRST (NL query → ops-assistant skill). Only once the
    # run is real do we persist the turn — so a dispatch failure never orphans a user message
    # (and a retry never double-posts the user turn against a half-written thread).
    dispatched = await create_run(
        _ASSISTANT_SKILL, {"query": content},
        trigger_kind="chat", surface="chat", channel=str(conversation_id),
        user_id=principal.user_id,
    )
    if dispatched is None:
        raise HTTPException(status_code=503, detail="ops-assistant skill not available")
    run_id = dispatched["run_id"]
    # User + assistant turns are written atomically: the thread can never show a half turn.
    assistant = await chat.add_turn(
        principal.org_id, conversation_id, user_content=content, run_id=run_id
    )
    return {"message_id": assistant["id"], "run_id": run_id, "run_status": dispatched["status"]}
