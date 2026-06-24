"""M2: runs API — dispatch enqueues a job; detail + SSE serve a finished run."""

from __future__ import annotations

import pytest
import yaml
from conftest import api_client
from heuristic_gateway import HeuristicGateway
from run_evals import run_scenario
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.db import scope_to_org, session_factory
from opsforge.skills import install_builtin_skills

pytestmark = pytest.mark.usefixtures("db_required")

SCENARIO = yaml.safe_load(
    open("skills/incident-investigation/evals/pool_exhaustion.yaml", encoding="utf-8")
)


async def test_post_run_creates_queued_run_and_job(auth_headers):
    await install_builtin_skills()
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/runs",
            headers=auth_headers,
            json={
                "skill_slug": "incident-investigation",
                "inputs": {"query": "why is payment-svc throwing 5xx"},
            },
        )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "queued"

    async with session_factory().begin() as s:
        # jobs is RLS-protected (M6.0): declare the org before reading it.
        await scope_to_org(s, get_settings().org_id)
        job = (
            await s.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE kind='run_agent' "
                    "AND payload->>'run_id' = :r"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    assert job == 1


async def test_unknown_skill_404(auth_headers):
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/runs",
            headers=auth_headers,
            json={"skill_slug": "does-not-exist", "inputs": {}},
        )
    assert resp.status_code == 404


async def test_run_detail_and_sse_after_completion(auth_headers):
    await install_builtin_skills()
    # Produce a finished run via the eval harness (no live worker/LLM needed).
    result = await run_scenario(
        "incident-investigation", SCENARIO, HeuristicGateway(), "heuristic-demo"
    )
    run_id = result["run_id"]

    async with api_client() as client:
        detail = await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "done"
        assert "payment-svc" in detail.json()["report_md"]

        # SSE replays the event stream and ends with a done sentinel.
        async with client.stream(
            "GET", f"/api/v1/runs/{run_id}/events", headers=auth_headers
        ) as stream:
            kinds = []
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    kinds.append(line.split(":", 1)[1].strip())
                if line.startswith("event: done"):
                    break
    assert "report" in kinds
    assert kinds[-1] == "done"


async def test_skills_api_lists_incident_investigation(auth_headers):
    await install_builtin_skills()
    async with api_client() as client:
        listed = (await client.get("/api/v1/skills", headers=auth_headers)).json()
        slugs = [s["slug"] for s in listed]
        assert "incident-investigation" in slugs
        detail = await client.get(
            "/api/v1/skills/incident-investigation", headers=auth_headers
        )
    assert detail.status_code == 200
    assert detail.json()["tool_count"] >= 5
