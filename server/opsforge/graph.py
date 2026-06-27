"""Operational graph: per-connector mappers, idempotent upserts, and queries.

A mapper turns one connector's MCP tool output into a GraphDelta (nodes, edges,
changes) keyed by stable `natural_key`s. `sync_connector` opens a session, runs
the kind's mapper, and applies the delta with upserts so repeated syncs converge
instead of duplicating. Service identity is connector-neutral
(`service://<name>`) so K8s topology and observability telemetry enrich the same
node.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .connectors import ConnectorSession, open_connector
from .db import scope_to_org, session_factory

# --------------------------------------------------------------------------- #
# natural_key builders (stable identities, shared across connectors)
# --------------------------------------------------------------------------- #


def svc_key(name: str) -> str:
    return f"service://{name}"


def pod_key(namespace: str, name: str) -> str:
    return f"k8s://{namespace}/pod/{name}"


def node_key(name: str) -> str:
    return f"k8s://node/{name}"


def ns_key(name: str) -> str:
    return f"k8s://ns/{name}"


def vm_key(region: str, instance_id: str) -> str:
    return f"aws://{region}/instance/{instance_id}"


# --------------------------------------------------------------------------- #
# GraphDelta = what a mapper produces
# --------------------------------------------------------------------------- #
# nodes:   [{"kind", "natural_key", "props"}]
# edges:   [{"src", "dst", "kind", "props"}]   (src/dst are natural_keys)
# changes: [{"kind", "ref", "summary", "diff", "target_keys", "occurred_at"}]
GraphDelta = dict[str, list[dict[str, Any]]]


def _empty_delta() -> GraphDelta:
    return {"nodes": [], "edges": [], "changes": []}


# --------------------------------------------------------------------------- #
# Mappers
# --------------------------------------------------------------------------- #
async def map_kubernetes(cs: ConnectorSession) -> GraphDelta:
    delta = _empty_delta()
    namespaces: set[str] = set()
    services: set[tuple[str, str]] = set()

    nodes = await _safe_call(cs, "kubernetes.list_nodes", {})
    for n in nodes or []:
        delta["nodes"].append(
            {"kind": "node", "natural_key": node_key(n["name"]), "props": n}
        )

    pods = await _safe_call(cs, "kubernetes.list_pods", {"namespace": "prod"})
    for p in pods or []:
        ns, svc = p.get("namespace", "default"), p.get("service")
        namespaces.add(ns)
        pk = pod_key(ns, p["name"])
        delta["nodes"].append({"kind": "pod", "natural_key": pk, "props": p})
        if p.get("node"):
            delta["edges"].append(
                {"src": pk, "dst": node_key(p["node"]), "kind": "runs_on", "props": {}}
            )
        if svc:
            services.add((svc, ns))
            delta["edges"].append(
                {"src": svc_key(svc), "dst": pk, "kind": "routes_to", "props": {}}
            )

    for ns in namespaces:
        delta["nodes"].append(
            {"kind": "namespace", "natural_key": ns_key(ns), "props": {"name": ns}}
        )
    for svc, ns in services:
        delta["nodes"].append(
            {"kind": "service", "natural_key": svc_key(svc), "props": {"name": svc}}
        )
        delta["edges"].append(
            {"src": svc_key(svc), "dst": ns_key(ns), "kind": "member_of", "props": {}}
        )

    deployments = await _safe_call(cs, "kubernetes.list_deployments", {"namespace": "prod"})
    for d in deployments or []:
        svc = d["name"]
        delta["changes"].append(
            {
                "kind": "deploy",
                "ref": f"{svc}@rev{d.get('revision')}",
                "summary": f"Deployed {svc} image {d.get('image')} (revision {d.get('revision')})",
                "diff": json.dumps(d, sort_keys=True),
                "target_keys": [svc_key(svc)],
                "occurred_at": d.get("updated_at"),
            }
        )
    return delta


async def map_aws(cs: ConnectorSession) -> GraphDelta:
    """Cloud mapper: EC2 instances -> vm nodes. (CloudTrail-lite diffing is a
    later enhancement; the change webhook is the better deploy source.)"""
    delta = _empty_delta()
    instances = await _safe_call(cs, "aws.describe_instances", {})
    for i in instances or []:
        region = i.get("region", "us-east-1")
        delta["nodes"].append(
            {
                "kind": "vm",
                "natural_key": vm_key(region, i["instance_id"]),
                "props": i,
            }
        )
        if i.get("service"):
            delta["edges"].append(
                {
                    "src": svc_key(i["service"]),
                    "dst": vm_key(region, i["instance_id"]),
                    "kind": "runs_on",
                    "props": {},
                }
            )
    return delta


async def map_observability(cs: ConnectorSession) -> GraphDelta:
    """Observability mapper: scrape targets -> service nodes (enriching the same
    `service://` identities the K8s mapper created)."""
    delta = _empty_delta()
    targets = await _safe_call(cs, f"{cs.kind}.list_targets", {})
    for t in targets or []:
        svc = t.get("service") or t.get("job")
        if not svc:
            continue
        delta["nodes"].append(
            {
                "kind": "service",
                "natural_key": svc_key(svc),
                "props": {"name": svc, "monitored": True, "health": t.get("health")},
            }
        )
    return delta


async def map_servicenow(cs: ConnectorSession) -> GraphDelta:
    """ITSM/CMDB mapper: the CIs of currently-open incidents → `service://` nodes
    + `depends_on` edges (the SAME natural_keys infra connectors use, so CMDB and
    live topology fuse onto one node)."""
    delta = _empty_delta()
    incidents = await _safe_call(cs, f"{cs.kind}.search_incidents", {}) or []
    seen: set[str] = set()
    for inc in incidents:
        ci = inc.get("cmdb_ci") if isinstance(inc, dict) else None
        if not ci or ci in seen:
            continue
        seen.add(ci)
        cis = await _safe_call(cs, f"{cs.kind}.get_related_cis", {"ci": ci}) or []
        for c in cis:
            delta["nodes"].append(
                {
                    "kind": "service",
                    "natural_key": c["natural_key"],
                    "props": {"name": c.get("name"), "source": "cmdb"},
                }
            )
        if cis:
            root = cis[0]["natural_key"]
            for c in cis[1:]:
                delta["edges"].append(
                    {"src": root, "dst": c["natural_key"], "kind": "depends_on", "props": {}}
                )
    return delta


_MAPPERS = {
    "kubernetes": map_kubernetes,
    "aws": map_aws,
    "datadog": map_observability,
    "servicenow": map_servicenow,
    "jira": map_servicenow,
    "pagerduty": map_servicenow,
    "custom": map_observability,
}


async def _safe_call(cs: ConnectorSession, tool_fqn: str, params: dict) -> Any:
    """Call a tool but tolerate it being absent from the allowlist (mappers ask
    for the union of useful tools; a connector may expose only some)."""
    _, _, tool = tool_fqn.partition(".")
    if tool not in cs.allowlist:
        return None
    return await cs.call(tool_fqn, params)


def mapper_for(kind: str):
    # Observability connectors are registered by their concrete kind; anything
    # exposing list_targets falls back to the observability mapper.
    return _MAPPERS.get(kind, map_observability)


# --------------------------------------------------------------------------- #
# Apply a delta (idempotent upserts)
# --------------------------------------------------------------------------- #
_UPSERT_NODE = text(
    """
    INSERT INTO graph_nodes (org_id, kind, natural_key, props, source_connector_id, last_seen_at)
    VALUES (:org_id, :kind, :natural_key, CAST(:props AS jsonb), :connector_id, now())
    ON CONFLICT (natural_key) DO UPDATE
        SET props = graph_nodes.props || EXCLUDED.props,
            kind = EXCLUDED.kind,
            source_connector_id = EXCLUDED.source_connector_id,
            last_seen_at = now()
    RETURNING id
    """
)
_UPSERT_EDGE = text(
    """
    INSERT INTO graph_edges (org_id, src_id, dst_id, kind, props, last_seen_at)
    VALUES (:org_id, :src_id, :dst_id, :kind, CAST(:props AS jsonb), now())
    ON CONFLICT (src_id, dst_id, kind) DO UPDATE
        SET props = graph_edges.props || EXCLUDED.props, last_seen_at = now()
    """
)
_UPSERT_CHANGE = text(
    """
    INSERT INTO changes (org_id, kind, ref, summary, diff, target_keys,
                         occurred_at, source_connector_id)
    VALUES (:org_id, :kind, :ref, :summary, :diff, :target_keys,
            COALESCE(CAST(:occurred_at AS timestamptz), now()), :connector_id)
    ON CONFLICT (source_connector_id, kind, ref) DO UPDATE
        SET summary = EXCLUDED.summary, diff = EXCLUDED.diff,
            target_keys = EXCLUDED.target_keys
    """
)


async def apply_delta(
    session: AsyncSession,
    org_id: str,
    connector_id: UUID | None,
    delta: GraphDelta,
) -> dict[str, int]:
    key_to_id: dict[str, UUID] = {}
    for n in delta["nodes"]:
        node_id = (
            await session.execute(
                _UPSERT_NODE,
                {
                    "org_id": org_id,
                    "kind": n["kind"],
                    "natural_key": n["natural_key"],
                    "props": json.dumps(n.get("props") or {}),
                    "connector_id": connector_id,
                },
            )
        ).scalar_one()
        key_to_id[n["natural_key"]] = node_id

    edges_applied = 0
    for e in delta["edges"]:
        src_id = key_to_id.get(e["src"]) or await _resolve_id(session, e["src"])
        dst_id = key_to_id.get(e["dst"]) or await _resolve_id(session, e["dst"])
        if not src_id or not dst_id:
            continue  # endpoint not in graph (yet); skip silently
        await session.execute(
            _UPSERT_EDGE,
            {
                "org_id": org_id,
                "src_id": src_id,
                "dst_id": dst_id,
                "kind": e["kind"],
                "props": json.dumps(e.get("props") or {}),
            },
        )
        edges_applied += 1

    for c in delta["changes"]:
        await session.execute(
            _UPSERT_CHANGE,
            {
                "org_id": org_id,
                "kind": c["kind"],
                "ref": c.get("ref"),
                "summary": c.get("summary"),
                "diff": c.get("diff"),
                "target_keys": c.get("target_keys"),
                "occurred_at": c.get("occurred_at"),
                "connector_id": connector_id,
            },
        )

    return {
        "nodes": len(delta["nodes"]),
        "edges": edges_applied,
        "changes": len(delta["changes"]),
    }


async def _resolve_id(session: AsyncSession, natural_key: str) -> UUID | None:
    return (
        await session.execute(
            text("SELECT id FROM graph_nodes WHERE natural_key = :k"),
            {"k": natural_key},
        )
    ).scalar_one_or_none()


async def sync_connector(connector: dict[str, Any]) -> dict[str, int]:
    """Open a session, run the kind's mapper, and apply the delta."""
    mapper = mapper_for(connector["kind"])
    async with open_connector(connector) as cs:
        delta = await mapper(cs)
    org_id = str(connector["org_id"])
    async with session_factory().begin() as session:
        await scope_to_org(session, org_id)
        return await apply_delta(
            session, org_id, connector.get("id"), delta
        )


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
_NEIGHBORHOOD_IDS = text(
    """
    WITH RECURSIVE seed AS (
        SELECT id FROM graph_nodes WHERE natural_key = :key
    ),
    reachable(id, depth) AS (
        SELECT id, 0 FROM seed
        UNION
        SELECT CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END, r.depth + 1
        FROM reachable r
        JOIN graph_edges e ON (e.src_id = r.id OR e.dst_id = r.id)
        WHERE r.depth < :hops
    )
    SELECT DISTINCT id FROM reachable
    """
)


async def neighborhood(natural_key: str, hops: int = 2, org_id: str = "") -> dict[str, Any]:
    """Nodes reachable within `hops` of a node, plus the edges among them."""
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        ids = [
            r[0]
            for r in (
                await s.execute(_NEIGHBORHOOD_IDS, {"key": natural_key, "hops": hops})
            ).all()
        ]
        if not ids:
            return {"root": natural_key, "nodes": [], "edges": []}

        nodes = [
            dict(r._mapping)
            for r in (
                await s.execute(
                    text(
                        "SELECT id, kind, natural_key, props FROM graph_nodes "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            ).all()
        ]
        edges = [
            dict(r._mapping)
            for r in (
                await s.execute(
                    text(
                        "SELECT src_id, dst_id, kind FROM graph_edges "
                        "WHERE src_id = ANY(:ids) AND dst_id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            ).all()
        ]
    return {"root": natural_key, "nodes": nodes, "edges": edges}


def render_neighborhood(graph: dict[str, Any]) -> str:
    """Compact text rendering of a neighborhood for LLM context (M2)."""
    by_id = {n["id"]: n for n in graph["nodes"]}
    lines = [f"# Operational graph around {graph['root']}"]
    for n in graph["nodes"]:
        props = {k: v for k, v in (n.get("props") or {}).items() if k != "name"}
        lines.append(f"- {n['kind']} {n['natural_key']} {props}".rstrip())
    for e in graph["edges"]:
        src = by_id.get(e["src_id"], {}).get("natural_key", e["src_id"])
        dst = by_id.get(e["dst_id"], {}).get("natural_key", e["dst_id"])
        lines.append(f"  {src} --{e['kind']}--> {dst}")
    return "\n".join(lines)
