"""Unit tests for assert_restricted_role().

These tests mock the DB engine so no real Postgres is required.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run_assert(bypasses_rls: bool, environment: str) -> None:
    """Helper: patch engine + settings and call assert_restricted_role()."""
    row = {"bypasses_rls": bypasses_rls, "role": "opsforge"}

    mock_result = MagicMock()
    mock_result.mappings.return_value.one.return_value = row

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn

    mock_settings = MagicMock()
    mock_settings.environment = environment

    with (
        patch("opsforge.db.engine", return_value=mock_engine),
        patch("opsforge.db.get_settings", return_value=mock_settings),
    ):
        from opsforge.db import assert_restricted_role
        await assert_restricted_role()


async def test_assert_restricted_role_raises_in_production() -> None:
    """A superuser/bypassrls role in production must raise RuntimeError."""
    with pytest.raises(RuntimeError, match="bypass RLS"):
        await _run_assert(bypasses_rls=True, environment="production")


async def test_assert_restricted_role_warns_in_dev(caplog: pytest.LogCaptureFixture) -> None:
    """A superuser/bypassrls role in dev logs a warning instead of raising."""
    with caplog.at_level(logging.WARNING, logger="opsforge.db"):
        await _run_assert(bypasses_rls=True, environment="dev")

    assert any("bypass RLS" in r.message for r in caplog.records), (
        "Expected a warning about RLS bypass but got none"
    )


async def test_assert_restricted_role_passes_for_restricted_role() -> None:
    """A role without superuser/bypassrls must neither raise nor warn."""
    await _run_assert(bypasses_rls=False, environment="production")
