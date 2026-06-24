"""Fake Kubernetes MCP server (stdio). Serves a small fixed prod topology:

  namespace prod
    deployment payment-svc  (image :v42, revision 7) -- a recent deploy
      pod payment-svc-7f9c  -> node-1
      pod payment-svc-8a2b  -> node-2   (CrashLoopBackOff)

Enough for the graph mapper to build service/pod/node/namespace nodes + edges
and emit a deploy change, and for get_logs/get_events telemetry tools.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-kubernetes")

_PODS = [
    {
        "name": "payment-svc-7f9c",
        "namespace": "prod",
        "node": "node-1",
        "service": "payment-svc",
        "status": "Running",
        "restarts": 0,
    },
    {
        "name": "payment-svc-8a2b",
        "namespace": "prod",
        "node": "node-2",
        "service": "payment-svc",
        "status": "CrashLoopBackOff",
        "restarts": 7,
    },
]

_NODES = [
    {"name": "node-1", "status": "Ready"},
    {"name": "node-2", "status": "Ready"},
]

_DEPLOYMENTS = [
    {
        "name": "payment-svc",
        "namespace": "prod",
        "image": "registry.local/payment-svc:v42",
        "revision": 7,
        "replicas": 2,
        "updated_at": "2026-06-13T09:15:00Z",
    }
]


@mcp.tool()
def list_pods(namespace: str = "prod") -> list[dict]:
    return [p for p in _PODS if p["namespace"] == namespace]


@mcp.tool()
def list_nodes() -> list[dict]:
    return _NODES


@mcp.tool()
def list_deployments(namespace: str = "prod") -> list[dict]:
    return [d for d in _DEPLOYMENTS if d["namespace"] == namespace]


@mcp.tool()
def get_events(namespace: str = "prod") -> list[dict]:
    return [
        {
            "pod": "payment-svc-8a2b",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "count": 7,
        }
    ]


@mcp.tool()
def get_logs(pod: str) -> str:
    if pod == "payment-svc-8a2b":
        return (
            "Traceback (most recent call last):\n"
            "  psycopg.OperationalError: connection pool exhausted\n"
            "password=should-be-redacted\n"
        )
    return "ok\n"


if __name__ == "__main__":
    mcp.run()
