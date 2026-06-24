"""Validated-process generation (M6.4).

From the reconciled, scored chunks of one process, draft the canonical
current-version process (steps + decisions + gates). The LLM *drafts* the steps
(injected `ProcessDrafter`, faked in tests); the deterministic code attaches each
step's provenance — the source chunk ids, their kinds, the oldest source's
freshness, and a confidence that is the MINIMUM of the step's grounding (a step is
only as trustworthy as its weakest source). Steps below the configured threshold
are flagged low_confidence so the signoff screen can say "this one is a guess —
look hard."

Versioned and append-only: regenerating mints a new version and supersedes the
prior one. Signoff reuses the kernel's audit trail.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .config import get_settings
from .db import record_audit, scope_to_org, session_factory
from .gateway import ModelGateway
from .knowledge import KnowledgeChunkRow, freshness_days, get_chunks

logger = logging.getLogger("opsforge.processes")


@dataclass(frozen=True)
class StepDraft:
    """An LLM-proposed step grounded in specific chunks. `kind` is step|decision|gate."""

    text: str
    source_chunks: list[UUID] = field(default_factory=list)
    kind: str = "step"


@runtime_checkable
class ProcessDrafter(Protocol):
    async def draft(self, chunks: list[KnowledgeChunkRow]) -> list[StepDraft]: ...


class ValidatedProcessRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    process_key: str
    version: int
    status: str
    steps: list[dict[str, Any]]
    reconciliation_id: UUID | None = None
    min_confidence: float | None = None
    uncovered_chunks: list[Any] = []
    superseded_by: UUID | None = None
    signed_off_by: UUID | None = None


_INSERT = text(
    """
    INSERT INTO validated_processes
        (org_id, process_key, version, steps, reconciliation_id, min_confidence,
         uncovered_chunks)
    VALUES (:org, :pk, :version, CAST(:steps AS jsonb), :recon, :min_conf,
            CAST(:uncovered AS jsonb))
    RETURNING id
    """
)

_COLS = (
    "id, org_id, process_key, version, status, steps, reconciliation_id, "
    "min_confidence, uncovered_chunks, superseded_by, signed_off_by"
)


def _build_step(index: int, draft: StepDraft, by_id: dict[UUID, KnowledgeChunkRow],
                threshold: float, as_of: Any) -> dict[str, Any] | None:
    # Guardrail #1: reject a step with no valid chunk provenance — ghost ids are
    # filtered out, and a step left citing nothing (or only ghosts) does NOT enter
    # the process. The hard wall against the drafter inventing process from nothing.
    source = [by_id[c] for c in draft.source_chunks if c in by_id]
    if not source:
        return None
    # Guardrail #2/#3: confidence and provenance are recomputed deterministically
    # from the cited chunks — NEVER taken from the drafter. A step is only as
    # trustworthy as its weakest grounding (unscored source → 0). Fluent prose
    # cannot raise confidence; the drafter cannot relabel a document as behaviour.
    confs = [float(c.confidence) if c.confidence is not None else 0.0 for c in source]
    confidence = min(confs)
    fresh = max(freshness_days(c.observed_at, as_of) for c in source)
    kind = draft.kind if draft.kind in ("step", "decision", "gate") else "step"
    return {
        "index": index,
        "kind": kind,
        "text": draft.text,
        "source_chunks": [str(c.id) for c in source],
        "source_kinds": sorted({c.source_kind for c in source}),
        "freshness_days": fresh,
        "confidence": confidence,
        "low_confidence": confidence < threshold,
    }


def _build_steps(drafts, by_id, threshold, as_of) -> list[dict[str, Any]]:
    """Build steps from a drafter's output, dropping any with no valid provenance.
    Step index is the surviving position (rejected steps leave no gap)."""
    steps: list[dict[str, Any]] = []
    for draft in drafts:
        step = _build_step(len(steps), draft, by_id, threshold, as_of)
        if step is not None:
            steps.append(step)
    return steps


async def generate_process(
    org_id: Any, process_key: str, *, drafter: ProcessDrafter, as_of=None
) -> ValidatedProcessRow | None:
    """Draft (or re-draft) the current-version process. Returns None if the
    process has no active knowledge to draft from. Supersedes the prior version.

    The drafter proposes structure + wording; this function disposes — it rejects
    steps without provenance, recomputes every confidence, and (guardrail #5) falls
    back to mechanical one-step-per-chunk generation if the drafter yields nothing
    usable, so drafting never breaks the loop."""
    chunks = await get_chunks(org_id, process_key)
    if not chunks:
        return None
    by_id = {c.id: c for c in chunks}
    threshold = get_settings().validated_process_low_confidence_threshold

    steps = _build_steps(await drafter.draft(chunks), by_id, threshold, as_of)
    if not steps and not isinstance(drafter, OutlineDrafter):
        # the drafter produced nothing usable → mechanical fallback
        logger.warning("drafter yielded no usable steps; falling back to mechanical")
        steps = _build_steps(await OutlineDrafter().draft(chunks), by_id, threshold, as_of)

    # Guardrail #4: flag surviving chunks the drafter left unrepresented.
    covered = {cid for s in steps for cid in s["source_chunks"]}
    uncovered = [str(c.id) for c in chunks if str(c.id) not in covered]
    if uncovered:
        logger.warning("process %s: %d chunk(s) uncovered by any step", process_key, len(uncovered))

    min_conf = min((s["confidence"] for s in steps), default=None)
    # carry a reconciliation id for traceability (chunks share the latest run's id)
    recon = next((c.reconciliation_id for c in chunks if c.reconciliation_id), None)

    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        cur = (
            await s.execute(
                text(
                    "SELECT id FROM validated_processes WHERE org_id=:o AND process_key=:pk "
                    "AND status != 'superseded' ORDER BY version DESC LIMIT 1"
                ),
                {"o": str(org_id), "pk": process_key},
            )
        ).first()
        maxv = (
            await s.execute(
                text(
                    "SELECT COALESCE(MAX(version),0) FROM validated_processes "
                    "WHERE org_id=:o AND process_key=:pk"
                ),
                {"o": str(org_id), "pk": process_key},
            )
        ).scalar_one()
        new_id = (
            await s.execute(
                _INSERT,
                {
                    "org": str(org_id),
                    "pk": process_key,
                    "version": maxv + 1,
                    "steps": json.dumps(steps),
                    "recon": str(recon) if recon else None,
                    "min_conf": min_conf,
                    "uncovered": json.dumps(uncovered),
                },
            )
        ).scalar_one()
        if cur is not None:
            await s.execute(
                text(
                    "UPDATE validated_processes SET status='superseded', superseded_by=:new "
                    "WHERE id=:old"
                ),
                {"new": new_id, "old": cur.id},
            )
    return await _load(org_id, new_id)


async def _load(org_id: Any, process_id: UUID) -> ValidatedProcessRow | None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(f"SELECT {_COLS} FROM validated_processes WHERE id=:id AND org_id=:o"),
                {"id": str(process_id), "o": str(org_id)},
            )
        ).first()
    return ValidatedProcessRow.model_validate(dict(row._mapping)) if row else None


async def get_current_process(org_id: Any, process_key: str) -> ValidatedProcessRow | None:
    """The current (non-superseded) version for a process, or None."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    f"SELECT {_COLS} FROM validated_processes WHERE org_id=:o AND process_key=:pk "
                    "AND status != 'superseded' ORDER BY version DESC LIMIT 1"
                ),
                {"o": str(org_id), "pk": process_key},
            )
        ).first()
    return ValidatedProcessRow.model_validate(dict(row._mapping)) if row else None


async def list_processes(org_id: Any) -> list[ValidatedProcessRow]:
    """The current (non-superseded) validated process for every key in the workspace,
    newest-touched first — for the process-review index. RLS-scoped."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    f"SELECT {_COLS} FROM validated_processes "
                    "WHERE org_id = :o AND status != 'superseded' "
                    "ORDER BY process_key"
                ),
                {"o": str(org_id)},
            )
        ).all()
    return [ValidatedProcessRow.model_validate(dict(r._mapping)) for r in rows]


async def list_process_versions(org_id: Any, process_key: str) -> list[ValidatedProcessRow]:
    """Every version of one process, newest first — for the version-history + diff view.
    Each row carries its full steps + per-step provenance so the diff is grounded.
    RLS-scoped."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    f"SELECT {_COLS} FROM validated_processes "
                    "WHERE org_id = :o AND process_key = :pk ORDER BY version DESC"
                ),
                {"o": str(org_id), "pk": process_key},
            )
        ).all()
    return [ValidatedProcessRow.model_validate(dict(r._mapping)) for r in rows]


async def sign_off_process(
    org_id: Any, process_id: UUID, *, signed_by: UUID | str | None
) -> None:
    """Human signoff of a drafted process. Reuses the kernel audit trail."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        res = await s.execute(
            text(
                "UPDATE validated_processes SET status='signed_off', signed_off_by=:by, "
                "signed_off_at=now() WHERE id=:id AND org_id=:o AND status='draft'"
            ),
            {
                "by": str(signed_by) if signed_by else None,
                "id": str(process_id),
                "o": str(org_id),
            },
        )
        if res.rowcount != 1:
            raise ValueError("process not found or not in 'draft' state")
    await record_audit(
        org_id,
        actor=f"user:{signed_by}" if signed_by else "system",
        event="process.signed_off",
        subject_ref=str(process_id),
        detail={},
    )


# Convenience adapter so a plain async function can be passed as a drafter.
@dataclass
class FunctionDrafter:
    fn: Any

    async def draft(self, chunks: list[KnowledgeChunkRow]) -> list[StepDraft]:
        return await self.fn(chunks)


@dataclass
class OutlineDrafter:
    """Keyless mechanical fallback: one step per surviving chunk, grounded in it.
    The deterministic layer still recomputes each step's confidence from
    provenance, so this cannot inflate trust. Used when no LLM provider is
    configured, or whenever the LLM drafter yields nothing usable."""

    async def draft(self, chunks: list[KnowledgeChunkRow]) -> list[StepDraft]:
        kind_to_label = {"behaviour": "step", "document": "step", "research": "decision"}
        return [
            StepDraft(text=c.content, source_chunks=[c.id],
                      kind=kind_to_label.get(c.source_kind, "step"))
            for c in chunks
        ]


_DRAFT_PROMPT = (
    "You are drafting a clear, ordered operational process from the knowledge "
    "below. MERGE statements that describe the same step into one step. Put steps "
    "in real operational order. Where the knowledge implies a branch (e.g. 'if the "
    "health check fails, stop'), make that step a decision.\n\n"
    "Knowledge (cite by index):\n{listing}\n\n"
    "Return ONLY a JSON array of steps, each: "
    '{{"text": "<clear instruction>", "kind": "step"|"decision"|"gate", '
    '"source_chunks": [<the indices this step is built from>]}}. '
    "Every step MUST cite at least one index it was built from. Do NOT invent "
    "steps with no basis in the knowledge."
)


@dataclass
class LLMDrafter:
    """Production drafter: the LLM PROPOSES the structure + wording (merging,
    ordering, surfacing decisions); the deterministic engine still disposes — it
    rejects steps without provenance, recomputes confidence, inherits provenance,
    and checks coverage. The LLM has ZERO authority over any trust-bearing number.
    Cites chunks by index into the prompt listing; invalid indices are dropped.
    Any failure (no key, model error, unparseable output) falls back to the
    mechanical drafter, so drafting never breaks."""

    gateway: ModelGateway
    model: str
    fallback: ProcessDrafter = field(default_factory=lambda: OutlineDrafter())

    async def draft(self, chunks: list[KnowledgeChunkRow]) -> list[StepDraft]:
        if not chunks:
            return []
        listing = "\n".join(f"[{i}] ({c.source_kind}) {c.content}" for i, c in enumerate(chunks))
        try:
            result = await self.gateway.chat(
                [{"role": "user", "content": _DRAFT_PROMPT.format(listing=listing)}],
                None,
                self.model,
            )
            match = re.search(r"\[.*\]", result.text or "", re.DOTALL)
            data = json.loads(match.group(0)) if match else []
            drafts: list[StepDraft] = []
            for item in data:
                idxs = [
                    int(i) for i in item.get("source_chunks", [])
                    if isinstance(i, int) or (isinstance(i, str) and i.lstrip("-").isdigit())
                ]
                src = [chunks[i].id for i in idxs if 0 <= i < len(chunks)]
                # a step the LLM left citing nothing valid is dropped downstream
                drafts.append(
                    StepDraft(text=str(item.get("text", "")), source_chunks=src,
                              kind=item.get("kind", "step"))
                )
            return drafts
        except Exception:  # noqa: BLE001 — any LLM/parse failure is contained
            logger.warning("LLM drafter failed; falling back to mechanical", exc_info=True)
            return await self.fallback.draft(chunks)


def configured_drafter() -> ProcessDrafter:
    """The LLM drafter when a provider key is configured, else the mechanical
    one-step-per-chunk fallback — so generation works with or without a provider."""
    import os

    if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        from .gateway import LiteLLMGateway

        return LLMDrafter(LiteLLMGateway(), get_settings().model)
    return OutlineDrafter()
