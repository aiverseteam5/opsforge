"""Step E: GAP3 ops rules (freeze windows + priority approval) and skill upload."""

from __future__ import annotations

import datetime
import io
import json
import uuid
import zipfile

import pytest
from conftest import api_client
from sqlalchemy import text

from opsforge.config import DEFAULT_ORG_ID
from opsforge.db import session_factory
from opsforge.policy import freeze_active, min_approval_role, role_allows

# --------------------------- pure policy helpers ---------------------------


def test_freeze_active_respects_window_and_day():
    win = [{"days_of_week": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"}]
    mon_noon = datetime.datetime(2026, 6, 15, 12, 0)  # Monday
    sat_noon = datetime.datetime(2026, 6, 13, 12, 0)  # Saturday
    mon_eve = datetime.datetime(2026, 6, 15, 20, 0)
    assert freeze_active(win, mon_noon) is True
    assert freeze_active(win, sat_noon) is False  # not a freeze day
    assert freeze_active(win, mon_eve) is False  # outside window
    assert freeze_active([], mon_noon) is False


def test_min_approval_role_and_rank():
    policy = {"requires_role_for_priority": {"P1": "admin"}}
    assert min_approval_role(policy, "P1") == "admin"
    assert min_approval_role(policy, "P3") is None
    assert min_approval_role(policy, None) is None
    assert role_allows("operator", None) is True
    assert role_allows("operator", "admin") is False
    assert role_allows("admin", "admin") is True


# --------------------------- executor freeze gate --------------------------
@pytest.mark.usefixtures("db_required")
async def test_change_freeze_defers_execution():
    from opsforge.actions import execute_action

    # A skill whose policy freezes all day; an approved action under it must not run.
    manifest = {"policy": {"freeze_windows": [{"start": "00:00", "end": "23:59"}]}}
    async with session_factory().begin() as s:
        skill_id = (
            await s.execute(
                text(
                    "INSERT INTO skills (org_id,slug,version,manifest,source,enabled) "
                    "VALUES (:o,:slug,'0.1.0',CAST(:m AS jsonb),'org',true) RETURNING id"
                ),
                {"o": DEFAULT_ORG_ID, "slug": f"frozen-{uuid.uuid4().hex[:8]}",
                 "m": json.dumps(manifest)},
            )
        ).scalar_one()
        aid = (
            await s.execute(
                text(
                    "INSERT INTO actions (org_id,skill_id,action_class,tool,state,"
                    "policy_trace) VALUES (:o,:sk,'reversible','kubernetes.restart_pod',"
                    "'approved',CAST(:tr AS jsonb)) RETURNING id"
                ),
                {"o": DEFAULT_ORG_ID, "sk": skill_id, "tr": json.dumps({"allowed": True})},
            )
        ).scalar_one()

    result = await execute_action(aid)
    assert result.get("frozen") is True
    async with session_factory().begin() as s:
        state = (
            await s.execute(text("SELECT state FROM actions WHERE id=:i"), {"i": aid})
        ).scalar_one()
    assert state == "approved"  # deferred, not executed


# --------------------------- skill upload ---------------------------------
def _zip_skill() -> bytes:
    manifest = (
        "schema: opsforge/skill/v1\nslug: uploaded-skill\nversion: 0.1.0\n"
        "name: Uploaded skill\ntriggers: [manual]\n"
        "inputs:\n  - {name: query, type: string, required: true}\n"
        "report:\n  format: rca_v1\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("uploaded-skill/skill.yaml", manifest)
        zf.writestr("uploaded-skill/INSTRUCTIONS.md", "# Uploaded\nDo the thing.\n")
    return buf.getvalue()


async def _admin_headers() -> dict[str, str]:
    from opsforge.security import generate_token

    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        uid = (
            await s.execute(
                text("INSERT INTO users (org_id,email,name,role) "
                     "VALUES (:o,:e,'a','admin') RETURNING id"),
                {"o": DEFAULT_ORG_ID, "e": f"{uuid.uuid4().hex}@a.local"},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO api_tokens (org_id,user_id,token_hash,name) "
                 "VALUES (:o,:u,:h,'a')"),
            {"o": DEFAULT_ORG_ID, "u": uid, "h": token_hash},
        )
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.usefixtures("db_required")
async def test_upload_skill_zip_installs(auth_headers):
    admin = await _admin_headers()
    files = {"file": ("uploaded-skill.zip", _zip_skill(), "application/zip")}
    async with api_client() as client:
        # non-admin forbidden
        forbidden = await client.post("/api/v1/skills/install", headers=auth_headers, files=files)
        assert forbidden.status_code == 403
        # admin installs
        resp = await client.post("/api/v1/skills/install", headers=admin, files=files)
    assert resp.status_code == 200, resp.text
    async with api_client() as client:
        detail = await client.get("/api/v1/skills/uploaded-skill", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["source"] == "org"
