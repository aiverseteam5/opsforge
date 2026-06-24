"""Bearer-token auth on a protected route. Requires the Compose db + migrate."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.db import session_factory
from opsforge.main import app
from opsforge.security import generate_token

pytestmark = pytest.mark.usefixtures("db_required")


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_missing_token_rejected():
    async with await _client() as client:
        resp = await client.get("/api/v1/runs")
    assert resp.status_code == 401


async def test_invalid_token_rejected():
    async with await _client() as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": "Bearer not-a-real-token"}
        )
    assert resp.status_code == 401


async def test_valid_token_accepted_and_touched():
    raw, token_hash = generate_token()
    org_id = get_settings().org_id
    name = f"test-{uuid.uuid4().hex}"

    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name) "
                "VALUES (:org_id, :token_hash, :name)"
            ),
            {"org_id": org_id, "token_hash": token_hash, "name": name},
        )

    async with await _client() as client:
        resp = await client.get(
            "/api/v1/runs", headers={"Authorization": f"Bearer {raw}"}
        )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)  # auth passed; contents depend on history

    # last_used_at was stamped.
    async with session_factory().begin() as s:
        last_used = (
            await s.execute(
                text("SELECT last_used_at FROM api_tokens WHERE token_hash = :h"),
                {"h": token_hash},
            )
        ).scalar_one()
    assert last_used is not None
