"""Reconciliation engine — cluster's chunks → scored chunks + findings (M6.3).

Doctrine #5: the LLM *proposes*, the deterministic engine *disposes*. A
`ContradictionDetector` (LLM-backed in production, a fake in tests) proposes which
chunk pairs agree or contradict; everything that follows is pure Python:

  1. staleness — if one chunk is materially newer AND of equal/higher source rank,
     the older is superseded (auto), and a `stale` finding is recorded;
  2. confidence — each surviving chunk is scored by the deterministic formula
     (M6.2) from source rank, freshness, corroboration and contradiction counts;
  3. gaps — a process with only behaviour (undocumented) or only documents (never
     practiced) yields a `gap` finding;
  4. resolution — a genuine (contemporaneous) contradiction is resolved by
     precedence + the human-declared disposition (M6.2):
       descriptive  → behaviour wins → `drift` (propose updating the document),
       prescriptive → document is law → `violation` (behaviour diverged),
       undeclared   → `contradiction` (resolve nothing, request a disposition).

Nothing here calls an LLM; the only model touch is the injected detector.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from .confidence import score_confidence
from .config import get_settings
from .dispositions import get_disposition
from .findings import emit_finding, list_findings
from .gateway import ModelGateway
from .knowledge import (
    SOURCE_RANK,
    KnowledgeChunkRow,
    freshness_days,
    get_chunks,
    set_reconciliation,
    supersede_chunk,
)
from .reconciliations import record_reconciliation

logger = logging.getLogger("opsforge.reconcile")

# Placeholder root for a contradictor whose lineage can't be established. It is
# named (not silently dropped) so an indeterminate contradiction still lowers
# confidence — the safe direction — and stays visible in the explainable breakdown.
_INDETERMINATE_ROOT = "(indeterminate-lineage)"

Relation = Literal["agrees", "contradicts"]


def _gap_missing(kinds: set[str]) -> str | None:
    """Which side of the behaviour/document pairing a process is missing, or None.
    A process known only by behaviour is undocumented; only by document, never
    practiced; only by research, has no authoritative source."""
    if "behaviour" in kinds and "document" not in kinds:
        return "no_documentation"
    if "document" in kinds and "behaviour" not in kinds:
        return "not_practiced"
    if "behaviour" not in kinds and "document" not in kinds:
        return "no_authoritative_source"
    return None


@dataclass(frozen=True)
class ClaimRelation:
    """A proposed relationship between two chunks' claims about the same process."""

    chunk_a: UUID
    chunk_b: UUID
    relation: Relation


@runtime_checkable
class ContradictionDetector(Protocol):
    async def analyze(self, chunks: list[KnowledgeChunkRow]) -> list[ClaimRelation]: ...


@dataclass
class ReconciliationResult:
    reconciliation_id: UUID
    scored: int
    superseded: int
    finding_ids: list[UUID] = field(default_factory=list)
    findings_by_kind: dict[str, int] = field(default_factory=dict)
    # Which detector actually ran. 'lexical_fallback' means the LLM detector failed
    # and the lexical floor stood in for this run (M7.4 — degraded, not silent).
    detector: str = "unknown"


def _detector_mode(detector: object) -> str:
    """The detector mode to record, for production observability. A detector may
    expose effective_mode() (LLM reports llm/lexical_fallback after running)."""
    fn = getattr(detector, "effective_mode", None)
    return fn() if callable(fn) else "unknown"


def _key(a: UUID, b: UUID) -> tuple[str, str]:
    return tuple(sorted((str(a), str(b))))  # type: ignore[return-value]


async def reconcile_process(
    org_id: object,
    process_key: str,
    *,
    detector: ContradictionDetector,
    as_of=None,
) -> ReconciliationResult:
    """Reconcile one process's chunks: score them and emit findings. Idempotent —
    re-running rescores the current chunk set (an UPDATE, not a duplicate) and does
    not re-open a finding that is already open for the same condition. `as_of`
    (test hook) fixes the clock for freshness; defaults to now."""
    recon_id = uuid.uuid4()
    active = await get_chunks(org_id, process_key)
    if not active:
        return ReconciliationResult(reconciliation_id=recon_id, scored=0, superseded=0)

    by_id = {c.id: c for c in active}
    relations = await detector.analyze(active)
    detector_mode = _detector_mode(detector)  # after analyze: LLM knows if it fell back

    # The lexical floor (keyless OR an LLM fallback) proposes 'agrees' on mere token
    # overlap, not semantics — and a behaviour/document pair always has distinct
    # roots, so M7.2 would count such an agree as a 'legitimate' corroboration LIFT.
    # A degraded/floor run must never MANUFACTURE confidence, so we keep only its
    # contradictions (which can lower confidence — the fail-safe direction) and drop
    # its agreements. The keyed LLM path is untouched (its agreement IS semantic).
    fallback = detector_mode in ("lexical", "lexical_fallback")
    if fallback:
        relations = [r for r in relations if r.relation == "contradicts"]

    staleness_days = get_settings().reconcile_staleness_days
    superseded_ids: set[UUID] = set()
    stale_pairs: list[tuple[KnowledgeChunkRow, KnowledgeChunkRow, int]] = []
    real_contradictions: list[tuple[UUID, UUID]] = []
    corroboration: list[tuple[UUID, UUID]] = []

    seen: set[tuple[str, str]] = set()
    for rel in relations:
        if rel.chunk_a not in by_id or rel.chunk_b not in by_id:
            continue
        if rel.chunk_a == rel.chunk_b:
            continue  # a chunk cannot relate to itself; reject malformed detector output
        k = _key(rel.chunk_a, rel.chunk_b)
        if k in seen:
            continue
        seen.add(k)
        a, b = by_id[rel.chunk_a], by_id[rel.chunk_b]
        if rel.relation == "agrees":
            corroboration.append((a.id, b.id))
            continue
        # contradicts. Auto-supersession is only "a newer version of the SAME kind
        # of source replaces the older one" — same source_kind (≡ same rank). A
        # cross-kind conflict (e.g. fresh behaviour vs an old document) is a real
        # reconciliation decision and MUST go to disposition resolution, never be
        # silently superseded (doctrine: nothing silently resolved).
        older, newer = sorted((a, b), key=lambda c: c.observed_at)
        gap = (newer.observed_at - older.observed_at).days
        if gap >= staleness_days and newer.source_kind == older.source_kind:
            stale_pairs.append((older, newer, gap))
            superseded_ids.add(older.id)
        else:
            real_contradictions.append((a.id, b.id))

    # Apply supersessions (the older chunk stays for audit, just flagged).
    for older, newer, _gap in stale_pairs:
        await supersede_chunk(org_id, old_id=older.id, new_id=newer.id)

    survivors = [c for c in active if c.id not in superseded_ids]

    # M7.2 — confidence is lifted only by PROVENANCE-DISJOINT agreement. Collect
    # each chunk's agreeing/contradicting partners, then count DISTINCT provenance
    # roots, NOT raw chunks: duplication of one source (a document split into many
    # chunks, a page restated) shares a root and counts once. A partner whose root
    # is indeterminate, or equal to the chunk's own root, contributes nothing —
    # biasing to the SAFE error (under-counting only makes the gate fire more).
    corr_partners: dict[UUID, set[UUID]] = {c.id: set() for c in survivors}
    contra_partners: dict[UUID, set[UUID]] = {c.id: set() for c in survivors}
    for x, y in corroboration:
        if x in superseded_ids or y in superseded_ids:
            continue
        corr_partners[x].add(y)
        corr_partners[y].add(x)
    for x, y in real_contradictions:
        if x in superseded_ids or y in superseded_ids:
            continue
        contra_partners[x].add(y)
        contra_partners[y].add(x)

    # M7.6: a ticket-sourced behaviour chunk's provenance_root IS its connector-VERIFIED
    # identity (a real directory id), or None if the identity could not be verified —
    # decided at INGEST. So no corpus-breadth attestation is needed (that M7.5 heuristic
    # was forgeable via attacker-controlled process_keys): an unverified origin is already
    # indeterminate at the root, contributing no corroboration and no pattern support.
    # Documents keep their source_ref root. root_of is just the stored provenance_root.
    root_of: dict[UUID, str | None] = {c.id: c.provenance_root for c in survivors}

    def _corroborating_roots(center: UUID, partners: set[UUID]) -> list[str]:
        # Corroboration LIFTS confidence, so the fail-safe is to UNDER-count. A
        # chunk whose OWN lineage is indeterminate cannot be lifted at all — we
        # can't even prove a partner isn't secretly its own source — and an
        # indeterminate or same-source partner contributes nothing.
        own = root_of.get(center)
        if not own:
            return []
        roots: set[str] = set()
        for p in partners:
            r = root_of.get(p)
            if not r or r == own:  # indeterminate, or the same source as `center`
                continue
            roots.add(r)
        return sorted(roots)

    def _contradicting_roots(center: UUID, partners: set[UUID]) -> list[str]:
        # Contradiction LOWERS confidence, so the fail-safe is INVERTED — over-count
        # rather than drop. Distinct determinate sources still collapse (one source
        # split into many can't dominate the count), but unlike corroboration a
        # same-source or indeterminate-lineage contradictor is NOT dropped: dropping
        # it would leave confidence higher, the unsafe direction. Each
        # indeterminate-lineage contradictor counts as its own bucket.
        roots: set[str] = set()
        indeterminate = 0
        for p in partners:
            r = root_of.get(p)
            if not r:
                indeterminate += 1
            else:
                roots.add(r)
        return sorted(roots) + [_INDETERMINATE_ROOT] * indeterminate

    # Score and persist each surviving chunk.
    confidence_of: dict[UUID, float] = {}
    # M7.5: distinct provenance-disjoint ATTESTED ORIGINS backing each ticket-sourced
    # behavioural claim = its own (attested) origin + the distinct OTHER attested
    # origins that agree with it. Reuses the M7.2 corroboration counting (corr_roots
    # already excludes same/indeterminate/unattested origins), so volume from one
    # origin — or from minted sockpuppet origins — collapses to a tiny support.
    origin_support: dict[UUID, int] = {}
    pattern_min = get_settings().behaviour_pattern_min_origins
    for c in survivors:
        corr_roots = _corroborating_roots(c.id, corr_partners[c.id])
        contra_roots = _contradicting_roots(c.id, contra_partners[c.id])
        eff_rank = c.source_rank
        if c.source_kind == "behaviour" and c.origin:
            # A pattern is distinct VERIFIED-IDENTITY BEHAVIOUR origins agreeing — NOT
            # arbitrary corroborators. A document/research agreer is corroboration for
            # CONFIDENCE (corr_roots) but is not an independent behavioural origin, so it
            # must not count toward the pattern (else one ticket borrows a doc as its
            # "second origin"). An unverified origin (root None) does not count at all.
            # root_of is the verified identity (or None); count distinct verified ids among
            # agreeing BEHAVIOUR partners, plus the chunk's own (if verified).
            own_id = root_of.get(c.id)
            if not own_id:
                # The CENTER's own identity is unverified → it earns ZERO pattern support,
                # no matter how many verified partners agree with it. Otherwise an
                # identity-less claim would launder itself to behaviour-rank by BORROWING
                # the verified identities of the legitimate teams it echoes — relocating,
                # not closing, the forgery. An unverified origin never counts (own or
                # partner); it is demoted to research rank below.
                origin_support[c.id] = 0
            else:
                beh_ids = {
                    rp
                    for p in corr_partners[c.id]
                    if (pc := by_id.get(p)) is not None
                    and pc.source_kind == "behaviour"
                    and (rp := root_of.get(p))
                    and rp != own_id
                }
                origin_support[c.id] = len(beh_ids) + 1
            # Gate TRUST, not just the finding: an UNPROVEN ticket-sourced behaviour
            # (below the pattern threshold) is weak evidence, not behaviour-rank — score
            # it at research rank so a lone/fabricated fresh ticket cannot clear the gate.
            if origin_support[c.id] < pattern_min:
                eff_rank = SOURCE_RANK["research"]
        score = score_confidence(
            source_rank=eff_rank,
            freshness_days=freshness_days(c.observed_at, as_of),
            corroborated_by=len(corr_roots),
            contradicted_by=len(contra_roots),
        ).confidence
        confidence_of[c.id] = score
        await set_reconciliation(
            org_id,
            c.id,
            confidence=score,
            corroborated_by=len(corr_roots),
            contradicted_by=len(contra_roots),
            corroborating_roots=corr_roots,
            contradicting_roots=contra_roots,
            reconciliation_id=recon_id,
            # A degraded LLM→lexical run may under-detect a contradiction; it must not
            # raise a chunk's stored confidence above what a prior (truthful) run earned.
            cap_existing=(detector_mode == "lexical_fallback"),
        )

    result = ReconciliationResult(
        reconciliation_id=recon_id, scored=len(survivors), superseded=len(superseded_ids)
    )

    # Idempotent emission: a still-open finding for the same (kind, evidence set)
    # already covers this condition, so a re-run does not pile up duplicates.
    # Findings stay append-only (doctrine #7) — we just don't re-open one.
    existing = await list_findings(org_id, process_key=process_key, state="open")
    emitted: set[tuple[str, frozenset[str]]] = {
        (f.kind, frozenset(f.evidence_refs)) for f in existing
    }

    async def _emit(kind, detail, evidence, conf) -> None:
        key = (kind, frozenset(str(e) for e in evidence))
        if key in emitted:
            return
        emitted.add(key)
        fid = await emit_finding(
            org_id=org_id,
            kind=kind,
            process_key=process_key,
            detail=detail,
            evidence_refs=evidence,
            confidence=conf,
            reconciliation_id=recon_id,
        )
        result.finding_ids.append(fid)
        result.findings_by_kind[kind] = result.findings_by_kind.get(kind, 0) + 1

    # stale findings
    for older, newer, gap in stale_pairs:
        await _emit(
            "stale",
            {
                "superseded_chunk": str(older.id),
                "superseding_chunk": str(newer.id),
                "age_gap_days": gap,
            },
            [older.id, newer.id],
            confidence_of.get(newer.id),
        )

    # gap finding — a process missing behaviour or documentation
    if survivors:
        missing = _gap_missing({c.source_kind for c in survivors})
        if missing:
            await _emit(
                "gap",
                {"missing": missing},
                [c.id for c in survivors],
                max((confidence_of[c.id] for c in survivors), default=None),
            )

    # contradiction resolution by precedence + disposition (pattern_min from above)
    disposition = await get_disposition(org_id, process_key)
    for x, y in real_contradictions:
        if x in superseded_ids or y in superseded_ids:
            continue
        a, b = by_id[x], by_id[y]
        higher, lower = sorted((a, b), key=lambda c: c.source_rank, reverse=True)
        evidence = [a.id, b.id]
        # drift/violation are specifically the behaviour-vs-document resolution
        # (spec §5.5). Any other kind pairing (e.g. document-vs-research) has no
        # doc/behaviour remediation and must be surfaced for a human, not
        # auto-mapped with inverted labels.
        is_behaviour_doc = {a.source_kind, b.source_kind} == {"behaviour", "document"}
        beh = a if a.source_kind == "behaviour" else b
        # M7.5: a TICKET-SOURCED behaviour claim may override a document ONLY if it is
        # a genuine pattern — its agreeing cluster spans >= pattern_min DISTINCT
        # origins. A single event, or volume from one origin, is demoted: the document
        # is NOT overridden; surface "seen once — not yet a pattern". Human-asserted
        # (origin-less) behaviour is not gated. Bias to safe: uncertain → demote.
        unproven_behaviour = (
            is_behaviour_doc
            and beh.origin is not None
            and origin_support.get(beh.id, 1) < pattern_min
        )
        if is_behaviour_doc and unproven_behaviour:
            await _emit(
                "contradiction",
                {
                    "disposition": disposition,
                    "behaviour_chunk": str(beh.id),
                    "document_chunk": str(lower.id),
                    "action": "behaviour_below_pattern_threshold",
                    "note": "seen — not yet a provenance-disjoint pattern",
                    "distinct_origins": origin_support.get(beh.id, 1),
                    "required_origins": pattern_min,
                },
                evidence,
                confidence_of.get(beh.id),
            )
        elif is_behaviour_doc and disposition == "descriptive":
            await _emit(
                "drift",
                {
                    "disposition": "descriptive",
                    "winner_chunk": str(higher.id),
                    "behaviour_chunk": str(higher.id),
                    "document_chunk": str(lower.id),
                    "action": "update_document_to_match_behaviour",
                },
                evidence,
                confidence_of.get(higher.id),
            )
        elif is_behaviour_doc and disposition == "prescriptive":
            await _emit(
                "violation",
                {
                    "disposition": "prescriptive",
                    "standard_chunk": str(lower.id),
                    "violating_chunk": str(higher.id),
                    "action": "behaviour_violates_standard",
                },
                evidence,
                confidence_of.get(lower.id),
            )
        else:
            await _emit(
                "contradiction",
                {
                    "disposition": disposition,
                    "chunk_a": str(a.id),
                    "chunk_b": str(b.id),
                    "action": "declare_disposition" if disposition == "undeclared"
                    else "needs_human_review",
                },
                evidence,
                max(confidence_of.get(a.id, 0.0), confidence_of.get(b.id, 0.0)),
            )

    # Record the run so a degraded (LLM→lexical fallback) reconciliation is visible.
    # Best-effort: the scoring already landed on the chunks, so an audit-write failure
    # must not fail an otherwise-complete reconciliation (it would just retry it).
    result.detector = detector_mode
    try:
        await record_reconciliation(
            org_id,
            recon_id,
            process_key,
            detector=detector_mode,
            scored=result.scored,
            superseded=result.superseded,
            findings=len(result.finding_ids),
        )
    except Exception:  # noqa: BLE001 — observability write, never fail the run on it
        logger.warning("failed to record reconciliation %s", recon_id, exc_info=True)
    return result


# A convenience adapter so callers can pass a plain async function as a detector.
DetectorFn = Callable[[list[KnowledgeChunkRow]], Awaitable[list[ClaimRelation]]]


@dataclass
class FunctionDetector:
    fn: DetectorFn

    async def analyze(self, chunks: list[KnowledgeChunkRow]) -> list[ClaimRelation]:
        return await self.fn(chunks)

    def effective_mode(self) -> str:
        return "scripted"


_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)

_DETECT_PROMPT = (
    "You compare statements about the same operational process and identify which "
    "PAIRS clearly agree or contradict each other.\n\nStatements:\n{listing}\n\n"
    "Return ONLY a JSON array of objects "
    '{{"a": <index>, "b": <index>, "relation": "agrees"|"contradicts"}} for pairs '
    "that clearly agree or contradict. Omit unrelated pairs. If none, return [].\n"
    'Example: [{{"a": 0, "b": 1, "relation": "contradicts"}}]'
)


@dataclass
class LLMDetector:
    """Production detector: the LLM PROPOSES which chunk pairs agree/contradict;
    the deterministic engine still disposes (scoring, staleness, resolution, and
    the malformed-output guards). Any failure — no key, a model error, unparseable
    output — falls back to the lexical stand-in, so reconciliation never breaks."""

    gateway: ModelGateway
    model: str
    fallback: ContradictionDetector = field(default_factory=lambda: LexicalDetector())
    _mode: str = field(default="llm", init=False)

    async def analyze(self, chunks: list[KnowledgeChunkRow]) -> list[ClaimRelation]:
        self._mode = "llm"
        if len(chunks) < 2:
            return []
        listing = "\n".join(
            f"[{i}] ({c.source_kind}) {c.content}" for i, c in enumerate(chunks)
        )
        try:
            result = await self.gateway.chat(
                [{"role": "user", "content": _DETECT_PROMPT.format(listing=listing)}],
                None,
                self.model,
            )
            match = _JSON_ARRAY.search(result.text or "")
            if not match:
                # Unusable output (prose, truncation, no array at all) is malformed
                # → fall back to the floor, don't silently propose zero relations.
                raise ValueError("no JSON array in detector output")
            data = json.loads(match.group(0))
            rels: list[ClaimRelation] = []
            n = len(chunks)
            for item in data:
                a, b = int(item["a"]), int(item["b"])
                rel = item["relation"]
                if rel in ("agrees", "contradicts") and 0 <= a < n and 0 <= b < n:
                    rels.append(ClaimRelation(chunks[a].id, chunks[b].id, rel))
            return rels
        except Exception:  # noqa: BLE001 — any LLM/parse failure is contained
            logger.warning("LLM detector failed; falling back to lexical", exc_info=True)
            self._mode = "lexical_fallback"  # M7.4: degraded run, recorded not silent
            return await self.fallback.analyze(chunks)

    def effective_mode(self) -> str:
        return self._mode


async def configured_detector(org_id: object | None = None) -> ContradictionDetector:
    """The PRODUCTION reconcile detector (M7.4, per-workspace in M7.6 Job A): the LLM
    detector built from the WORKSPACE's ACTIVE vault-credentialed provider when one is
    configured; else the keyless lexical floor (NOT a shared global key, so LLM isolation
    holds per workspace). A `.env` provider key is only a LOCAL-DEV fallback (gated on
    settings.dev_llm_fallback), never the deployed path. The LLM only PROPOSES relations;
    the deterministic engine still disposes, and any LLM failure falls back to lexical."""
    if org_id is not None:
        from .llm_providers import active_config

        cfg = await active_config(org_id)
        if cfg is not None:
            # An active binding whose credential did not RESOLVE (api_key and api_base both
            # None — e.g. the vault blob failed to decrypt after a Fernet-key rotation, or a
            # corrupted credential) must fail CLOSED to the lexical floor. We must NOT build
            # a key-less litellm gateway: litellm would then silently read the ambient
            # OPENAI_API_KEY/ANTHROPIC_API_KEY, routing this workspace's data through a
            # shared global key it was never bound to — exactly the isolation breach the
            # vault exists to prevent. (Self-hosted bindings carry an api_base, so they
            # resolve and are unaffected.)
            if cfg.api_key is None and cfg.api_base is None:
                return LexicalDetector()
            from .gateway import LiteLLMGateway

            return LLMDetector(
                LiteLLMGateway(api_key=cfg.api_key, api_base=cfg.api_base), cfg.model
            )
    # Local-dev-only fallback to an environment key — never the production path.
    import os

    keyed = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if get_settings().dev_llm_fallback and keyed:
        from .gateway import LiteLLMGateway

        return LLMDetector(LiteLLMGateway(), get_settings().model)
    return LexicalDetector()


@dataclass
class LexicalDetector:
    """Keyless dev/demo stand-in for the LLM detector. Compares each behaviour
    chunk against each document chunk in the cluster by token overlap (Jaccard):
    dissimilar → propose 'contradicts', similar → 'agrees'. It deliberately
    OVER-surfaces divergence for human review — a finding never auto-resolves, so
    a false contradiction is safe (a reviewer dismisses it) while a missed one is
    not. Real semantic contradiction detection is the LLM gateway path (M7)."""

    overlap_threshold: float = 0.5

    @staticmethod
    def _tokens(s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", s.lower()))

    async def analyze(self, chunks: list[KnowledgeChunkRow]) -> list[ClaimRelation]:
        behaviours = [c for c in chunks if c.source_kind == "behaviour"]
        documents = [c for c in chunks if c.source_kind == "document"]
        rels: list[ClaimRelation] = []
        for b in behaviours:
            for d in documents:
                tb, td = self._tokens(b.content), self._tokens(d.content)
                if not tb or not td:
                    continue
                jaccard = len(tb & td) / len(tb | td)
                rel = "agrees" if jaccard >= self.overlap_threshold else "contradicts"
                rels.append(ClaimRelation(b.id, d.id, rel))
        return rels

    def effective_mode(self) -> str:
        return "lexical"
