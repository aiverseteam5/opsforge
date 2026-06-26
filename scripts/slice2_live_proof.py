"""Slice-2 live proof — run INSIDE the api container (opsforge_app):

  docker compose exec -T api python - < scripts/slice2_live_proof.py

Proves governed OUTBOUND + iterative remediation end-to-end as the restricted role: commission ->
LEARN (now incl. the verify step) -> a "service down" ticket fires triage R0 -> R0 validates the
signal + proposes ONE GATED fix -> a HUMAN APPROVES -> it EXECUTES against the fake monitor
(stale_cleared) -> the worker chain hook spawns a follow-up R1 -> R1 OBSERVES the result + re-reads
ground truth -> reports RESOLVED, proposes nothing -> the case ends. Every consequential move
GATES; zero un-gated execution. Honesty bar: the monitor + corpus are TEST DATA.
"""

import asyncio
import json
import os
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
STATE_FILE = "/data/fakestate/monitoring.json"


async def admin_token():
    raw, h = generate_token()
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        # idempotent across re-runs: drop any prior seeded principal first (token FK -> user)
        await s.execute(text("DELETE FROM api_tokens WHERE org_id=:o AND name='s2-admin'"),
                        {"o": str(ORG)})
        await s.execute(text("DELETE FROM users WHERE org_id=:o AND email='s2@test'"),
                        {"o": str(ORG)})
        uid = (await s.execute(text(
            "INSERT INTO users (org_id,email,name,role) VALUES (:o,'s2@test','S2 Admin','admin') "
            "RETURNING id"), {"o": str(ORG)})).scalar_one()
        await s.execute(text(
            "INSERT INTO api_tokens (org_id,user_id,token_hash,name) VALUES (:o,:u,:h,'s2-admin')"),
            {"o": str(ORG), "u": str(uid), "h": h})
    return raw


async def seed_connector():
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM connectors WHERE org_id=:o AND kind='monitoring'"),
                        {"o": str(ORG)})
        # default environment 'prod' = UN-VOUCHED -> a config change to it gates (production_gate).
        # check_health is allowlisted so the executor's post-exec health gate actually fires.
        await s.execute(text(
            "INSERT INTO connectors (org_id,name,kind,transport,endpoint,tool_allowlist,"
            "environment,status) VALUES (:o,'monitoring (TEST)','monitoring','stdio',:e,"
            "CAST(:a AS jsonb),'prod','healthy')"),
            {"o": str(ORG), "e": f"{sys.executable} {FAKE}",
             "a": json.dumps(["get_service_status", "set_pull_interval", "check_health",
                              "verify_credential"])})


async def seed_schedule():
    skill = await get_skill("triage")
    ef = {"match": {"source": "monitoring-alert"},
          "query_template": "Triage ticket {incident_ref}: service {service} reported {summary}"}
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        # remove ANY prior triage schedule so the alert dispatches exactly one run
        await s.execute(text("DELETE FROM schedules WHERE org_id=:o AND skill_id=:sk"),
                        {"o": str(ORG), "sk": str(skill["id"])})
        await s.execute(text(
            "INSERT INTO schedules (org_id,skill_id,name,trigger_kind,event_filter,enabled) "
            "VALUES (:o,:sk,'s2-triage','event',CAST(:ef AS jsonb),true)"),
            {"o": str(ORG), "sk": str(skill["id"]), "ef": json.dumps(ef)})


async def wait_process(pk, n=300):
    for _ in range(n):
        proc = await kt._process(ORG, {"process_key": pk}, uuid.uuid4())
        if proc["found"] and proc.get("steps"):
            return proc
        await asyncio.sleep(1)
    return await kt._process(ORG, {"process_key": pk}, uuid.uuid4())


async def wait_run(run_id, n=300):
    for _ in range(n):
        await asyncio.sleep(1)
        async with session_factory().begin() as s:
            await scope_to_org(s, ORG)
            st = (await s.execute(text("SELECT status FROM runs WHERE id=:r"),
                                  {"r": run_id})).scalar_one_or_none()
        if st in ("done", "failed", "cancelled"):
            return st
    return st


async def first_action(run_id):
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        row = (await s.execute(text(
            "SELECT id, tool, state, action_class, policy_trace FROM actions WHERE run_id=:r "
            "ORDER BY created_at LIMIT 1"), {"r": run_id})).first()
    return dict(row._mapping) if row else None


async def wait_action(action_id, n=180):
    for _ in range(n):
        await asyncio.sleep(1)
        async with session_factory().begin() as s:
            await scope_to_org(s, ORG)
            row = (await s.execute(text("SELECT state, result FROM actions WHERE id=:i"),
                                   {"i": action_id})).first()
        if row and row._mapping["state"] in ("succeeded", "failed", "rolled_back"):
            return dict(row._mapping)
    return dict(row._mapping) if row else {}


async def wait_followup(parent, n=180):
    for _ in range(n):
        await asyncio.sleep(1)
        async with session_factory().begin() as s:
            await scope_to_org(s, ORG)
            row = (await s.execute(text(
                "SELECT id FROM runs WHERE parent_run_id=:p AND trigger->>'kind'='followup' "
                "ORDER BY created_at DESC LIMIT 1"), {"p": parent})).first()
        if row:
            return str(row._mapping["id"])
    return None


async def artifacts(run_id):
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        rep = (await s.execute(text("SELECT report_json FROM runs WHERE id=:r"),
                               {"r": run_id})).scalar_one() or {}
        tools = [t[0] for t in (await s.execute(text(
            "SELECT payload->>'tool' FROM run_events WHERE run_id=:r AND kind='tool_call'"),
            {"r": run_id})).all()]
        n_actions = (await s.execute(text("SELECT count(*) FROM actions WHERE run_id=:r"),
                                     {"r": run_id})).scalar_one()
    return tools, rep, n_actions


async def ungated_executions(root_run_id):
    """Count actions that reached an executed state WITHOUT a preceding human approval — the
    safety-critical invariant for governed outbound (must be 0)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG)
        acts = (await s.execute(text(
            "SELECT a.id, a.state FROM actions a JOIN runs r ON r.id=a.run_id "
            "WHERE r.org_id=:o AND (r.case_id=CAST(:c AS uuid) OR r.id=CAST(:c AS uuid))"),
            {"o": str(ORG), "c": root_run_id})).all()
        ungated = 0
        for aid, st in [(x[0], x[1]) for x in acts]:
            if st in ("succeeded", "executing"):
                approved = (await s.execute(text(
                    "SELECT count(*) FROM audit_log WHERE subject_ref=:a "
                    "AND event='action.approved'"
                ), {"a": str(aid)})).scalar_one()
                if approved == 0:
                    ungated += 1
    return ungated, len(acts)


async def main():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)  # checkout-svc starts at the default (stale) interval
    await install_builtin_skills()
    raw = await admin_token()
    hdr = {"Authorization": f"Bearer {raw}"}
    await seed_connector()
    await seed_schedule()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        print("== A1/A2 COMMISSION -> LEARN (with the verify step) ==")
        (await c.post("/api/v1/skills/triage/commission", headers=hdr)).json()
        proc = await wait_process(PK)
        steps_blob = " ".join(st.get("text", "") for st in proc.get("steps", [])).lower()
        learned_verify = any(w in steps_blob for w in ("verif", "re-read", "resolved", "close"))
        print(f"  validated process found={proc['found']} steps={len(proc.get('steps', []))} "
              f"verify_step_learned={learned_verify}")

        print("== A3 TICKET -> R0 (validate + ONE gated fix) ==")
        alert = {"source": "monitoring-alert", "incident_ref": "INC-700",
                 "service": "checkout-svc", "summary": "checkout-svc is DOWN"}
        disp = (await c.post("/api/v1/webhooks/alert", json=alert)).json()
        r0 = disp["dispatched"][0]["run_id"]
        print(f"  dispatched={disp['count']}  R0 -> {await wait_run(r0)}")
        tools0, rep0, _ = await artifacts(r0)
        print("  R0 tools:", tools0)
        print("  R0 hypothesis:", (rep0.get("hypothesis") or "")[:200])
        act = await first_action(r0)
        if not act:
            print("  !! R0 proposed no action (LLM variance) — re-run the proof")
            return
        rules = (act["policy_trace"] or {}).get("rules")
        print(f"  R0 action: {act['tool']} class={act['action_class']} state={act['state']} "
              f"rules={rules}")

        print("== A4 HUMAN APPROVES -> governed OUTBOUND (it actually EXECUTES) ==")
        appr = (await c.post(f"/api/v1/actions/{act['id']}/approve", headers=hdr)).json()
        print(f"  approve -> {appr}")
        done = await wait_action(act["id"])
        res = done.get("result") or {}
        print(f"  action -> {done.get('state')}  stale_cleared={res.get('stale_cleared')}  "
              f"TEST DATA={'TEST DATA' in str(res.get('source'))}")

        print("== A5/A6 chain hook -> follow-up R1 OBSERVES + verifies ==")
        r1 = await wait_followup(r0)
        if not r1:
            print("  !! no follow-up spawned")
            return
        print(f"  R1 (parent={r0[:8]}) -> {await wait_run(r1)}")
        tools1, rep1, n_actions1 = await artifacts(r1)
        print("  R1 tools:", tools1)
        print("  R1 hypothesis:", (rep1.get("hypothesis") or "")[:240])
        print(f"  R1 proposed actions (RESOLVED => 0): {n_actions1}")

        print("== A7 GATE INVARIANT across the case ==")
        ungated, n_acts = await ungated_executions(r0)
        print(f"  actions in case={n_acts}  un-gated executions (MUST be 0): {ungated}")

    async with session_factory().begin() as s:  # cleanup the seeded principal only
        await scope_to_org(s, ORG)
        await s.execute(text("DELETE FROM api_tokens WHERE name='s2-admin'"))
        await s.execute(text("DELETE FROM users WHERE email='s2@test'"))


asyncio.run(main())
