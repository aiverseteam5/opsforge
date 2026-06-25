"""G1 — the chat surface: conversations/messages are workspace-isolated (FORCE RLS), a user
turn spawns the existing agent run for the ops-assistant skill, and the thread is read back
with the run linked. No new secret surface; the agent runs through the existing loop.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from conftest import api_client, auth_headers  # noqa: F401 - auth_headers is a fixture
from sqlalchemy import text

from opsforge import chat
from opsforge.config import get_settings

pytestmark = pytest.mark.usefixtures("db_required")


async def _cleanup_org(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("messages", "conversations"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


# --------------------------------------------------------------------------- #
# conversations + messages are workspace-isolated (lib-level, two orgs)
# --------------------------------------------------------------------------- #
async def test_chat_is_workspace_isolated():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        conv_b = await chat.create_conversation(org_b, title="B private")
        await chat.add_message(org_b, uuid.UUID(conv_b["id"]), role="user", content="secret B")
        # A cannot see B's conversation or its messages
        assert all(c["id"] != conv_b["id"] for c in await chat.list_conversations(org_a))
        assert await chat.conversation_exists(org_a, uuid.UUID(conv_b["id"])) is False
        assert await chat.get_messages(org_a, uuid.UUID(conv_b["id"])) == []
        # B sees its own
        assert any(c["id"] == conv_b["id"] for c in await chat.list_conversations(org_b))
        msgs = await chat.get_messages(org_b, uuid.UUID(conv_b["id"]))
        assert [m["content"] for m in msgs] == ["secret B"]
    finally:
        await _cleanup_org(org_a)
        await _cleanup_org(org_b)


async def test_messages_seq_is_monotonic_per_conversation():
    org = str(uuid.uuid4())
    try:
        c = await chat.create_conversation(org)
        cid = uuid.UUID(c["id"])
        for i in range(3):
            await chat.add_message(org, cid, role="user", content=f"m{i}")
        seqs = [m["seq"] for m in await chat.get_messages(org, cid)]
        assert seqs == [1, 2, 3]
    finally:
        await _cleanup_org(org)


# --------------------------------------------------------------------------- #
# the API surface: a turn spawns the existing agent run for ops-assistant
# --------------------------------------------------------------------------- #
async def test_post_message_spawns_agent_run(auth_headers):  # noqa: F811
    from opsforge.skills import install_builtin_skills

    await install_builtin_skills()  # ensure ops-assistant exists in the default org
    org = get_settings().org_id
    try:
        async with api_client() as c:
            conv = (await c.post("/api/v1/chat/conversations", headers=auth_headers,
                                 json={"title": "t"})).json()
            r = await c.post(f"/api/v1/chat/conversations/{conv['id']}/messages",
                             headers=auth_headers, json={"content": "what is broken?"})
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["run_id"] and body["run_status"] == "queued"  # the agent loop spawned

            msgs = (await c.get(f"/api/v1/chat/conversations/{conv['id']}/messages",
                                headers=auth_headers)).json()
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant"]
        assert msgs[0]["content"] == "what is broken?"
        assert msgs[1]["run_id"] == body["run_id"]  # assistant turn links to its run
        # the linked run is a real queued run (the existing agent loop), no secret in the thread
        assert msgs[1]["run_status"] in ("queued", "running", "succeeded", "failed", "cancelled")
        assert "credential" not in str(msgs) and "token" not in str(msgs).lower()
        # a run row exists for it
        from opsforge.db import scope_to_org, session_factory
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            n = (await s.execute(text("SELECT count(*) FROM runs WHERE id=:r"),
                                 {"r": body["run_id"]})).scalar_one()
        assert n == 1
    finally:
        await _cleanup_org(org)


async def test_empty_message_and_missing_conversation(auth_headers):  # noqa: F811
    async with api_client() as c:
        conv = (await c.post("/api/v1/chat/conversations", headers=auth_headers,
                             json={})).json()
        try:
            empty = await c.post(f"/api/v1/chat/conversations/{conv['id']}/messages",
                                 headers=auth_headers, json={"content": "   "})
            assert empty.status_code == 400
            miss = await c.post(f"/api/v1/chat/conversations/{uuid.uuid4()}/messages",
                                headers=auth_headers, json={"content": "hi"})
            assert miss.status_code == 404
        finally:
            await _cleanup_org(get_settings().org_id)


# --------------------------------------------------------------------------- #
# robustness: a dispatch failure must NOT orphan a user message; concurrent
# posts must not collide on seq; the turn is written atomically.
# --------------------------------------------------------------------------- #
async def test_dispatch_failure_does_not_orphan_user_message(auth_headers, monkeypatch):  # noqa: F811
    """If the run can't be dispatched (503), the user turn is never persisted — the thread
    stays empty rather than showing a dangling user bubble with no answer."""
    import opsforge.api.chat as chat_api

    async def _no_dispatch(*a, **k):
        return None

    monkeypatch.setattr(chat_api, "create_run", _no_dispatch)
    org = get_settings().org_id
    try:
        async with api_client() as c:
            conv = (await c.post("/api/v1/chat/conversations", headers=auth_headers,
                                 json={})).json()
            r = await c.post(f"/api/v1/chat/conversations/{conv['id']}/messages",
                             headers=auth_headers, json={"content": "hi"})
            assert r.status_code == 503
            msgs = (await c.get(f"/api/v1/chat/conversations/{conv['id']}/messages",
                                headers=auth_headers)).json()
        assert msgs == []  # no orphaned user turn
    finally:
        await _cleanup_org(org)


async def test_concurrent_posts_get_distinct_contiguous_seqs():
    """Eight concurrent appends to the SAME conversation must yield distinct, contiguous seqs
    (the UNIQUE (conversation_id, seq) index + retry-on-conflict) — never a silent collision."""
    org = str(uuid.uuid4())
    try:
        c = await chat.create_conversation(org)
        cid = uuid.UUID(c["id"])
        await asyncio.gather(*[
            chat.add_message(org, cid, role="user", content=f"m{i}") for i in range(8)
        ])
        seqs = sorted(m["seq"] for m in await chat.get_messages(org, cid))
        assert seqs == list(range(1, 9))
    finally:
        await _cleanup_org(org)


async def test_add_turn_is_atomic():
    """add_turn writes the user turn and its run-linked assistant turn together: consecutive
    seqs, the assistant linked to the run, both present."""
    org = str(uuid.uuid4())
    try:
        c = await chat.create_conversation(org)
        cid = uuid.UUID(c["id"])
        run_id = str(uuid.uuid4())
        await chat.add_turn(org, cid, user_content="ping", run_id=run_id)
        msgs = await chat.get_messages(org, cid)
        assert [(m["role"], m["seq"]) for m in msgs] == [("user", 1), ("assistant", 2)]
        assert msgs[0]["content"] == "ping"
        assert msgs[1]["run_id"] == run_id
    finally:
        await _cleanup_org(org)


# --------------------------------------------------------------------------- #
# credential hardening: the agent report is redacted before it reaches the
# chat thread (the report obeys the same redact() chokepoint as every other
# agent boundary) — proven end-to-end through chat.get_messages.
# --------------------------------------------------------------------------- #
class _ReportGateway:
    """Submits a report whose hypothesis echoes a secret-like string."""

    def __init__(self, hypothesis: str):
        self._h = hypothesis

    async def chat(self, messages, tools, model):
        from opsforge.gateway import ChatResult, ToolCall

        return ChatResult(
            text="reporting",
            tool_calls=[ToolCall("s", "submit_report",
                                 {"hypothesis": self._h, "confidence": "low", "evidence": []})],
        )

    async def embedding(self, texts, model):
        return [[0.0] * 1536 for _ in texts]


async def test_report_is_redacted_in_the_thread():
    from opsforge.agent import run_agent
    from opsforge.db import session_factory

    org = str(uuid.uuid4())
    secret = "sk-supersecretvalue123"
    try:
        skill = {"id": None,
                 "manifest": {"context": {"graph": False}, "tools": [], "proposals": []},
                 "instructions": "", "trust_overrides": {}, "model": None}
        trigger = {"kind": "chat", "payload": {"query": "q"}}
        async with session_factory().begin() as s:
            run_id = (await s.execute(
                text("INSERT INTO runs (org_id, status, trigger) "
                     "VALUES (:o,'queued',CAST(:t AS jsonb)) RETURNING id"),
                {"o": org, "t": json.dumps(trigger)})).scalar_one()
        await run_agent(run_id, skill, _ReportGateway(f"leaked api_key: {secret} oops"))

        # link an assistant message to that run and read the thread back
        conv = await chat.create_conversation(org)
        cid = uuid.UUID(conv["id"])
        await chat.add_message(org, cid, role="assistant", content="", run_id=str(run_id))
        msgs = await chat.get_messages(org, cid)
        report = msgs[0]["report"]
        assert report is not None
        assert secret not in json.dumps(report)          # the secret never reaches the thread
        assert "***REDACTED***" in report["hypothesis"]  # the chokepoint fired
    finally:
        async with session_factory().begin() as s:
            await s.execute(text("DELETE FROM runs WHERE org_id=:o"), {"o": org})
        await _cleanup_org(org)


# --------------------------------------------------------------------------- #
# G4 legibility: get_messages surfaces the run's actions (legible, no leak)
# --------------------------------------------------------------------------- #
async def test_get_messages_surfaces_run_actions():
    from opsforge.db import scope_to_org, session_factory

    org = str(uuid.uuid4())
    try:
        conv = await chat.create_conversation(org)
        cid = uuid.UUID(conv["id"])
        run_id = str(uuid.uuid4())
        await chat.add_message(org, cid, role="user", content="roll it back")
        await chat.add_message(org, cid, role="assistant", content="", run_id=run_id)
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(
                text("INSERT INTO runs (id, org_id, status, trigger) "
                     "VALUES (:r,:o,'done',CAST('{}' AS jsonb))"), {"r": run_id, "o": org})
            await s.execute(
                text("INSERT INTO actions (org_id, run_id, action_class, tool, target_ref, "
                     "rollback, state, policy_trace) VALUES (:o,:r,'reversible',"
                     "'kubernetes.deploy_x','svc://x',CAST(:rb AS jsonb),'awaiting_approval',"
                     "CAST(:tr AS jsonb))"),
                {"o": org, "r": run_id, "rb": json.dumps({"tool": "kubernetes.redeploy_secret"}),
                 "tr": json.dumps({"reason": "trust=awaiting_approval; gated:production",
                                   "auto_execute": False})})
            await s.execute(
                text("INSERT INTO actions (org_id, run_id, action_class, tool, target_ref, "
                     "rollback, state, policy_trace) VALUES (:o,:r,'reversible',"
                     "'kubernetes.scale_x','svc://y',CAST(:rb AS jsonb),'succeeded',"
                     "CAST(:tr AS jsonb))"),
                {"o": org, "r": run_id, "rb": json.dumps({"tool": "kubernetes.scaleback_secret"}),
                 "tr": json.dumps({"reason": "auto:reversible_safe", "auto_execute": True})})

        msgs = await chat.get_messages(org, cid)
        amsg = next(m for m in msgs if m["role"] == "assistant")
        acts = amsg["actions"]
        assert acts is not None and len(acts) == 2
        gated = next(a for a in acts if a["tool"] == "kubernetes.deploy_x")
        assert gated["awaiting"] is True and gated["undoable"] is False
        assert "production" in (gated["reason"] or "")
        done = next(a for a in acts if a["tool"] == "kubernetes.scale_x")
        assert done["undoable"] is True and done["auto_executed"] is True
        assert done["awaiting"] is False
        # the view never leaks the rollback internals (tool/params)
        blob = json.dumps(acts)
        assert "redeploy_secret" not in blob and "scaleback_secret" not in blob

        # workspace isolation: another org sees nothing of this thread
        assert await chat.get_messages(str(uuid.uuid4()), cid) == []
    finally:
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(text("DELETE FROM actions WHERE org_id=:o"), {"o": org})
            await s.execute(text("DELETE FROM runs WHERE org_id=:o"), {"o": org})
        await _cleanup_org(org)
