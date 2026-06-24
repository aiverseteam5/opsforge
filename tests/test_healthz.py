"""/healthz does a real DB round-trip. Requires the Compose db + migrate."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from opsforge.main import app

pytestmark = pytest.mark.usefixtures("db_required")


async def test_healthz_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
