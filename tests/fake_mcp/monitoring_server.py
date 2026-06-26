"""Fake 'monitoring' MCP server — REALISTIC TEST DATA, not a real monitoring system.

Contract-faithful stand-in for a generic monitoring tool (Grafana/Datadog-shaped), built to a
plausible published API so the SAME connector code runs against a real instance later. Used to
prove the validate-the-signal loop AND the iterative-remediation loop (Slice 2):

  - it reports a service "up" so a "service down" ticket can be caught as a stale/false alert; and
  - that reading is backed by a DATA-PULL interval — when the interval is too large the data lags
    and an alert off it is stale. Tightening the interval (the remediation) clears the staleness,
    which the agent can OBSERVE on a follow-up read to VERIFY the fix actually worked.

Tools:
  get_service_status(service)         -> live health read (always "up"; reports data_stale + lag)
  set_pull_interval(service, seconds) -> the config-change write (stateful; returns stale_cleared)
  check_health(target)                -> post-execution health gate the executor calls ({healthy})
  verify_credential()                 -> credential parity

Every payload carries a "TEST DATA" marker so it can never be mistaken for a real monitor.
"""

from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-monitoring")

_MARK = "TEST DATA — synthetic monitor, not a real monitoring system"
_DEFAULT_INTERVAL = 300  # per-service data-pull interval (seconds)
# A pull interval ABOVE this lags → the monitor's data is stale → a 'down' alert off it is false.
# The remediation tightens the interval to/below this, which CLEARS the staleness (observable).
# Set so any reasonable tightening from the 300s default clears it (one approved fix resolves the
# common case); a still-too-large value does not clear, so a genuine multi-step case is possible.
_STALE_THRESHOLD_S = 250
# State must persist across connector SESSIONS (and worker replicas): the executor's fix runs in
# one session and the follow-up VERIFY read in another, so a stateless module dict would make the
# fix invisible (and the monitor would contradict itself). The local stack mounts a shared volume
# at /data/fakestate on every api + worker container; when present, state lives there. On the host
# (unit tests) there is no such dir → an in-memory fallback (each spawn fresh → the default
# interval), preserving the tests' default-state assumption. TEST DATA either way.
_STATE_DIR = "/data/fakestate"
_STATE_FILE = os.path.join(_STATE_DIR, "monitoring.json") if os.path.isdir(_STATE_DIR) else None
_MEM: dict[str, int] = {}


def _all_intervals() -> dict[str, int]:
    if _STATE_FILE and os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                return {k: int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}
    return dict(_MEM)


def _interval(service: str) -> int:
    return _all_intervals().get(service, _DEFAULT_INTERVAL)


def _set_interval(service: str, seconds: int) -> None:
    state = _all_intervals()
    state[service] = int(seconds)
    if _STATE_FILE:
        try:
            # Atomic write: a concurrent reader never sees a torn file (which would decode-fail and
            # masquerade as "all default/stale"). os.replace is atomic on POSIX.
            tmp = f"{_STATE_FILE}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, _STATE_FILE)
            return
        except OSError:
            pass
    _MEM[service] = int(seconds)


def _is_stale(service: str) -> bool:
    """The data-pull lags when the interval exceeds the freshness threshold."""
    return _interval(service) > _STALE_THRESHOLD_S


@mcp.tool()
def get_service_status(service: str) -> dict:
    """The service's CURRENT health from monitoring (ground truth). Always reports up here, so a
    'down' ticket surfaces as a report-vs-reality discrepancy. `data_stale` says whether the
    data-pull is lagging (the stale-alert mechanism) — a follow-up read after the fix shows it
    cleared, which is how the agent verifies the remediation worked."""
    interval = _interval(service)
    stale = _is_stale(service)
    return {
        "service": service,
        "status": "up",
        "healthy": True,
        "data_stale": stale,
        "stale_lag_seconds": max(0, interval - _STALE_THRESHOLD_S) if stale else 0,
        "last_check": "2026-06-24T06:00:00Z (lagging)" if stale else "2026-06-26T14:00:00Z",
        "pull_interval_seconds": interval,
        "source": _MARK,
    }


@mcp.tool()
def set_pull_interval(service: str, seconds: int) -> dict:
    """Adjust the monitoring data source's pull/refresh interval for a service — the remediation
    for a stale data-pull. Stateful so a rollback (set_pull_interval with the prior value) restores
    it. `stale_cleared` is true only when this change moved the service stale → fresh (interval
    crossed to/below the threshold); a still-too-large interval does NOT clear it (so the agent may
    have to take another gated step)."""
    old = _interval(service)
    was_stale = _is_stale(service)
    _set_interval(service, int(seconds))
    now_stale = _is_stale(service)
    return {
        "ok": True,
        "service": service,
        "old_interval_seconds": old,
        "new_interval_seconds": int(seconds),
        "stale_cleared": was_stale and not now_stale,
        "data_stale": now_stale,
        "source": _MARK,
    }


@mcp.tool()
def check_health(target: str | None = None) -> dict:
    """Post-execution health gate the executor calls after a mutating action ({kind}.check_health).
    `target` is optional — a config change like set_pull_interval has no specific target_ref, so the
    executor passes target=None and this reports the monitor's overall health. Up here, so the
    remediation always passes the post-exec health check."""
    return {"target": target, "healthy": True, "source": _MARK}


@mcp.tool()
def verify_credential() -> dict:
    token = os.environ.get("MONITORING_TOKEN", "")
    return {"authenticated": token == "good-token" or token == "", "source": _MARK}


if __name__ == "__main__":
    mcp.run()
