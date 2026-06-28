"""Shared test fixtures.

Tests that need a database talk to the Compose `db` service on localhost:5432.
Bring it up first with:  docker compose up -d db migrate
Override the URL with OPSFORGE_TEST_DATABASE_URL if your DB lives elsewhere.

DB-backed tests are skipped automatically when no database is reachable, so the
pure-unit tests (redaction, model metadata) still run anywhere.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from cryptography.fernet import Fernet

# psycopg3 async cannot run on Windows' default ProactorEventLoop; force the
# SelectorEventLoop so the DB-backed tests can connect from a Windows host.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Point settings at the test DB BEFORE any opsforge module reads them.
TEST_DB_URL = os.environ.get(
    "OPSFORGE_TEST_DATABASE_URL",
    "postgresql+psycopg://opsforge:opsforge@localhost:5432/opsforge",
)
os.environ.setdefault("OPSFORGE_DATABASE_URL", TEST_DB_URL)
# A real Fernet key by default so credential encryption works across the suite (A2 captures
# real credentials). Individual tests that need a fresh key still override + cache_clear.
os.environ.setdefault("OPSFORGE_FERNET_KEY", Fernet.generate_key().decode())


async def _db_reachable(url: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await eng.dispose()


@pytest.fixture
async def db_required() -> None:
    """Skip the test if the Compose database isn't up."""
    if not await _db_reachable(TEST_DB_URL):
        pytest.skip("database not reachable; run `docker compose up -d db migrate`")


@pytest.fixture
async def auth_headers(db_required: None) -> dict[str, str]:
    """Seed an API token and return an Authorization header for it."""
    from sqlalchemy import text

    from opsforge.config import get_settings
    from opsforge.db import session_factory
    from opsforge.security import generate_token

    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, token_hash, name, token_version) "
                "VALUES (:org, :h, 'test', 1)"
            ),
            {"org": get_settings().org_id, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def admin_auth_headers(db_required: None) -> dict[str, str]:
    """Seed an admin-role user + API token and return an Authorization header."""
    import uuid as _uuid

    from sqlalchemy import text as _text

    from opsforge.config import get_settings
    from opsforge.db import session_factory
    from opsforge.security import generate_token

    raw, token_hash = generate_token()
    user_id = str(_uuid.uuid4())
    org_id = get_settings().org_id
    async with session_factory().begin() as s:
        await s.execute(
            _text(
                "INSERT INTO users (id, org_id, email, role) "
                "VALUES (:id, :org, :email, 'admin')"
            ),
            {"id": user_id, "org": org_id, "email": f"admin-{user_id[:8]}@test.local"},
        )
        await s.execute(
            _text(
                "INSERT INTO api_tokens (org_id, user_id, token_hash, name, token_version) "
                "VALUES (:org, :uid, :h, 'admin-test', 1)"
            ),
            {"org": org_id, "uid": user_id, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


def api_client():
    from httpx import ASGITransport, AsyncClient

    from opsforge.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

