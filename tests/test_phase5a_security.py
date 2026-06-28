"""Phase 5a security tests: timeline scope strip + delegation key decoupling.

These complement test_security.py (pure unit) with DB-backed integration tests.
All tests require a running Compose stack.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.usefixtures("db_required")


# --------------------------------------------------------------------------- #
# Timeline scope strip for delegation callers (T10)
# --------------------------------------------------------------------------- #


async def _insert_run_and_event(org_id: str, event_payload: dict) -> uuid.UUID:
    """Helper: insert a minimal run + one run_event. Returns run_id."""
    from sqlalchemy import text

    from opsforge.db import scope_to_org, session_factory

    run_id = uuid.uuid4()
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO runs (id, org_id, skill_slug, status) "
                "VALUES (:id, :org, 'test-skill', 'done')"
            ),
            {"id": run_id, "org": org_id},
        )
        await s.execute(
            text(
                "INSERT INTO run_events (org_id, run_id, seq, kind, payload) "
                "VALUES (:org, :run_id, 1, 'test_kind', CAST(:payload AS jsonb))"
            ),
            {
                "org": org_id,
                "run_id": run_id,
                "payload": json.dumps(event_payload),
            },
        )
    return run_id


async def test_timeline_strips_scope_for_delegation_callers(auth_headers: dict):
    """Delegation callers must not see 'scope' in timeline event payloads."""
    from datetime import UTC, datetime, timedelta

    from conftest import api_client
    from sqlalchemy import text

    from opsforge.config import get_settings
    from opsforge.db import scope_to_org, session_factory
    from opsforge.delegation import mint_delegation_token

    org_id = get_settings().org_id
    event_payload = {"summary": "agent proposed fix", "scope": ["tool_a", "tool_b"]}
    run_id = await _insert_run_and_event(org_id, event_payload)

    # Mint a delegation token and insert its jti.
    token, jti = mint_delegation_token(
        run_id=str(uuid.uuid4()),
        sub_run_id=str(uuid.uuid4()),
        org_id=org_id,
        scope=["tool_a"],
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO delegation_tokens "
                "(jti, org_id, iss, sub, scope, expires_at) "
                "VALUES (:jti, :org, :iss, :sub, CAST(:scope AS json), :exp)"
            ),
            {
                "jti": jti,
                "org": org_id,
                "iss": str(uuid.uuid4()),
                "sub": str(uuid.uuid4()),
                "scope": json.dumps(["tool_a"]),
                "exp": expires_at,
            },
        )

    async with api_client() as client:
        resp = await client.get(
            f"/api/v1/runs/{run_id}/timeline",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) >= 1
    assert "scope" not in events[0]["payload"], (
        "delegation caller must not receive 'scope' in event payloads"
    )
    assert "summary" in events[0]["payload"], "non-scope keys must still be present"


async def test_timeline_preserves_scope_for_regular_callers(auth_headers: dict):
    """Regular API token callers receive the full event payload including 'scope'."""
    from conftest import api_client

    from opsforge.config import get_settings

    org_id = get_settings().org_id
    event_payload = {"summary": "agent proposed fix", "scope": ["tool_a", "tool_b"]}
    run_id = await _insert_run_and_event(org_id, event_payload)

    async with api_client() as client:
        resp = await client.get(
            f"/api/v1/runs/{run_id}/timeline",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) >= 1
    assert "scope" in events[0]["payload"], (
        "regular API token callers must see full payload"
    )
