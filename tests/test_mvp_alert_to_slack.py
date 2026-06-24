"""M3 acceptance (Phase-1 MVP): a fired alert produces an unprompted RCA in a
Slack channel. alert -> event-schedule match -> run -> agent -> Block Kit post.

Uses the offline HeuristicGateway + a fake Slack poster. Requires db+migrate.
"""

from __future__ import annotations

import uuid

import pytest
import yaml
from conftest import api_client
from heuristic_gateway import HeuristicGateway
from run_evals import run_scenario
from sqlalchemy import text

from opsforge.agent import run_agent
from opsforge.db import session_factory
from opsforge.skills import get_skill_by_id, install_builtin_skills
from opsforge.surfaces.slack import notify_run

pytestmark = pytest.mark.usefixtures("db_required")

SCENARIO = yaml.safe_load(
    open("skills/incident-investigation/evals/pool_exhaustion.yaml", encoding="utf-8")
)


async def _create_event_schedule(headers, channel: str) -> str:
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "payment-svc 5xx -> slack",
                "skill_slug": "incident-investigation",
                "trigger_kind": "event",
                "event_filter": {
                    "match": {"service": "payment-svc"},
                    "notify": {"surface": "slack", "channel": channel},
                    "query_template": "why is {service} throwing {symptom}",
                },
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_alert_fires_unprompted_rca_into_slack(auth_headers):
    await install_builtin_skills()
    # Populate the graph + change timeline (fixtures) for the org.
    await run_scenario("incident-investigation", SCENARIO, HeuristicGateway(), "demo")

    # Isolate from prior runs (the dev DB persists): exactly one event schedule.
    async with session_factory().begin() as s:
        await s.execute(text("DELETE FROM schedules WHERE trigger_kind='event'"))

    channel = "C-INCIDENTS"
    await _create_event_schedule(auth_headers, channel)

    # 1. Fire an alert (no auth bearer; HMAC skipped in dev).
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/webhooks/alert",
            json={"service": "payment-svc", "symptom": "5xx errors", "ref": "alert-7"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1
    run_id = uuid.UUID(resp.json()["dispatched"][0]["run_id"])

    # 2. The dispatched run targets the Slack channel and used the query template.
    async with session_factory().begin() as s:
        run = (
            await s.execute(
                text("SELECT skill_id, trigger FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).first()
    assert run.trigger["surface"] == "slack"
    assert run.trigger["channel"] == channel
    assert run.trigger["payload"]["query"] == "why is payment-svc throwing 5xx errors"

    # 3. Run the agent on the dispatched run (offline gateway stands in for the LLM).
    skill = await get_skill_by_id(run.skill_id)
    await run_agent(run_id, skill, HeuristicGateway())

    # 4. Deliver to Slack via a fake poster; assert a Block Kit RCA hit the channel.
    posted: list[tuple] = []

    async def fake_poster(ch, text_summary, blocks):
        posted.append((ch, text_summary, blocks))
        return {"ok": True}

    result = await notify_run(run_id, poster=fake_poster)
    assert result["ok"] is True
    assert len(posted) == 1
    ch, summary, blocks = posted[0]
    assert ch == channel
    assert "payment-svc" in summary
    blob = str(blocks)
    assert blocks[0]["type"] == "header"
    assert "payment-svc@rev7" in blob  # cites the deploy
    assert "suggested fix" in blob.lower()  # proposal rendered, not executed


async def test_notify_run_skips_non_slack_runs(auth_headers):
    await install_builtin_skills()
    result = await run_scenario(
        "incident-investigation", SCENARIO, HeuristicGateway(), "demo"
    )
    # run_scenario runs are surface=eval, not slack -> notify is a no-op.
    res = await notify_run(uuid.UUID(result["run_id"]))
    assert res["ok"] is False
    assert "skipped" in res


async def test_schedule_crud(auth_headers):
    await install_builtin_skills()
    async with api_client() as client:
        created = await client.post(
            "/api/v1/schedules",
            headers=auth_headers,
            json={
                "name": "nightly sweep",
                "skill_slug": "incident-investigation",
                "trigger_kind": "cron",
                "cron_expr": "0 2 * * *",
            },
        )
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        assert created.json()["next_run_at"] is not None  # cron computed

        listed = (await client.get("/api/v1/schedules", headers=auth_headers)).json()
        assert any(s["id"] == sid for s in listed)

        patched = await client.patch(
            f"/api/v1/schedules/{sid}", headers=auth_headers, json={"enabled": False}
        )
        assert patched.status_code == 200

        deleted = await client.delete(
            f"/api/v1/schedules/{sid}", headers=auth_headers
        )
        assert deleted.status_code == 204


async def test_cron_create_rejects_bad_expression(auth_headers):
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/schedules",
            headers=auth_headers,
            json={
                "name": "bad",
                "skill_slug": "incident-investigation",
                "trigger_kind": "cron",
                "cron_expr": "not a cron",
            },
        )
    assert resp.status_code == 400
