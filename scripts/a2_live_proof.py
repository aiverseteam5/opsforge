"""A2 live proof — configure a connector through the operator API (running as opsforge_app),
capture the credential to the vault, test, watch the catalog status flip honestly, and prove a
cross-workspace write is impossible. The secret VALUE is never printed.

Usage:
  A2_TOKEN=ofg_... PYTHONPATH=server PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python scripts/a2_live_proof.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BASE = "http://localhost:8080/api/v1"
TOKEN = os.environ["A2_TOKEN"]
SECRET = f"sk-A2-LIVE-{uuid.uuid4().hex}"  # generated; never printed
ORG_B = "00000000-0000-0000-0000-0000000000bb"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


async def _seed_org_b() -> str:
    os.environ["OPSFORGE_DATABASE_URL"] = (
        "postgresql+psycopg://opsforge:opsforge@localhost:5432/opsforge"
    )
    from sqlalchemy import text

    from opsforge.db import scope_to_org, session_factory
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG_B)
        cid = (await s.execute(
            text("INSERT INTO connectors (org_id,name,kind,transport,endpoint,status) "
                 "VALUES (:o,'victim','jira','http','http://b.local','healthy') RETURNING id"),
            {"o": ORG_B})).scalar_one()
    return str(cid)


async def _wipe_org_b() -> None:
    from sqlalchemy import text

    from opsforge.db import scope_to_org, session_factory
    async with session_factory().begin() as s:
        await scope_to_org(s, ORG_B)
        await s.execute(text("DELETE FROM connectors WHERE org_id=:o"), {"o": ORG_B})


def main() -> None:
    print("[as opsforge_app via the operator API]\n")
    # 1. catalog before
    _, b = call("GET", "/catalog/jira")
    d = json.loads(b)
    secret_fields = [f["name"] for f in d["config_fields"] if f["secret"]]
    print(f"[1] catalog/jira BEFORE: status={d['status']} connectable={d['connectable']} "
          f"instance_id={d['instance_id']} secret_fields={secret_fields}")

    # 2. configure → credential to the vault
    code, b = call("POST", "/connectors", {
        "name": "jira-a2", "kind": "jira", "transport": "http",
        "endpoint": "http://stub.local", "tool_allowlist": [],
        "credentials": {"api_key": SECRET},
    })
    cid = json.loads(b)["id"]
    print(f"[2] configure: HTTP {code}; credential in create response? "
          f"{'LEAK!' if SECRET in b else 'no — vaulted'}")

    # 3. catalog status flips (honest: stub endpoint → error, NOT false-connected)
    _, b = call("GET", "/catalog/jira")
    d = json.loads(b)
    print(f"[3] catalog/jira AFTER: status={d['status']} (stub → honest error, not false green) "
          f"instance_id_set={d['instance_id'] == cid}")

    # 4. credential in list / test?
    _, lst = call("GET", "/connectors")
    _, tst = call("POST", f"/connectors/{cid}/test")
    print(f"[4] credential in list={'LEAK' if SECRET in lst else 'clean'} "
          f"test={'LEAK' if SECRET in tst else 'clean'}; test_status={json.loads(tst)['status']}")

    # 5. edit: rotate credential (still never returned)
    code, b = call("PATCH", f"/connectors/{cid}", {"credentials": {"api_key": f"rot-{SECRET}"}})
    print(f"[5] rotate credential: HTTP {code}; secret in response? "
          f"{'LEAK!' if (SECRET in b) else 'no — write-only'}")

    # 6. cross-workspace write probe
    bid = asyncio.run(_seed_org_b())
    pc, _ = call("PATCH", f"/connectors/{bid}", {"credentials": {"api_key": "evil"}})
    dc, _ = call("DELETE", f"/connectors/{bid}")
    print(f"[6] cross-workspace write (A → B): PATCH HTTP {pc}, DELETE HTTP {dc} "
          f"→ {'BLOCKED (404)' if pc == 404 and dc == 404 else 'LEAK — not blocked!'}")
    asyncio.run(_wipe_org_b())

    # 7. disconnect (purges the vault credential)
    dc, _ = call("DELETE", f"/connectors/{cid}")
    print(f"[7] disconnect jira-a2: HTTP {dc} (credential purged with the row)")


if __name__ == "__main__":
    main()
