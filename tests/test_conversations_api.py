"""C1/C2/C4: Conversations API + postmortem endpoint tests.

Tests that require a database talk to the Compose `db` service.
Bring it up first with:  docker compose up -d db migrate
"""

from __future__ import annotations

import json
import uuid

import pytest
from conftest import api_client
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.db import scope_to_org, session_factory

pytestmark = pytest.mark.usefixtures("db_required")


# --------------------------------------------------------------------------- #
# C1: Conversations CRUD
# --------------------------------------------------------------------------- #


async def test_create_conversation(auth_headers):
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "Incident 2026-06-28: payment-svc"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Incident 2026-06-28: payment-svc"
    assert "id" in body
    assert "created_at" in body


async def test_list_conversations(auth_headers):
    async with api_client() as client:
        await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "List test"},
        )
        resp = await client.get("/api/v1/conversations", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    assert any(c["title"] == "List test" for c in items)


async def test_get_conversation_not_found(auth_headers):
    missing_id = str(uuid.uuid4())
    async with api_client() as client:
        resp = await client.get(
            f"/api/v1/conversations/{missing_id}", headers=auth_headers
        )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# C1/C2: Messages
# --------------------------------------------------------------------------- #


async def test_list_messages_empty(auth_headers):
    async with api_client() as client:
        cr = await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "Empty thread"},
        )
        conv_id = cr.json()["id"]
        resp = await client.get(
            f"/api/v1/conversations/{conv_id}/messages", headers=auth_headers
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_post_message_creates_user_and_assistant(auth_headers):
    """POST /messages creates a user message + an assistant reply.

    We don't assert that a run was dispatched (no skill is loaded in test DB),
    but we DO assert the shape of the response: both messages are returned and
    the assistant content is valid JSON.
    """
    async with api_client() as client:
        cr = await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "Test dispatch"},
        )
        conv_id = cr.json()["id"]

        resp = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            headers=auth_headers,
            json={"content": "why is payment-svc throwing 5xx?"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "user_message" in body
    assert "assistant_message" in body
    assert body["user_message"]["role"] == "user"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["user_message"]["content"] == "why is payment-svc throwing 5xx?"

    # assistant content is always JSON-parseable
    parsed = json.loads(body["assistant_message"]["content"])
    assert "type" in parsed

    # seq must be ascending
    assert body["user_message"]["seq"] < body["assistant_message"]["seq"]


async def test_post_message_empty_content_is_400(auth_headers):
    async with api_client() as client:
        cr = await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "Empty msg test"},
        )
        conv_id = cr.json()["id"]
        resp = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            headers=auth_headers,
            json={"content": "   "},
        )
    assert resp.status_code == 400


async def test_messages_are_ordered_by_seq(auth_headers):
    async with api_client() as client:
        cr = await client.post(
            "/api/v1/conversations",
            headers=auth_headers,
            json={"title": "Ordering test"},
        )
        conv_id = cr.json()["id"]
        # Send two messages
        for msg in ["first question", "second question"]:
            await client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                headers=auth_headers,
                json={"content": msg},
            )
        resp = await client.get(
            f"/api/v1/conversations/{conv_id}/messages", headers=auth_headers
        )

    messages = resp.json()
    seqs = [m["seq"] for m in messages]
    assert seqs == sorted(seqs), "messages must be returned in seq order"
    assert len(messages) >= 4  # 2 user + 2 assistant


# --------------------------------------------------------------------------- #
# C4: AI Postmortem endpoint
# --------------------------------------------------------------------------- #


async def _seed_terminal_run(org_id: str) -> str:
    """Insert a minimal done run and return its id."""
    run_id = str(uuid.uuid4())
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO runs (id, org_id, status, trigger) "
                "VALUES (:id, :org, 'done', CAST(:trigger AS jsonb))"
            ),
            {
                "id": run_id,
                "org": org_id,
                "trigger": json.dumps({"kind": "manual"}),
            },
        )
    return run_id


async def test_postmortem_enqueues_job(auth_headers):
    org_id = get_settings().org_id
    run_id = await _seed_terminal_run(org_id)

    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/runs/{run_id}/postmortem",
            headers=auth_headers,
            json={},
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "queued"
    assert "job_id" in body

    # Verify the job was created in the DB.
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        job = (
            await s.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE kind='postmortem' "
                    "AND payload->>'run_id' = :r"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    assert job == 1


async def test_postmortem_rejects_non_terminal_run(auth_headers):
    org_id = get_settings().org_id
    run_id = str(uuid.uuid4())
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO runs (id, org_id, status, trigger) "
                "VALUES (:id, :org, 'running', CAST(:trigger AS jsonb))"
            ),
            {
                "id": run_id,
                "org": org_id,
                "trigger": json.dumps({"kind": "manual"}),
            },
        )

    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/runs/{run_id}/postmortem",
            headers=auth_headers,
            json={},
        )
    assert resp.status_code == 409


async def test_postmortem_404_unknown_run(auth_headers):
    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/runs/{uuid.uuid4()}/postmortem",
            headers=auth_headers,
            json={},
        )
    assert resp.status_code == 404
