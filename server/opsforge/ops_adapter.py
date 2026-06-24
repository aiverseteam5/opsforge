"""Ops adapter — the connector boundary that yields CANONICAL ops objects.

Doctrine: "the connector maps native→canonical." This thin layer = a connector
tool call + `ops_model.from_native(...)` using the connector's `field_mapping`.
Consumers (agent context assembly, the NL entity resolver) reason over the
canonical `Incident`; the raw tool stays available to the LLM separately.
"""

from __future__ import annotations

from typing import Any

from .connectors import open_connector
from .ops_model import Incident, from_native

# v1 convention: ops connectors expose `get_incident(number=<ref>)` and
# `search_incidents(service=..., open_only=...)`. A connector that names them
# differently declares it in its tool layer; broaden here as kinds are added.


async def read_incident(connector: dict[str, Any], ref: str) -> Incident:
    """Fetch one incident and translate it to the canonical model."""
    kind = connector["kind"]
    async with open_connector(connector) as cs:
        raw = await cs.call(f"{kind}.get_incident", {"number": ref})
    if not isinstance(raw, dict):
        raw = {}
    return from_native("incident", raw, connector.get("field_mapping"))  # type: ignore[return-value]


async def search_incident_ref(
    connector: dict[str, Any], service: str, open_only: bool = True
) -> str | None:
    """Resolve a service mention to an open incident ref (entity lookup for NL)."""
    kind = connector["kind"]
    async with open_connector(connector) as cs:
        results = await cs.call(
            f"{kind}.search_incidents", {"service": service, "open_only": open_only}
        )
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            mapping = connector.get("field_mapping") or {}
            ref_field = (mapping.get("incident.ref") or {}).get("field", "number")
            return str(first.get(ref_field) or first.get("number") or first.get("ref") or "")
    return None
