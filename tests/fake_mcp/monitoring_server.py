"""Fake 'monitoring' MCP server — REALISTIC TEST DATA, not a real monitoring system.

Contract-faithful stand-in for a generic monitoring tool (Grafana/Datadog-shaped), built to a
plausible published API so the SAME connector code runs against a real instance later. Used to
prove the validate-the-signal loop: it reports a service "up" so a "service down" ticket can be
caught as a stale/false alert.

Tools:
  get_service_status(service)            -> the live health read (always "up" here; ground truth)
  set_pull_interval(service, seconds)    -> the config-change write (stateful; returns old+new)
  verify_credential()                    -> health-check parity

Every payload carries a "TEST DATA" marker so it can never be mistaken for a real monitor.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-monitoring")

_MARK = "TEST DATA — synthetic monitor, not a real monitoring system"
_INTERVALS: dict[str, int] = {}  # per-service data-pull interval (seconds)


@mcp.tool()
def get_service_status(service: str) -> dict:
    """The service's CURRENT health from monitoring (ground truth). Always reports up here, so a
    'down' ticket surfaces as a report-vs-reality discrepancy (a stale/false alert)."""
    return {
        "service": service,
        "status": "up",
        "healthy": True,
        "last_check": "2026-06-25T14:00:00Z",
        "pull_interval_seconds": _INTERVALS.get(service, 300),
        "source": _MARK,
    }


@mcp.tool()
def set_pull_interval(service: str, seconds: int) -> dict:
    """Adjust the monitoring data source's pull/refresh interval for a service — the remediation
    for a stale data-pull. Stateful so a rollback (set_pull_interval with the prior value)
    restores it. Reversible by construction."""
    old = _INTERVALS.get(service, 300)
    _INTERVALS[service] = int(seconds)
    return {
        "ok": True,
        "service": service,
        "old_interval_seconds": old,
        "new_interval_seconds": int(seconds),
        "source": _MARK,
    }


@mcp.tool()
def verify_credential() -> dict:
    token = os.environ.get("MONITORING_TOKEN", "")
    return {"authenticated": token == "good-token" or token == "", "source": _MARK}


if __name__ == "__main__":
    mcp.run()
