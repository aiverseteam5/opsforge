"""Slice-1 live proof — run INSIDE the api container (opsforge_app):

  docker compose exec -T api python - < scripts/slice1_live_proof.py

Proves the differentiated core end-to-end on ONE operation (service-health triage), as the
restricted role: commission a workspace from the (extended) triage manifest -> the agent LEARNS
the operation from the declared corpus (M6) -> a "service down" ticket fires the triage skill ->
the agent validates the signal against ground truth via the contract-faithful FAKE monitoring
connector (reports UP) -> surfaces report-vs-reality (contested) -> proposes ONE GATED config
change. Read-heavy; zero auto-executed. Honesty bar: the monitor + corpus are TEST DATA.
"""

import asyncio
import json
import sys
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from opsforge import knowledge_tools as kt
from opsforge.config import get_settings
from opsforge.db import scope_to_org, session_factory
from opsforge.main import app
from opsforge.security import generate_token
from opsforge.skills import get_skill, install_builtin_skills

ORG = get_settings().org_id
FAKE = "/app/tests/fake_mcp/monitoring_server.py"
PK = "service-health-triage"


async def admin_token():
    raw, h = generate_token()
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        uid = (await s.execute(text(
            "INSERT INTO users (org_id,email,name,role) VALUES (:o,'s1@test','S1 Admin','admin') "
            "RETURNING id"), {"o": str(ORG)})).scalar_one()
        await s.execute(text(
            "INSERT INTO api_tokens (org_id,user_id,token_hash,name) VALUES (:o,:u,:h,'s1-admin')"),
            {"o": str(ORG), "u": str(uid), "h": h})
    return raw


async def seed_connector():
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM connectors WHERE org_id=:o AND kind='monitoring'"),
                        {"o": str(ORG)})
        # default environment 'prod' = UN-VOUCHED -> a config change to it gates (production_gate)
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


async def wait_process(pk, n=300):
    """Poll for the validated process (the LEARN signal) rather than the job endpoint — robust
    to real-LLM commission latency."""
    for _ in range(n):
        proc = await kt._process(ORG, {"process_key": pk}, uuid.uuid4())
        if proc["found"] and proc.get("steps"):
            return proc
        await asyncio.sleep(1)
    return await kt._process(ORG, {"process_key": pk}, uuid.uuid4())


async def main():
    await install_builtin_skills()
    raw = await admin_token()
    hdr = {"Authorization": f"Bearer {raw}"}
    await seed_connector()
    await seed_schedule()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        print("== A1/A2 COMMISSION -> LEARN the operation ==")
        com = (await c.post("/api/v1/skills/triage/commission", headers=hdr)).json()
        print(f"  commission job {com['job_id'][:8]} enqueued; waiting for the learned process...")
        proc = await wait_process(PK)
        print(f"  validated process found={proc['found']} steps={len(proc.get('steps', []))}")

        print("== A3 TICKET webhook -> event-triggered triage run ==")
        # service 'checkout-svc' so only the s1-triage schedule (source=monitoring-alert) matches
        alert = {"source": "monitoring-alert", "incident_ref": "INC-501",
                 "service": "checkout-svc", "summary": "checkout-svc is DOWN"}
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
        print(f"  triage run -> {st}")

    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        rep = (await s.execute(text("SELECT report_json FROM runs WHERE id=:r"),
                               {"r": run_id})).scalar_one() or {}
        tools = [t[0] for t in (await s.execute(text(
            "SELECT payload->>'tool' FROM run_events WHERE run_id=:r AND kind='tool_call'"),
            {"r": run_id})).all()]
        actions = (await s.execute(text(
            "SELECT state, action_class, tool, policy_trace FROM actions WHERE run_id=:r"),
            {"r": run_id})).all()
    print("== A4 validate-the-signal: agent tool calls ==", tools)
    print("== A5 report (report-vs-reality, contested) ==")
    print("  hypothesis:", (rep.get("hypothesis") or "")[:300])
    print("  confidence:", rep.get("confidence"))
    print("== A6/A7 actions (one GATED, zero auto-executed) ==")
    for state, cls, tool, trace in actions:
        print(f"  - {tool} class={cls} state={state} rules={(trace or {}).get('rules')}")
    n_exec = sum(1 for state, *_ in actions if state in ("succeeded", "executing"))
    print("  auto-executed actions (MUST be 0):", n_exec)

    async with session_factory().begin() as s:  # cleanup the seeded principal only
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM api_tokens WHERE name='s1-admin'"))
        await s.execute(text("DELETE FROM users WHERE email='s1@test'"))


asyncio.run(main())
