"""Connector-sourced knowledge ingestion (Phase B) — the first REAL knowledge source.

Pulls real documents through a vault-credentialed knowledge connector (Confluence first) and
stores each as a `document` chunk with REAL provenance: the page URL as `source_ref` and the
page's real last-modified as `observed_at` (NOT ingest time — this is the staleness signal the
reconcile engine uses). Read-only: it only ever calls the connector's `list_documents` tool.

Honest partials: returns (chunk_ids, complete) — `complete` is False when the connector
reported a partial pull (rate-limit cap, mid-pull failure), so a partial ingest is never
reported as a silent success.

Process grouping (design note / real-system limitation): a Confluence page has no
`process_key`. The fake fixture carries one; the real connector does not, so REAL ingestion
groups by `default_process_key` (a per-connector setting) — a later milestone can derive it
from a page label. Pages with no resolvable process_key are skipped, not mis-grouped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .connectors import open_connector
from .knowledge import PendingChunk, ProvenanceEnvelope, store_chunks
from .security import redact

# Same contract as ingest.Embedder, defined locally so this module stays in its own band.
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


def _parse_observed(value: Any) -> datetime | None:
    """Parse a document's REAL last-modified. Returns None when there is no real timestamp
    (empty/unparseable) — we must NOT fabricate now() for observed_at, which would present a
    stale page as fresh and invert the staleness signal. now() is only ever valid for
    ingested_at."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


async def ingest_knowledge_from_connector(
    connector: dict[str, Any],
    *,
    org_id: Any,
    embedder: Embedder,
    default_process_key: str | None = None,
    as_of: datetime | None = None,
) -> tuple[list[UUID], bool]:
    """Pull documents through the connector and store them as `document` chunks with real
    provenance. Returns (stored chunk ids, complete)."""
    kind = connector["kind"]
    async with open_connector(connector) as cs:
        raw = await cs.call(f"{kind}.list_documents", {})
    if isinstance(raw, dict):
        docs = raw.get("documents", []) or []
        complete = bool(raw.get("complete", True))
    else:
        docs = raw or []
        complete = True

    ingested_at = as_of or datetime.now(UTC)
    pending: list[PendingChunk] = []
    for d in docs:
        text = (d.get("text") or "").strip()
        url = d.get("url")
        process_key = d.get("process_key") or default_process_key
        if not text or not url or not process_key:
            # empty/malformed/ungroupable doc → nothing to ingest, skipped honestly.
            continue
        observed = _parse_observed(d.get("updated_at"))
        if observed is None:
            # real content but NO real last-modified — we refuse to fabricate now() (false-
            # fresh). Drop the page and mark the pull PARTIAL so the gap is reported, never
            # ingested as a clean, falsely-fresh success.
            complete = False
            continue
        env = ProvenanceEnvelope(
            source_kind="document",
            source_ref=str(url),          # the REAL page URL
            observed_at=observed,         # the REAL last-modified — never now()
            ingested_at=ingested_at,
        )
        # redact before the text crosses the embedder / lands in content (doctrine).
        pending.append(
            PendingChunk(content=redact(text), envelope=env, process_key=str(process_key))
        )
    if not pending:
        return [], complete
    vectors = await embedder([p.content for p in pending])
    pending = [replace(p, embedding=vectors[i]) for i, p in enumerate(pending)]
    ids = await store_chunks(org_id, pending)
    return ids, complete
