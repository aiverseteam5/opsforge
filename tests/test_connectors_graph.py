"""M1: connector lifecycle, MCP tool calls + redaction, and graph build/query.

Uses the in-repo fake MCP servers (no live cluster). Requires Compose db+migrate.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import api_client
from fake_mcp import server_command
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.connectors import ConnectorError, load_connector, open_connector
from opsforge.db import session_factory
from opsforge.graph import neighborhood
from opsforge.worker import handle_graph_sync

ORG = get_settings().org_id  # the org auth_headers seeds its token for

K8S_TOOLS = ["list_pods", "list_nodes", "list_deployments", "get_events", "get_logs"]
PROM_TOOLS = ["list_targets", "query_metrics"]


async def _create_connector(headers, name, kind, tools, server) -> str:
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/connectors",
            headers=headers,
            json={
                "name": name,
                "kind": kind,
                "transport": "stdio",
                "endpoint": server_command(server),
                "tool_allowlist": tools,
                # the fake servers ignore creds, but these kinds now require one (A2 fail-closed)
                "credentials": {"token": "fake"},
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "healthy"  # health-checked on create
    assert "credentials" not in body  # never serialized
    return body["id"]


async def test_connector_create_lists_and_tests(auth_headers):
    cid = await _create_connector(
        auth_headers, "k8s-prod", "kubernetes", K8S_TOOLS, "k8s"
    )

    async with api_client() as client:
        listed = (await client.get("/api/v1/connectors", headers=auth_headers)).json()
        assert any(c["id"] == cid for c in listed)
        assert all("credentials" not in c for c in listed)

        test = await client.post(
            f"/api/v1/connectors/{cid}/test", headers=auth_headers
        )
    assert test.status_code == 200
    assert test.json()["status"] == "healthy"
    assert set(test.json()["tools"]) == {f"kubernetes.{t}" for t in K8S_TOOLS}


async def test_tool_allowlist_is_enforced(auth_headers):
    # Only allow list_pods; get_logs must be invisible/blocked even though the
    # server exposes it.
    cid = await _create_connector(
        auth_headers, "k8s-limited", "kubernetes", ["list_pods"], "k8s"
    )
    connector = await load_connector(uuid.UUID(cid), ORG)
    async with open_connector(connector) as cs:
        tools = await cs.list_tools()
        assert tools == ["kubernetes.list_pods"]
        with pytest.raises(ConnectorError):
            await cs.call("kubernetes.get_logs", {"pod": "x"})


async def test_tool_result_redacted_in_run_events(auth_headers):
    cid = await _create_connector(
        auth_headers, "k8s-redact", "kubernetes", K8S_TOOLS, "k8s"
    )
    connector = await load_connector(uuid.UUID(cid), ORG)
    run_id = uuid.uuid4()
    async with open_connector(connector) as cs:
        logs = await cs.call(
            "kubernetes.get_logs", {"pod": "payment-svc-8a2b"}, run_id=run_id
        )
    # The returned value is the raw log (caller's responsibility), but what we
    # persisted to run_events must be redacted.
    assert "password=should-be-redacted" in logs
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT kind, payload FROM run_events WHERE run_id = :r ORDER BY seq"
                ),
                {"r": run_id},
            )
        ).all()
    kinds = [r.kind for r in rows]
    assert kinds == ["tool_call", "tool_result"]
    result_text = str(rows[1].payload)
    assert "should-be-redacted" not in result_text
    assert "REDACTED" in result_text


async def test_graph_sync_builds_topology_and_change(auth_headers):
    cid = await _create_connector(
        auth_headers, "k8s-graph", "kubernetes", K8S_TOOLS, "k8s"
    )
    # Run the actual worker handler (load connector -> mapper -> upsert).
    await handle_graph_sync({"connector_id": cid, "org_id": ORG})

    # Neighborhood of the service returns pods, nodes, namespace, and edges.
    nb = await neighborhood("service://payment-svc", hops=2)
    kinds = {n["kind"] for n in nb["nodes"]}
    assert {"service", "pod", "node", "namespace"} <= kinds
    pod_keys = {n["natural_key"] for n in nb["nodes"] if n["kind"] == "pod"}
    assert "k8s://prod/pod/payment-svc-7f9c" in pod_keys
    edge_kinds = {e["kind"] for e in nb["edges"]}
    assert {"routes_to", "runs_on"} <= edge_kinds

    # The deployment emitted a deploy change targeting the service.
    async with session_factory().begin() as s:
        change = (
            await s.execute(
                text(
                    "SELECT kind, ref, target_keys FROM changes "
                    "WHERE source_connector_id = :c AND kind = 'deploy'"
                ),
                {"c": cid},
            )
        ).first()
    assert change is not None
    assert change.ref == "payment-svc@rev7"
    assert "service://payment-svc" in change.target_keys


async def test_graph_sync_is_idempotent(auth_headers):
    cid = await _create_connector(
        auth_headers, "k8s-idem", "kubernetes", K8S_TOOLS, "k8s"
    )
    await handle_graph_sync({"connector_id": cid, "org_id": ORG})
    nb1 = await neighborhood("service://payment-svc", hops=2)
    await handle_graph_sync({"connector_id": cid, "org_id": ORG})
    nb2 = await neighborhood("service://payment-svc", hops=2)
    # Re-sync must not duplicate edges.
    assert len(nb2["edges"]) == len(nb1["edges"])


async def test_observability_connector_enriches_service(auth_headers):
    k8s = await _create_connector(
        auth_headers, "k8s-obs", "kubernetes", K8S_TOOLS, "k8s"
    )
    await handle_graph_sync({"connector_id": k8s, "org_id": ORG})
    prom = await _create_connector(
        auth_headers, "prom", "datadog", PROM_TOOLS, "prom"
    )
    await handle_graph_sync({"connector_id": prom, "org_id": ORG})

    # The same service:// node now carries observability props.
    async with session_factory().begin() as s:
        props = (
            await s.execute(
                text(
                    "SELECT props FROM graph_nodes "
                    "WHERE natural_key = 'service://payment-svc'"
                )
            )
        ).scalar_one()
    assert props.get("monitored") is True


async def test_neighborhood_endpoint(auth_headers):
    cid = await _create_connector(
        auth_headers, "k8s-api", "kubernetes", K8S_TOOLS, "k8s"
    )
    await handle_graph_sync({"connector_id": cid, "org_id": ORG})
    async with api_client() as client:
        resp = await client.get(
            "/api/v1/graph/neighborhood",
            params={"key": "service://payment-svc", "hops": 2},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["root"] == "service://payment-svc"
    assert len(data["nodes"]) >= 4
