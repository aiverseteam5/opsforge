"""MCP client lifecycle, tool discovery, and per-connector allowlists (doctrine #4).

All external systems are MCP servers. This module owns the client sessions,
intersects discovered tools with each connector's allowlist, and runs `call()`
(decrypt creds at spawn only, execute, redact result, optionally record a
tool_call/tool_result pair into run_events).

Sessions are opened per scope (a `graph_sync` mapper or an agent run holds one
open session and makes several calls), not pooled long-term — credentials are
decrypted only at spawn, and a dead subprocess can never leak a stale session.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import text

from .db import append_run_event, scope_to_org, session_factory
from .security import decrypt, redact


class ConnectorError(RuntimeError):
    """Raised when a connector cannot be reached or a tool call fails."""


def _decrypt_credentials(credentials_enc: bytes | None) -> dict[str, str]:
    if not credentials_enc:
        return {}
    raw = decrypt(credentials_enc)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ConnectorError("credentials envelope is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _tool_payload(result: types.CallToolResult) -> Any:
    """Reduce an MCP CallToolResult to plain JSON-able data."""
    if result.structuredContent is not None:
        sc = result.structuredContent
        # FastMCP (and others) wrap a non-object return under {"result": ...};
        # unwrap so callers see the list/scalar the tool actually returned.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    parts: list[Any] = []
    for block in result.content:
        text_val = getattr(block, "text", None)
        if text_val is None:
            parts.append({"type": getattr(block, "type", "unknown")})
            continue
        try:
            parts.append(json.loads(text_val))
        except (ValueError, TypeError):
            parts.append(text_val)
    if len(parts) == 1:
        return parts[0]
    return parts


class ConnectorSession:
    """A live MCP session for one connector. Exposes only allowlisted tools as
    `{kind}.{tool}` fully-qualified names."""

    def __init__(self, connector: dict[str, Any], session: ClientSession):
        self._connector = connector
        self._session = session
        self.kind: str = connector["kind"]
        self.allowlist: set[str] = set(connector.get("tool_allowlist") or [])

    async def list_tools(self) -> list[str]:
        """Discovered tools ∩ allowlist, as fully-qualified names. No wildcards."""
        result = await self._session.list_tools()
        return [
            f"{self.kind}.{t.name}"
            for t in result.tools
            if t.name in self.allowlist
        ]

    async def list_tool_defs(self) -> list[dict[str, Any]]:
        """Allowlisted tools with their fqn, description, and JSON input schema."""
        result = await self._session.list_tools()
        defs: list[dict[str, Any]] = []
        for t in result.tools:
            if t.name not in self.allowlist:
                continue
            defs.append(
                {
                    "fqn": f"{self.kind}.{t.name}",
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                }
            )
        return defs

    async def call(
        self,
        tool_fqn: str,
        params: dict[str, Any] | None = None,
        *,
        run_id: UUID | None = None,
    ) -> Any:
        """Invoke an allowlisted tool. Records a tool_call/tool_result pair into
        run_events (redacted) when run_id is given. Plaintext never logged."""
        kind, _, tool = tool_fqn.partition(".")
        if kind != self.kind:
            raise ConnectorError(
                f"tool {tool_fqn!r} does not belong to connector kind {self.kind!r}"
            )
        if tool not in self.allowlist:
            raise ConnectorError(f"tool {tool!r} is not allowlisted")

        params = params or {}
        if run_id is not None:
            await append_run_event(
                run_id,
                self._connector["org_id"],
                "tool_call",
                {"tool": tool_fqn, "params": redact(params)},
            )

        result = await self._session.call_tool(tool, params)
        payload = _tool_payload(result)
        redacted = redact(payload)

        if run_id is not None:
            await append_run_event(
                run_id,
                self._connector["org_id"],
                "tool_result",
                {"tool": tool_fqn, "is_error": bool(result.isError), "result": redacted},
            )
        if result.isError:
            raise ConnectorError(f"tool {tool_fqn!r} returned an error: {redacted}")
        return payload


@asynccontextmanager
async def open_connector(connector: dict[str, Any]) -> AsyncIterator[ConnectorSession]:
    """Open and initialize an MCP session for a connector row (as a dict).

    Credentials are decrypted here and injected into the server env (stdio) or
    request headers (http) at spawn time only.
    """
    creds = _decrypt_credentials(connector.get("credentials_enc"))
    transport = connector["transport"]
    endpoint = connector["endpoint"]

    if transport == "stdio":
        argv = shlex.split(endpoint, posix=False)
        if not argv:
            raise ConnectorError("stdio connector endpoint is empty")
        params = StdioServerParameters(command=argv[0], args=argv[1:], env=creds or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield ConnectorSession(connector, session)
    elif transport == "http":
        async with streamablehttp_client(endpoint, headers=creds or None) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield ConnectorSession(connector, session)
    else:
        raise ConnectorError(f"unknown transport {transport!r}")


async def health_check(connector: dict[str, Any]) -> dict[str, Any]:
    """Connect, list tools, and — if the connector exposes a `verify_credential` tool —
    actually EXERCISE the credential, so a reachable endpoint with a wrong/expired token is
    reported `unhealthy` (the catalog then shows `error`, never false-`connected`). This is
    the Phase-B closure of the A2/F2 boundary: reachability alone is not 'connected'. Pure
    (does not write the DB)."""
    kind = connector["kind"]
    try:
        async with open_connector(connector) as cs:
            tools = await cs.list_tools()
            if "verify_credential" in cs.allowlist:
                verdict = await cs.call(f"{kind}.verify_credential", {})
                if not (isinstance(verdict, dict) and verdict.get("authenticated")):
                    reason = (verdict or {}).get("error", "credential not valid") \
                        if isinstance(verdict, dict) else "credential not valid"
                    return {"status": "unhealthy", "error": redact(str(reason))}
        return {"status": "healthy", "tools": tools}
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        return {"status": "unhealthy", "error": redact(str(exc))}


async def discover(connector: dict[str, Any]) -> dict[str, Any]:
    """Introspect a connector's native schema via its `{kind}.describe_schema`
    tool (the GAP-1 onboarding step). Returns {} if the tool isn't exposed."""
    kind = connector["kind"]
    async with open_connector(connector) as cs:
        if "describe_schema" not in cs.allowlist:
            return {}
        result = await cs.call(f"{kind}.describe_schema", {})
    return result if isinstance(result, dict) else {"schema": result}


_UPDATE_HEALTH_SQL = text(
    "UPDATE connectors SET status = :status, last_health_at = :ts "
    "WHERE id = :id AND org_id = :org"
)


async def record_health(connector_id: UUID, status: str, org_id: Any) -> None:
    """Persist a health result. RLS-scoped (A1.5): the org GUC is set and the explicit org
    predicate is kept as defense-in-depth. Fails CLOSED — a foreign org affects no rows; a
    None/unparseable org raises (callers always pass a real org), never a cross-org no-op."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            _UPDATE_HEALTH_SQL,
            {"status": status, "ts": datetime.now(UTC), "id": connector_id,
             "org": str(org_id)},
        )


_CONNECTOR_COLS = (
    "id, org_id, name, kind, transport, endpoint, credentials_enc, "
    "tool_allowlist, field_mapping, discovered_schema, status, environment"
)


async def load_connector(connector_id: UUID, org_id: Any) -> dict[str, Any] | None:
    """Fetch a connector row as a dict (including credentials_enc for spawning), scoped to
    its workspace. RLS-scoped (A1.5): the `connectors` table now has FORCE RLS, so the org
    GUC MUST be set — `org_id` is required. Fails CLOSED: a FOREIGN org returns None (the
    policy + predicate match no rows); a None/unparseable org raises (callers always pass a
    real org). It can never return another workspace's connector. Predicate = defense-in-depth."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(f"SELECT {_CONNECTOR_COLS} FROM connectors "
                     "WHERE id = :id AND org_id = :org"),
                {"id": connector_id, "org": str(org_id)},
            )
        ).first()
    return dict(row._mapping) if row else None


async def load_connectors_by_kind(org_id: Any) -> dict[str, dict[str, Any]]:
    """One healthy connector per kind for an org (first wins). Used by the agent
    to bind manifest tools (`kind.tool`) to a live connector. RLS-scoped (A1.5)."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    f"SELECT {_CONNECTOR_COLS} FROM connectors "
                    "WHERE org_id = :org ORDER BY created_at"
                ),
                {"org": str(org_id)},
            )
        ).all()
    by_kind: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = dict(r._mapping)
        by_kind.setdefault(row["kind"], row)
    return by_kind
