"""Golden-eval runner: replay a scenario against a model and score it.

A scenario YAML defines the trigger, the fixture connectors/change, and
assertions. `run_scenario` sets up the graph from the fake MCP servers, runs the
real agent loop with the chosen gateway, and checks the assertions — producing a
per-(skill,model) scorecard. A model is "certified" for a skill when it passes.

  python evals/run_evals.py --skill incident-investigation --demo
  python evals/run_evals.py --skill incident-investigation --model claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Make `opsforge` (server/) and the fake MCP servers (tests/) importable when
# run as a standalone script.
_ROOT = Path(__file__).resolve().parent.parent
for sub in ("server", "tests"):
    p = str(_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from opsforge.agent import run_agent  # noqa: E402
from opsforge.config import get_settings  # noqa: E402
from opsforge.connectors import load_connector  # noqa: E402
from opsforge.db import session_factory  # noqa: E402
from opsforge.gateway import ModelGateway  # noqa: E402
from opsforge.graph import sync_connector  # noqa: E402
from opsforge.skills import get_skill, install_builtin_skills  # noqa: E402


async def _setup_connector(org_id: str, name: str, kind: str, server: str, allow: list[str]) -> str:
    from fake_mcp import server_command

    from opsforge.ops_model import load_starter_mapping

    mapping = load_starter_mapping(kind)  # ops connectors get their starter pack
    async with session_factory().begin() as s:
        # Remove any prior connector with this name OR kind so exactly one
        # connector per kind exists (keeps load_connectors_by_kind deterministic).
        await s.execute(
            text("DELETE FROM connectors WHERE org_id=:o AND (name=:n OR kind=:k)"),
            {"o": org_id, "n": name, "k": kind},
        )
        cid = (
            await s.execute(
                text(
                    "INSERT INTO connectors (org_id,name,kind,transport,endpoint,"
                    "tool_allowlist,field_mapping,status) VALUES (:o,:n,:k,'stdio',:e,"
                    "CAST(:a AS jsonb),CAST(:m AS jsonb),'healthy') RETURNING id"
                ),
                {
                    "o": org_id,
                    "n": name,
                    "k": kind,
                    "e": server_command(server),
                    "a": json.dumps(allow),
                    "m": json.dumps(mapping) if mapping else None,
                },
            )
        ).scalar_one()
    return str(cid)


async def _seed_change(org_id: str, connector_id: str, change: dict[str, Any]) -> None:
    async with session_factory().begin() as s:
        await s.execute(
            text(
                "INSERT INTO changes (org_id,kind,ref,summary,target_keys,occurred_at,"
                "source_connector_id) VALUES (:o,:k,:r,:s,:t,now(),:c) "
                "ON CONFLICT (source_connector_id,kind,ref) DO UPDATE "
                "SET summary=EXCLUDED.summary, target_keys=EXCLUDED.target_keys"
            ),
            {
                "o": org_id,
                "k": change.get("kind", "deploy"),
                "r": change["ref"],
                "s": change.get("summary"),
                "t": change.get("target_keys"),
                "c": connector_id,
            },
        )


def _allowlist_for_kind(manifest: dict[str, Any], kind: str) -> list[str]:
    return [
        t["tool"].split(".", 1)[1]
        for t in manifest.get("tools", []) or []
        if t["tool"].startswith(f"{kind}.")
    ]


async def _create_run(org_id: str, skill_id: str, trigger_inputs: dict[str, Any]) -> str:
    trigger = {"kind": "manual", "payload": trigger_inputs, "surface": "eval"}
    async with session_factory().begin() as s:
        run_id = (
            await s.execute(
                text(
                    "INSERT INTO runs (org_id,skill_id,status,trigger) "
                    "VALUES (:o,:s,'queued',CAST(:t AS jsonb)) RETURNING id"
                ),
                {"o": org_id, "s": skill_id, "t": json.dumps(trigger)},
            )
        ).scalar_one()
    return str(run_id)


async def _tool_call_count(run_id: str) -> int:
    async with session_factory().begin() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM run_events WHERE run_id=:r AND kind='tool_call'"
                ),
                {"r": run_id},
            )
        ).scalar_one()


async def _tool_call_names(run_id: str) -> list[str]:
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload->>'tool' FROM run_events WHERE run_id=:r AND kind='tool_call'"
                ),
                {"r": run_id},
            )
        ).all()
    return [r[0] for r in rows if r[0]]


async def _executed_actions(run_id: str) -> int:
    async with session_factory().begin() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM actions WHERE run_id=:r AND state IN "
                    "('executing','succeeded','dry_run_done')"
                ),
                {"r": run_id},
            )
        ).scalar_one()


def _check_assertions(
    assertions: dict[str, Any], report: dict[str, Any], tool_calls: int, executed: int,
    tool_names: list[str] | None = None,
) -> dict[str, bool]:
    report_text = json.dumps(report).lower()
    hypothesis = (report.get("hypothesis") or "").lower()
    checks: dict[str, bool] = {}

    for term in assertions.get("hypothesis_must_mention", []) or []:
        checks[f"hypothesis_mentions:{term}"] = term.lower() in hypothesis
    for term in assertions.get("report_must_mention", []) or []:
        checks[f"report_mentions:{term}"] = term.lower() in report_text
    if assertions.get("must_cite_change_ref"):
        ref = assertions["must_cite_change_ref"]
        checks[f"cites_change:{ref}"] = ref.lower() in report_text
    # A behaviour assertion: the run actually CALLED these tools (e.g. a follow-up must re-read
    # ground truth before declaring RESOLVED — so the honesty bar is tested, not just prompted).
    for tool in assertions.get("must_call_tools", []) or []:
        checks[f"called:{tool}"] = tool in (tool_names or [])
    if "max_tool_calls" in assertions:
        checks["within_tool_budget"] = tool_calls <= assertions["max_tool_calls"]
    if assertions.get("forbid_mutating_calls"):
        checks["no_mutating_execution"] = executed == 0
    return checks


async def _seed_knowledge(org_id: str, corpus: str | None, process_key: str | None) -> None:
    """Honor a scenario's `fixtures.knowledge`: LEARN the operation before the run by ingesting
    the named corpus then reconciling + generating its validated process — the same M6 path the
    commission step uses, so an LLM-keyed triage eval actually has the process it must consult
    (rather than the fixture being silently ignored)."""
    if not corpus or not process_key:
        return
    from opsforge.ingest import configured_embedder, ingest_directory
    from opsforge.processes import configured_drafter, generate_process
    from opsforge.reconcile import configured_detector, reconcile_process

    await ingest_directory(_ROOT / corpus, org_id=org_id, embedder=configured_embedder())
    await reconcile_process(org_id, process_key, detector=await configured_detector(org_id))
    await generate_process(org_id, process_key, drafter=configured_drafter())


async def run_scenario(
    skill_slug: str, scenario: dict[str, Any], gateway: ModelGateway, model: str
) -> dict[str, Any]:
    org_id = get_settings().org_id
    skill = await get_skill(skill_slug)
    if skill is None:
        raise RuntimeError(f"skill {skill_slug} not installed")
    manifest = skill["manifest"]

    fixtures = scenario.get("fixtures", {})
    k8s_connector_id = None
    for conn in fixtures.get("connectors", []) or []:
        allow = _allowlist_for_kind(manifest, conn["kind"])
        cid = await _setup_connector(
            org_id, conn["name"], conn["kind"], conn["server"], allow
        )
        connector = await load_connector(__import__("uuid").UUID(cid), org_id)
        await sync_connector(connector)
        if conn["kind"] == "kubernetes":
            k8s_connector_id = cid

    if fixtures.get("change"):
        await _seed_change(org_id, k8s_connector_id, fixtures["change"])

    if fixtures.get("knowledge"):
        kn = fixtures["knowledge"]
        await _seed_knowledge(org_id, kn.get("corpus"), kn.get("process_key"))

    run_id = await _create_run(org_id, skill["id"], scenario["trigger"])
    await run_agent(__import__("uuid").UUID(run_id), skill, gateway, model=model)

    async with session_factory().begin() as s:
        report = (
            await s.execute(
                text("SELECT report_json FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).scalar_one()

    tool_calls = await _tool_call_count(run_id)
    executed = await _executed_actions(run_id)
    tool_names = await _tool_call_names(run_id)
    checks = _check_assertions(
        scenario.get("assertions", {}), report or {}, tool_calls, executed, tool_names
    )
    return {
        "scenario": scenario.get("name", "unnamed"),
        "model": model,
        "run_id": run_id,
        "passed": all(checks.values()),
        "checks": checks,
        "tool_calls": tool_calls,
    }


def _load_scenarios(skill_slug: str) -> list[dict[str, Any]]:
    skill_dir = _ROOT / get_settings().skills_dir / skill_slug
    return [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted((skill_dir / "evals").glob("*.yaml"))
    ]


async def _amain(skill_slug: str, model: str, demo: bool) -> int:
    await install_builtin_skills()
    if demo:
        from heuristic_gateway import HeuristicGateway

        gateway: ModelGateway = HeuristicGateway()
        model = model or "heuristic-demo"
    else:
        from opsforge.gateway import LiteLLMGateway

        gateway = LiteLLMGateway()

    scorecard = []
    for scenario in _load_scenarios(skill_slug):
        if demo and scenario.get("requires_llm"):
            # The keyless heuristic gateway is wired to the incident-investigation topology and
            # cannot drive an arbitrary learned operation; an LLM-keyed scenario would FAIL it
            # spuriously. Skip it (don't count it) rather than overstate the keyless scorecard.
            print(f"[SKIP] {scenario.get('name', 'unnamed')} — requires an LLM gateway "
                  "(not runnable under the keyless --demo heuristic)")
            continue
        result = await run_scenario(skill_slug, scenario, gateway, model)
        scorecard.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['scenario']} ({result['tool_calls']} tool calls)")
        for name, ok in result["checks"].items():
            print(f"    {'ok ' if ok else 'XX '} {name}")

    passed = sum(1 for r in scorecard if r["passed"])
    print(f"\nScorecard for {model}: {passed}/{len(scorecard)} scenarios passed")
    return 0 if passed == len(scorecard) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run golden evals for a skill")
    parser.add_argument("--skill", default="incident-investigation")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--demo", action="store_true", help="use the offline heuristic gateway"
    )
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_amain(args.skill, args.model, args.demo))


if __name__ == "__main__":
    raise SystemExit(main())
