"""Fake 'staging' MCP server with stateful health — exercises the Phase-2 executor.

Tools let a test deterministically drive each execution path:
  apply_fix(target, outcome): outcome=ok -> healthy; exec_error -> raises (isError);
                              unhealthy -> applies but leaves the target unhealthy
  check_health(target): the executor's post-exec gate
  revert(target): the rollback tool (restores health)
Plus realistic proposal-tool names (rollback_deploy / restart_pod).

Health state lives per-process; the executor performs apply_fix + check_health in
one connector session, so they share state.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-staging")

_HEALTH: dict[str, bool] = {}


@mcp.tool()
def apply_fix(target: str, outcome: str = "ok") -> dict:
    if outcome == "exec_error":
        raise RuntimeError("simulated execution failure")
    _HEALTH[target] = outcome != "unhealthy"
    return {"ok": True, "applied": target, "outcome": outcome}


@mcp.tool()
def check_health(target: str) -> dict:
    return {"healthy": _HEALTH.get(target, True), "target": target}


@mcp.tool()
def revert(target: str) -> dict:
    _HEALTH[target] = True
    return {"ok": True, "reverted": target}


@mcp.tool()
def rollback_deploy(deployment: str, namespace: str = "prod") -> dict:
    return {"ok": True, "deployment": deployment, "rolled_back": True}


@mcp.tool()
def restart_pod(pod: str, namespace: str = "prod", fail: bool = False) -> dict:
    if fail:
        raise RuntimeError("restart failed")
    return {"ok": True, "pod": pod}


if __name__ == "__main__":
    mcp.run()
