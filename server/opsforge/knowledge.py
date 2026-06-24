"""Knowledge & Truth Plane — the provenance envelope contract + chunk store.

Doctrine #2: no fact without provenance. `ProvenanceEnvelope` is the central new
contract; a chunk cannot be stored without a valid one, exactly as the kernel
refuses an action without a `policy_trace`. The envelope is assigned at ingest
and consumed at context-assembly (M6.5).

Derived, not free-typed (doctrine #3): `source_rank` encodes the precedence
ladder behaviour(3) > document(2) > research(1) as data, so the deterministic
reconciliation/confidence formula (M6.2) reads it directly. `freshness_days` is
recomputed on read from `observed_at` (when the knowledge was *true*, never
ingest time) — it is deliberately not a stored column, which would be stale the
moment time passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .db import scope_to_org, session_factory
from .models import KnowledgeSourceKind, ProcessDisposition

# The precedence ladder as data (doctrine #3).
SOURCE_RANK: dict[str, int] = {"behaviour": 3, "document": 2, "research": 1}


def canonical_origin(raw: str | None) -> str | None:
    """Canonicalize a ticket origin so trivial variants of one actor collapse to a
    single root (M7.5 hardening). origin is attacker-controlled free text from the
    external system; without this, 'sre-a' / 'Sre-A' / 'sre-a ' / a tab / whitespace
    would each mint a separate 'disjoint' origin. Collapse all whitespace, strip, and
    casefold; a value that is empty/whitespace-only canonicalizes to None (indeterminate
    → fails safe to NOT-disjoint, never a distinct root)."""
    if raw is None:
        return None
    s = " ".join(str(raw).split()).strip().casefold()
    return s or None


def provenance_root_for(
    source_ref: str | None, origin: str | None = None, origin_identity: str | None = None
) -> str | None:
    """The provenance root a chunk's corroboration-disjointness is judged on (M7.2,
    origin-aware in M7.5, IDENTITY-BACKED in M7.6).

    Confidence is lifted only by agreement between provenance-DISJOINT sources; two
    chunks are disjoint only if their roots differ. For a TICKET-SOURCED chunk (origin
    set) the root is the connector-VERIFIED external identity (`origin_identity` — the
    real directory group/actor id), NOT the attacker-influenceable free-text origin. So
    two tickets are distinct origins ONLY if their verified identities differ; an origin
    whose identity could not be verified (unknown/ambiguous/unavailable) has root None →
    INDETERMINATE → it contributes no corroboration and cannot reach behaviour-rank.
    This closes the M7.5 residual at the root: forgery now requires a real directory
    identity the source system controls, not a string the attacker types. For non-ticket
    chunks the root remains the source_ref (one document = one root).

    An indeterminate root (None/empty) fails SAFE: treated as NOT disjoint, no
    corroboration. Under-counting only makes the gate fire more; over-counting bypasses it."""
    if origin:  # ticket-sourced → the VERIFIED identity, or indeterminate
        return (origin_identity or "").strip() or None
    return source_ref or None


class ProvenanceEnvelope(BaseModel):
    """Required provenance for every stored chunk. Constructing one with a field
    missing or a bad source_kind raises ValidationError — that *is* the ingest
    gate."""

    model_config = ConfigDict(frozen=True)

    source_kind: KnowledgeSourceKind
    source_ref: str = Field(min_length=1)
    observed_at: datetime  # when the knowledge was true/written, NOT ingest time
    ingested_at: datetime
    # M7.5: the behavioural origin (group/actor/job) for a ticket-sourced chunk —
    # the human-readable display string. None for documents and human-asserted behaviour.
    origin: str | None = None
    # M7.6: the connector-VERIFIED external identity of that origin (a real directory
    # sys_id the attacker can't mint). None = unverified → the root is indeterminate and
    # the behaviour is demoted. This — not the free-text origin — is the provenance root.
    origin_identity: str | None = None

    @property
    def source_rank(self) -> int:
        return SOURCE_RANK[self.source_kind]


class KnowledgeChunkRow(BaseModel):
    """Read view of a stored chunk. `freshness_days` is computed, never stored."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    process_key: str | None
    content: str
    source_kind: KnowledgeSourceKind
    source_ref: str
    source_rank: int
    observed_at: datetime
    ingested_at: datetime
    confidence: float | None = None
    corroborated_by: int | None = None
    contradicted_by: int | None = None
    provenance_root: str | None = None
    origin: str | None = None
    corroborating_roots: list[str] = Field(default_factory=list)
    contradicting_roots: list[str] = Field(default_factory=list)
    superseded_by: UUID | None = None
    process_disposition: ProcessDisposition = "undeclared"
    reconciliation_id: UUID | None = None

    def freshness_days(self, as_of: datetime | None = None) -> int:
        return freshness_days(self.observed_at, as_of)


def freshness_days(observed_at: datetime, as_of: datetime | None = None) -> int:
    """now - observed_at, in whole days, floored at 0. Recomputed on read.
    Naive datetimes (either argument) are treated as UTC."""
    as_of = as_of or datetime.now(UTC)
    as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    obs = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
    return max(0, (as_of - obs).days)


def _vector_literal(embedding: list[float] | None) -> str | None:
    """pgvector text form '[a,b,c]' for CAST(:emb AS vector); None → NULL."""
    if embedding is None:
        return None
    return "[" + ",".join(format(float(x), ".7g") for x in embedding) + "]"


_INSERT_CHUNK = text(
    """
    INSERT INTO knowledge_chunks
        (org_id, process_key, content, embedding,
         source_kind, source_ref, source_rank, observed_at, ingested_at,
         provenance_root, origin)
    VALUES
        (:org, :process_key, :content, CAST(:embedding AS vector),
         :source_kind, :source_ref, :source_rank, :observed_at, :ingested_at,
         :provenance_root, :origin)
    RETURNING id
    """
)


@dataclass(frozen=True)
class PendingChunk:
    """One chunk ready to store. The envelope is mandatory and pre-validated, so
    a chunk without provenance is unrepresentable (doctrine #2)."""

    content: str
    envelope: ProvenanceEnvelope
    embedding: list[float] | None = None
    process_key: str | None = None


def _chunk_params(org_id: Any, chunk: PendingChunk) -> dict[str, Any]:
    env = chunk.envelope
    return {
        "org": str(org_id),
        "process_key": chunk.process_key,
        "content": chunk.content,
        "embedding": _vector_literal(chunk.embedding),
        "source_kind": env.source_kind,
        "source_ref": env.source_ref,
        "source_rank": env.source_rank,
        "observed_at": env.observed_at,
        "ingested_at": env.ingested_at,
        "provenance_root": provenance_root_for(env.source_ref, env.origin, env.origin_identity),
        "origin": env.origin,
    }


async def store_chunk(
    *,
    org_id: Any,
    content: str,
    envelope: ProvenanceEnvelope,
    embedding: list[float] | None = None,
    process_key: str | None = None,
) -> UUID:
    """Append a single chunk in its own transaction. RLS-scoped."""
    pending = PendingChunk(
        content=content, envelope=envelope, embedding=embedding, process_key=process_key
    )
    (cid,) = await store_chunks(org_id, [pending])
    return cid


async def store_chunks(org_id: Any, chunks: list[PendingChunk]) -> list[UUID]:
    """Append many chunks for one source in a SINGLE transaction — all land or
    none do. The document is the provenance unit, so a mid-file failure must not
    leave partial provenance behind. RLS-scoped."""
    if not chunks:
        return []
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        out: list[UUID] = []
        for chunk in chunks:
            row = (await s.execute(_INSERT_CHUNK, _chunk_params(org_id, chunk))).one()
            out.append(row.id)
    return out


_COLS = (
    "id, org_id, process_key, content, source_kind, source_ref, source_rank, "
    "observed_at, ingested_at, confidence, corroborated_by, contradicted_by, "
    "provenance_root, origin, corroborating_roots, contradicting_roots, "
    "superseded_by, process_disposition, reconciliation_id"
)


async def count_chunks(org_id: Any, process_key: str | None = None) -> int:
    clause = "" if process_key is None else " AND process_key = :pk"
    params: dict[str, Any] = {"org": str(org_id)}
    if process_key is not None:
        params["pk"] = process_key
    sql = text(f"SELECT count(*) FROM knowledge_chunks WHERE org_id = :org{clause}")
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        return (await s.execute(sql, params)).scalar_one()


async def get_chunks(
    org_id: Any,
    process_key: str | None = None,
    *,
    limit: int = 200,
    include_superseded: bool = False,
) -> list[KnowledgeChunkRow]:
    """Fetch chunks (newest first) with provenance. Superseded (stale) chunks are
    excluded by default — they stay in the table for audit but are not active
    knowledge. Used by reconciliation (M6.3) and context-assembly (M6.5).
    RLS-scoped; the explicit org predicate keeps it correct under the dev
    superuser role too."""
    clause = "" if process_key is None else " AND process_key = :pk"
    if not include_superseded:
        clause += " AND superseded_by IS NULL"
    params: dict[str, Any] = {"org": str(org_id), "limit": limit}
    if process_key is not None:
        params["pk"] = process_key
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    f"SELECT {_COLS} FROM knowledge_chunks "
                    f"WHERE org_id = :org{clause} ORDER BY created_at DESC LIMIT :limit"
                ),
                params,
            )
        ).all()
    return [KnowledgeChunkRow.model_validate(dict(r._mapping)) for r in rows]


_SET_RECON = text(
    """
    UPDATE knowledge_chunks
    SET confidence = :confidence,
        corroborated_by = :corroborated_by,
        contradicted_by = :contradicted_by,
        corroborating_roots = CAST(:corroborating_roots AS jsonb),
        contradicting_roots = CAST(:contradicting_roots AS jsonb),
        reconciliation_id = :reconciliation_id
    WHERE id = :id AND org_id = :org
    """
)

# Non-regressive variant: confidence can only stay or DROP, never rise. Used by a
# degraded (lexical_fallback) reconciliation so that under-detecting a contradiction
# the LLM would have caught can never overwrite a truthful low score with a higher
# one (M7.4 §2.4 — a degraded run must not manufacture confidence).
_SET_RECON_CAP = text(
    """
    UPDATE knowledge_chunks
    SET confidence = LEAST(:confidence, COALESCE(confidence, :confidence)),
        corroborated_by = :corroborated_by,
        contradicted_by = :contradicted_by,
        corroborating_roots = CAST(:corroborating_roots AS jsonb),
        contradicting_roots = CAST(:contradicting_roots AS jsonb),
        reconciliation_id = :reconciliation_id
    WHERE id = :id AND org_id = :org
    """
)

_SUPERSEDE = text(
    "UPDATE knowledge_chunks SET superseded_by = :new WHERE id = :old AND org_id = :org"
)


async def set_reconciliation(
    org_id: Any,
    chunk_id: UUID,
    *,
    confidence: float,
    corroborated_by: int,
    contradicted_by: int,
    reconciliation_id: UUID,
    corroborating_roots: list[str] | None = None,
    contradicting_roots: list[str] | None = None,
    cap_existing: bool = False,
) -> None:
    """Write a reconciliation run's scored output onto a chunk. RLS-scoped.

    `corroborated_by`/`contradicted_by` are DISTINCT-provenance-root counts (M7.2);
    `*_roots` record which distinct roots they were, so the score stays explainable.
    `cap_existing` (a degraded lexical_fallback run) makes confidence non-regressive:
    it may stay or drop, never rise above the chunk's prior score.
    """
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            _SET_RECON_CAP if cap_existing else _SET_RECON,
            {
                "id": str(chunk_id),
                "org": str(org_id),
                "confidence": confidence,
                "corroborated_by": corroborated_by,
                "contradicted_by": contradicted_by,
                "corroborating_roots": json.dumps(corroborating_roots or []),
                "contradicting_roots": json.dumps(contradicting_roots or []),
                "reconciliation_id": str(reconciliation_id),
            },
        )


async def supersede_chunk(org_id: Any, *, old_id: UUID, new_id: UUID) -> None:
    """Mark `old_id` as superseded by `new_id` (staleness). Append-only spirit:
    the old chunk stays for audit, just flagged replaced. RLS-scoped."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            _SUPERSEDE, {"old": str(old_id), "new": str(new_id), "org": str(org_id)}
        )
