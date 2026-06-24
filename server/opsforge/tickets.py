"""Ticket-source ingestion (M7.5) — the first REAL behaviour signal.

Until now behaviour was seeded. This pulls resolved ticket / run history (ServiceNow
first) through the vault-credentialed connector path and stores each ticket as a
behaviour OBSERVATION carrying its ORIGIN (the group/actor/automated job that
resolved it). Doctrine: behaviour is a PATTERN, not an event — one ticket is one
observation, never authoritative behaviour on its own. The reconcile engine's
pattern threshold (>= N provenance-disjoint origins) decides which observations form
an authoritative pattern; this module's job is just to ingest them honestly with
origin metadata, so the M7.2 distinct-origin machinery can do the rest.

Origin metadata is the vehicle that closes the M7.2 residual: it lets
`provenance_root_for()` distinguish N tickets from ONE origin (one root) from N
tickets from separate origins (N roots).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .connectors import open_connector
from .knowledge import PendingChunk, ProvenanceEnvelope, canonical_origin, store_chunks
from .security import redact

# Same contract as ingest.Embedder, defined locally so this module stays in its own
# layer band (it must not import its peer `ingest`).
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


def normalize_ticket(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One raw resolved ticket → the fields a behaviour observation needs, or None if
    it lacks the essentials (no process, no resolution text, or no origin — an
    origin-less ticket can't be judged for pattern disjointness, so we skip it rather
    than mint untrustworthy behaviour). Tolerant of native or pre-normalized shapes."""
    process_key = raw.get("process_key") or raw.get("process") or raw.get("service_ref")
    content = raw.get("resolution") or raw.get("resolution_notes") or raw.get("work_notes")
    # Origin = who/what resolved it (an assignment group / actor / automated job): the
    # human-readable DISPLAY string, canonicalized for cleanliness. A ticket with no
    # display origin at all is dropped (can't attribute it).
    origin = canonical_origin(
        raw.get("origin")
        or raw.get("assignment_group")
        or raw.get("resolved_by")
        or raw.get("closed_by")
    )
    ref = raw.get("number") or raw.get("ref") or raw.get("sys_id")
    if not (process_key and content and origin and ref):
        return None
    # M7.6: the VERIFIED external identity of that origin — the connector's directory id
    # (assignment_group_id), an attestable signal the attacker can't mint. None when the
    # connector could not resolve it (unknown/ambiguous/unavailable). This — not the
    # free-text origin — is the provenance root; an unverified ticket is stored but
    # demoted (fail safe), NOT dropped (it is still a real observation, just untrusted).
    # ONLY the connector-stamped assignment_group_id counts: we never read a self-asserted
    # identity field off the raw payload, or an attacker who edits a ServiceNow record
    # could mint a "verified" root for an origin the directory never resolved (fail OPEN).
    # A group the connector could not resolve leaves this None → demotion, the safe error.
    identity = raw.get("assignment_group_id")
    observed_at = _parse_dt(raw.get("resolved_at") or raw.get("closed_at") or raw.get("opened_at"))
    return {
        "process_key": str(process_key),
        "content": str(content),
        "origin": str(origin),
        "origin_identity": str(identity).strip() if identity else None,
        "source_ref": ref if "://" in str(ref) else f"servicenow://{ref}",
        "observed_at": observed_at,
    }


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


async def ingest_tickets(
    org_id: Any,
    tickets: list[dict[str, Any]],
    *,
    embedder: Embedder,
    as_of: datetime | None = None,
) -> list[UUID]:
    """Normalize raw tickets → behaviour chunks (one observation per ticket) with
    origin metadata, redact, embed, and store. Returns the stored chunk ids.

    Each chunk is `source_kind='behaviour'` with `origin` set, so `provenance_root_for`
    roots it by origin and the reconcile pattern threshold counts distinct origins."""
    ingested_at = as_of or datetime.now(UTC)
    pending: list[PendingChunk] = []
    for raw in tickets:
        norm = normalize_ticket(raw)
        if norm is None:
            continue
        env = ProvenanceEnvelope(
            source_kind="behaviour",
            source_ref=norm["source_ref"],
            origin=norm["origin"],
            origin_identity=norm["origin_identity"],
            observed_at=norm["observed_at"],
            ingested_at=ingested_at,
        )
        # redact before the text crosses the embedder / lands in content (doctrine).
        pending.append(
            PendingChunk(
                content=redact(norm["content"]), envelope=env, process_key=norm["process_key"]
            )
        )
    if not pending:
        return []
    vectors = await embedder([p.content for p in pending])
    pending = [replace(p, embedding=vectors[i]) for i, p in enumerate(pending)]
    return await store_chunks(org_id, pending)


async def ingest_tickets_from_connector(
    connector: dict[str, Any],
    *,
    org_id: Any,
    embedder: Embedder,
    since_days: int = 90,
    as_of: datetime | None = None,
) -> list[UUID]:
    """Pull resolved tickets through the vault-credentialed connector (the real path:
    creds decrypted at spawn, never `.env`) and ingest them. The connector exposes a
    `{kind}.list_resolved_incidents` tool, allowlisted like every other."""
    kind = connector["kind"]
    async with open_connector(connector) as cs:
        raw = await cs.call(f"{kind}.list_resolved_incidents", {"since_days": since_days})
    tickets = raw if isinstance(raw, list) else (raw or {}).get("incidents", [])
    return await ingest_tickets(org_id, tickets, embedder=embedder, as_of=as_of)
