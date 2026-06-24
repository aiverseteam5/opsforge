"""Fake Prometheus-compatible observability MCP server (stdio).

Exposes query_metrics + list_targets. The observability mapper turns scrape
targets into `service` nodes so telemetry lines up with the K8s topology.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-prometheus")

_TARGETS = [
    {"job": "payment-svc", "service": "payment-svc", "instance": "10.0.0.7:9090", "health": "up"},
    {"job": "ledger-svc", "service": "ledger-svc", "instance": "10.0.0.9:9090", "health": "up"},
]


@mcp.tool()
def list_targets() -> list[dict]:
    return _TARGETS


@mcp.tool()
def query_metrics(query: str) -> dict:
    # A canned spike for payment-svc 5xx rate; shape mimics Prometheus results.
    return {
        "query": query,
        "result": [
            {
                "metric": {"service": "payment-svc", "code": "500"},
                "values": [[1749805200, "0.01"], [1749808800, "0.42"]],
            }
        ],
    }


if __name__ == "__main__":
    mcp.run()
