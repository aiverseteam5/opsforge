"""CASE C over-propose guard probe — run INSIDE the api container:

  docker compose exec -T api python - < scripts/slice1_caseC_probe.py

Reuses the workspace already commissioned by slice1_live_proof.py (the validated process is
learned). Fires a GENUINELY-HEALTHY ticket: the monitor reports UP and the ticket does NOT claim
an outage (it asks to confirm the service is fine). Per the strengthened triage instructions this
is CASE C -> the agent must PROPOSE NOTHING. Confirms the strengthened propose step does not
over-fire when there is no contested signal.
"""

import asyncio
import json
import os
import sys

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.db import scope_to_org, session_factory
from opsforge.main import app
from opsforge.skills import get_skill, install_builtin_skills

ORG = get_settings().org_id
FAKE = "/app/tests/fake_mcp/monitoring_server.py"


async def seed_connector():
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM connectors WHERE org_id=:o AND kind='monitoring'"),
                        {"o": str(ORG)})
        await s.execute(text(
            "INSERT INTO connectors (org_id,name,kind,transport,endpoint,tool_allowlist,"
            "environment,status) VALUES (:o,'monitoring (TEST)','monitoring','stdio',:e,"
            "CAST(:a AS jsonb),'prod','healthy')"),
            {"o": str(ORG), "e": f"{sys.executable} {FAKE}",
             "a": json.dumps(["get_service_status", "set_pull_interval", "verify_credential"])})


async def seed_schedule():
    skill = await get_skill("triage")
    ef = {"match": {"source": "monitoring-alert"},
          "query_template": "Triage ticket {incident_ref}: service {service} reported {summary}"}
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM schedules WHERE org_id=:o AND name='s1-triage'"),
                        {"o": str(ORG)})
        await s.execute(text(
            "INSERT INTO schedules (org_id,skill_id,name,trigger_kind,event_filter,enabled) "
            "VALUES (:o,:sk,'s1-triage','event',CAST(:ef AS jsonb),true)"),
            {"o": str(ORG), "sk": str(skill["id"]), "ef": json.dumps(ef)})


async def main():
    await install_builtin_skills()
    await seed_connector()
    await seed_schedule()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # GENUINELY HEALTHY: monitor reports UP and the ticket does NOT claim an outage.
        summary = os.environ.get(
            "PROBE_SUMMARY",
            "operating normally per user check; please confirm there is no outage")
        alert = {"source": "monitoring-alert", "incident_ref": "INC-OK1",
                 "service": "checkout-svc", "summary": summary}
        print(f"  ticket summary: {summary!r}")
        disp = (await c.post("/api/v1/webhooks/alert", json=alert)).json()
        print(f"  dispatched={disp['count']}")
        run_id = disp["dispatched"][0]["run_id"]
        st = "queued"
        for _ in range(300):
            await asyncio.sleep(1)
            async with session_factory().begin() as s:
                await scope_to_org(s, ORG)
                st = (await s.execute(text("SELECT status FROM runs WHERE id=:r"),
                                      {"r": run_id})).scalar_one()
            if st in ("done", "failed", "cancelled"):
                break
        print(f"  CASE C triage run -> {st}")

    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        rep = (await s.execute(text("SELECT report_json FROM runs WHERE id=:r"),
                               {"r": run_id})).scalar_one() or {}
        tools = [t[0] for t in (await s.execute(text(
            "SELECT payload->>'tool' FROM run_events WHERE run_id=:r AND kind='tool_call'"),
            {"r": run_id})).all()]
        actions = (await s.execute(text(
            "SELECT state, action_class, tool FROM actions WHERE run_id=:r"),
            {"r": run_id})).all()
    print("== tool calls ==", tools)
    print("== report hypothesis ==", (rep.get("hypothesis") or "")[:240])
    print("== actions (MUST be empty for CASE C) ==", [(t, st) for st, _, t in actions])
    print("  PASS (no over-propose):", len(actions) == 0)


asyncio.run(main())
