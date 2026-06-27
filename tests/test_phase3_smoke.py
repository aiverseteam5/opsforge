"""Phase 3 Codify Loop smoke test — end-to-end + /cso security fix verification.

Pipeline under test:
  completed run events → handle_codify_skill (worker) → proposed skill in DB
  → GET /skills/proposed returns {items,total,page,page_size} envelope with manifest
  → POST /skills/{id}/approve enables the skill
  → POST /skills/{id}/reject sets rejected_at

Also spot-checks /cso security fixes:
  CSO-001  ingest path traversal guard rejects paths outside allowed root
  CSO-002  action mutations enforce org_id (no cross-org IDOR)
  CSO-003  list_proposed envelope shape + manifest field (main loop fix)
  CSO-005  expired tokens rejected with 401
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

import conftest
from opsforge.config import DEFAULT_ORG_ID
from opsforge.db import session_factory
from opsforge.gateway import ChatResult, ToolCall
from opsforge.security import generate_token, hash_token

# DB-backed tests carry db_required via auth_headers or an explicit param.
# Pure-unit tests (Slack notify, ingest guard) carry no DB marker.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_run(org_id: str) -> str:
    """Insert a minimal completed run with events the codify job needs."""
    rid = str(uuid.uuid4())
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO runs (id, org_id, status, skill_slug, trigger) "
                "VALUES (:id, :org, 'succeeded', 'incident-investigation', '{}'::jsonb)"
            ),
            {"id": rid, "org": org_id},
        )
        for seq, (kind, payload) in enumerate([
            ("tool_result", {"tool": "k8s.list_pods", "result": {"pods": ["api-1", "api-2"]}}),
            ("evidence", {"text": "Pod api-1 OOMKilled 3 times in the last hour"}),
            ("proposal", {"tool": "k8s.restart_pod", "params": {"pod": "api-1"}}),
            ("report", {"hypothesis": "Memory leak in api pod", "confidence": 0.85}),
        ]):
            await s.execute(
                text(
                    "INSERT INTO run_events (run_id, org_id, seq, kind, payload) "
                    "VALUES (:rid, :org, :seq, :kind, CAST(:payload AS jsonb))"
                ),
                {"rid": rid, "org": org_id, "seq": seq,
                 "payload": json.dumps(payload), "kind": kind},
            )
    return rid


def _fake_gateway(slug: str = "oom-pod-restart") -> Any:
    """Return a mock LiteLLMGateway that responds to extract_skill_data."""
    skill_yaml = (
        "schema: opsforge/skill/v1\n"
        f"slug: {slug}\n"
        "tools:\n"
        "  - k8s.list_pods\n"
        "  - k8s.restart_pod\n"
        "proposals:\n"
        "  - tool: k8s.restart_pod\n"
        "    class: reversible\n"
    )
    tc = ToolCall(
        id="tc-1",
        name="extract_skill_data",
        arguments={
            "slug": slug,
            "name": "OOM Pod Restart",
            "description": "Detects and restarts OOMKilled pods",
            "instructions_md": "## Steps\n1. List pods\n2. Restart OOMKilled ones",
            "skill_yaml": skill_yaml,
        },
    )
    gw = AsyncMock()
    gw.chat = AsyncMock(return_value=ChatResult(text=None, tool_calls=[tc]))
    gw.embedding = AsyncMock(return_value=[[0.1] * 1536])
    return gw


async def _get_proposed(org_id: str) -> list[dict]:
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, slug, source, enabled, manifest, rejected_at "
                    "FROM skills WHERE org_id = :org AND source = 'codified' "
                    "ORDER BY created_at DESC"
                ),
                {"org": org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Core loop: codify → list → approve
# ---------------------------------------------------------------------------

async def test_codify_loop_propose_then_approve(auth_headers: dict):
    """
    Full happy path:
      1. Seed a run with events
      2. handle_codify_skill produces a proposed skill
      3. GET /skills/proposed returns the correct envelope + manifest
      4. Mission Control query (page_size=1) returns correct total
      5. POST /skills/{id}/approve enables the skill
    """
    org_id = DEFAULT_ORG_ID
    run_id = await _seed_run(org_id)
    gw = _fake_gateway("oom-pod-restart")

    # Step 2 — run the codify worker job
    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})

    # Verify skill is in DB as proposed (enabled=false, not rejected)
    proposed = await _get_proposed(org_id)
    skill = next((s for s in proposed if s["slug"] == "oom-pod-restart"), None)
    assert skill is not None, "codify_skill did not create a proposed skill"
    assert skill["enabled"] is False
    assert skill["rejected_at"] is None
    manifest = skill["manifest"]
    assert manifest is not None
    assert manifest.get("run_id") == run_id

    skill_id = str(skill["id"])

    # Step 3 — GET /skills/proposed returns correct envelope
    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills/proposed", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    # CSO-003: must be an object with items/total/page/page_size
    assert isinstance(body, dict), f"expected dict envelope, got {type(body).__name__}"
    assert "items" in body and "total" in body, "missing pagination envelope keys"
    assert body["page"] == 1
    assert body["total"] >= 1

    item = next((i for i in body["items"] if i["id"] == skill_id), None)
    assert item is not None, "proposed skill not in items list"

    # CSO-003: manifest must be present on each item
    assert "manifest" in item, "manifest field missing from proposed skill item"
    assert item["manifest"] is not None
    assert item["manifest"].get("slug") == "oom-pod-restart"

    # Step 4 — Mission Control counter query (page=1, page_size=1)
    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills/proposed?page=1&page_size=1", headers=auth_headers)
    assert r.status_code == 200
    mc = r.json()
    assert mc["total"] >= 1           # counter shows pending count
    assert len(mc["items"]) <= 1      # page_size honoured

    # Step 5 — Approve the skill
    async with conftest.api_client() as c:
        r = await c.post(f"/api/v1/skills/{skill_id}/approve", headers=auth_headers)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["enabled"] is True
    assert approved["id"] == skill_id

    # Verify it no longer appears in proposed list
    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills/proposed", headers=auth_headers)
    ids_after = [i["id"] for i in r.json()["items"]]
    assert skill_id not in ids_after, "approved skill still listed as proposed"


async def test_codify_loop_approve_with_note(auth_headers: dict):
    """Approve with a review note stores it and the worker picks it up next run."""
    org_id = DEFAULT_ORG_ID
    run_id = await _seed_run(org_id)
    gw = _fake_gateway("oom-note-approve")

    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})

    proposed = await _get_proposed(org_id)
    skill = next(s for s in proposed if s["slug"] == "oom-note-approve")
    skill_id = str(skill["id"])

    async with conftest.api_client() as c:
        r = await c.post(
            f"/api/v1/skills/{skill_id}/approve",
            headers=auth_headers,
            json={"note": "Good skill — keep the restart proposal reversible"},
        )
    assert r.status_code == 200

    # Verify note persisted in DB
    async with session_factory().begin() as s:
        note = (
            await s.execute(
                text("SELECT review_note FROM skills WHERE id = :id"),
                {"id": skill_id},
            )
        ).scalar_one()
    assert note == "Good skill — keep the restart proposal reversible"


async def test_codify_loop_reject_with_note(auth_headers: dict):
    """Reject with a note stores it; next codify call receives the feedback."""
    org_id = DEFAULT_ORG_ID
    run_id = await _seed_run(org_id)
    gw_first = _fake_gateway("oom-note-reject")

    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw_first):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})

    proposed = await _get_proposed(org_id)
    skill = next(s for s in proposed if s["slug"] == "oom-note-reject")
    skill_id = str(skill["id"])

    async with conftest.api_client() as c:
        r = await c.post(
            f"/api/v1/skills/{skill_id}/reject",
            headers=auth_headers,
            json={"note": "Too broad — split into separate disk and memory skills"},
        )
    assert r.status_code == 200
    assert r.json()["rejected"] is True

    # Run a second codify job and verify the feedback appears in the system prompt
    run2 = await _seed_run(org_id)
    gw_second = _fake_gateway("oom-note-reject-v2")
    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw_second):
        await handle_codify_skill({"run_id": run2, "org_id": org_id})

    # The second gateway.chat call should have received the feedback in system content
    call_args = gw_second.chat.call_args
    system_content = call_args.kwargs["messages"][0]["content"]
    assert "Too broad" in system_content, "feedback note not injected into system prompt"
    assert "rejected" in system_content


async def test_codify_loop_reject(auth_headers: dict):
    """POST /skills/{id}/reject sets rejected_at and removes from proposed list."""
    org_id = DEFAULT_ORG_ID
    run_id = await _seed_run(org_id)
    gw = _fake_gateway("oom-reject-test")

    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})

    proposed = await _get_proposed(org_id)
    skill = next((s for s in proposed if s["slug"] == "oom-reject-test"), None)
    assert skill is not None
    skill_id = str(skill["id"])

    async with conftest.api_client() as c:
        r = await c.post(f"/api/v1/skills/{skill_id}/reject", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["rejected"] is True

    # No longer in proposed list
    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills/proposed", headers=auth_headers)
    ids_after = [i["id"] for i in r.json()["items"]]
    assert skill_id not in ids_after


async def test_codify_loop_idempotent(auth_headers: dict):
    """Running handle_codify_skill twice for the same run_id creates only one skill."""
    org_id = DEFAULT_ORG_ID
    run_id = await _seed_run(org_id)
    gw = _fake_gateway("oom-idempotent")

    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})
        await handle_codify_skill({"run_id": run_id, "org_id": org_id})

    async with session_factory().begin() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM skills "
                    "WHERE org_id=:org AND manifest->>'run_id'=:rid"
                ),
                {"org": org_id, "rid": run_id},
            )
        ).scalar_one()
    assert count == 1, f"expected 1 skill, got {count}"


async def test_codify_loop_no_events_skips(auth_headers: dict):
    """handle_codify_skill with a run that has no events does not create a skill."""
    org_id = DEFAULT_ORG_ID
    rid = str(uuid.uuid4())
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO runs (id, org_id, status, skill_slug, trigger) "
                "VALUES (:id, :org, 'succeeded', 'incident-investigation', '{}'::jsonb)"
            ),
            {"id": rid, "org": org_id},
        )
    gw = _fake_gateway()
    with patch("opsforge.gateway.LiteLLMGateway", return_value=gw):
        from opsforge.worker import handle_codify_skill
        await handle_codify_skill({"run_id": rid, "org_id": org_id})

    assert gw.chat.call_count == 0, "LLM should not be called with no events"


async def test_slack_notify_skill_proposed():
    """notify_skill_proposed calls the poster with correct fields."""
    from opsforge.surfaces.slack import notify_skill_proposed

    received: list[tuple] = []

    async def fake_poster(channel: str, text: str, blocks: list) -> dict:
        received.append((channel, text, blocks))
        return {"ok": True}

    # Without skill_review_channel configured (default empty), it skips
    result = await notify_skill_proposed("id-1", "my-skill", str(uuid.uuid4()))
    assert result["ok"] is False
    assert result["skipped"] == "no skill_review_channel configured"

    # With channel configured, it calls the poster
    with patch("opsforge.surfaces.slack.get_settings") as mock_cfg:
        mock_cfg.return_value.skill_review_channel = "#ops-skills"
        result = await notify_skill_proposed(
            "sk-123", "disk-check", "abc123", poster=fake_poster
        )
    assert result["ok"] is True
    assert result["channel"] == "#ops-skills"
    assert len(received) == 1
    channel, summary_text, blocks = received[0]
    assert channel == "#ops-skills"
    assert "disk-check" in summary_text
    assert any("disk-check" in json.dumps(b) for b in blocks)


# ---------------------------------------------------------------------------
# CSO-001: ingest path traversal guard
# ---------------------------------------------------------------------------

async def test_ingest_path_traversal_blocked():
    """ingest_directory rejects paths outside the configured knowledge root."""
    from opsforge.ingest import ingest_directory

    bad_paths = ["/etc", "/", "/root", "../../etc", "/home"]
    for bad in bad_paths:
        with pytest.raises(ValueError, match="outside the allowed knowledge root"):
            await ingest_directory(bad, org_id=DEFAULT_ORG_ID)


async def test_ingest_path_prefix_bypass_blocked(tmp_path):
    """'knowledgebase' must not match when root is 'knowledge'."""
    import os
    from opsforge.ingest import ingest_directory

    # Create a sibling dir that starts with the allowed root name
    sibling = tmp_path / "knowledgebase"
    sibling.mkdir()
    (sibling / "secret.md").write_text("# secret")

    with patch("opsforge.ingest.get_settings") as mock_cfg:
        mock_cfg.return_value.knowledge_base_path = str(tmp_path / "knowledge")
        with pytest.raises(ValueError, match="outside the allowed knowledge root"):
            await ingest_directory(str(sibling), org_id=DEFAULT_ORG_ID)


async def test_ingest_allowed_path_proceeds(tmp_path):
    """Paths within the knowledge root are accepted (even if empty)."""
    from opsforge.ingest import ingest_directory

    allowed = tmp_path / "knowledge" / "runbooks"
    allowed.mkdir(parents=True)

    with patch("opsforge.ingest.get_settings") as mock_cfg:
        mock_cfg.return_value.knowledge_base_path = str(tmp_path / "knowledge")
        # No .md files → returns 0 files without error
        result = await ingest_directory(
            str(allowed), org_id=DEFAULT_ORG_ID, embedder=AsyncMock(return_value=[])
        )
    assert result["files"] == 0


# ---------------------------------------------------------------------------
# CSO-002: IDOR — action mutations enforce org_id
# ---------------------------------------------------------------------------

async def test_action_approve_wrong_org_raises(db_required):
    """approve_action with an action from a different org raises ActionError."""
    from opsforge.actions import ActionError, approve_action

    other_org = str(uuid.uuid4())
    action_org = str(uuid.uuid4())

    # Seed an action belonging to action_org
    async with session_factory().begin() as s:
        aid = (
            await s.execute(
                text(
                    "INSERT INTO actions (org_id, action_class, tool, state, policy_trace) "
                    "VALUES (:org, 'reversible', 'k8s.restart_pod', "
                    "'awaiting_approval', '{\"allowed\": true}'::jsonb) RETURNING id"
                ),
                {"org": action_org},
            )
        ).scalar_one()

    # Attempt to approve it as other_org — must raise ActionError (not found)
    with pytest.raises(ActionError, match="action not found"):
        await approve_action(
            aid,
            org_id=uuid.UUID(other_org),
            actor_role="operator",
            actor="user:attacker",
        )


async def test_action_deny_wrong_org_raises(db_required):
    """deny_action with a cross-org action_id raises ActionError."""
    from opsforge.actions import ActionError, deny_action

    other_org = str(uuid.uuid4())
    action_org = str(uuid.uuid4())

    async with session_factory().begin() as s:
        aid = (
            await s.execute(
                text(
                    "INSERT INTO actions (org_id, action_class, tool, state, policy_trace) "
                    "VALUES (:org, 'reversible', 'k8s.restart_pod', "
                    "'awaiting_approval', '{\"allowed\": true}'::jsonb) RETURNING id"
                ),
                {"org": action_org},
            )
        ).scalar_one()

    with pytest.raises(ActionError, match="action not found"):
        await deny_action(aid, org_id=uuid.UUID(other_org), actor="user:attacker")


# ---------------------------------------------------------------------------
# CSO-005: token expiry
# ---------------------------------------------------------------------------

async def test_expired_token_rejected(db_required):
    """A token whose expires_at is in the past returns 401."""
    raw, token_hash = generate_token()
    expired_at = datetime.now(UTC) - timedelta(hours=1)

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name, expires_at) "
                "VALUES (:org, :h, 'expired-test', :exp)"
            ),
            {"org": DEFAULT_ORG_ID, "h": token_hash, "exp": expired_at},
        )

    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401, r.text
    assert "expired" in r.json().get("detail", "").lower()


async def test_valid_expiry_token_accepted(db_required):
    """A token with expires_at in the future is accepted."""
    raw, token_hash = generate_token()
    future = datetime.now(UTC) + timedelta(days=30)

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name, expires_at) "
                "VALUES (:org, :h, 'future-expiry-test', :exp)"
            ),
            {"org": DEFAULT_ORG_ID, "h": token_hash, "exp": future},
        )

    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text


async def test_no_expiry_token_accepted(db_required):
    """A token with expires_at = NULL (legacy) is accepted."""
    raw, token_hash = generate_token()

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name) "
                "VALUES (:org, :h, 'no-expiry-test')"
            ),
            {"org": DEFAULT_ORG_ID, "h": token_hash},
        )

    async with conftest.api_client() as c:
        r = await c.get("/api/v1/skills", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
