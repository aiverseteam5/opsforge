"""Worker process: queue consumer + scheduler tick. Entrypoint: `worker`.

Run with `python -m opsforge.worker`. N replicas race for jobs safely via the
SKIP LOCKED claim in db.py — each job is claimed exactly once. Handlers must be
idempotent (a worker can die after claiming, before completing).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .config import get_settings
from .connectors import load_connector
from .db import (
    assert_restricted_role,
    claim_jobs,
    complete_job,
    fail_job,
    scope_to_org,
    session_factory,
)
from .graph import sync_connector

logger = logging.getLogger("opsforge.worker")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def handle_noop(payload: dict[str, Any]) -> None:
    """The M0 smoke job: success is simply not raising."""
    return None


async def handle_graph_sync(payload: dict[str, Any]) -> None:
    """Re-sync one connector's slice of the operational graph (idempotent)."""
    connector_id = payload.get("connector_id")
    org_id = payload.get("org_id")
    if not connector_id or not org_id:
        raise ValueError("graph_sync job missing connector_id or org_id")
    # A1.5: connectors is under FORCE RLS — load_connector requires the job's org.
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    await sync_connector(connector)


async def handle_run_agent(payload: dict[str, Any]) -> None:
    """Run the agent loop for a queued run, then notify its surface (e.g. Slack)."""
    from .agent import run_agent
    from .gateway import LiteLLMGateway
    from .skills import get_skill_by_id
    from .surfaces.slack import notify_run

    run_id = payload.get("run_id")
    org_id = payload.get("org_id", "")
    if not run_id:
        raise ValueError("run_agent job missing run_id")
    async with session_factory().begin() as s:
        if org_id:
            await scope_to_org(s, org_id)
        skill_id = (
            await s.execute(
                text("SELECT skill_id FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one_or_none()
    skill = await get_skill_by_id(skill_id) if skill_id else None
    if skill is None:
        raise ValueError(f"run {run_id} has no installed skill")
    await run_agent(UUID(run_id), skill, LiteLLMGateway(), org_id=org_id)
    # Deliver the report to the run's surface (no-op for non-Slack runs).
    try:
        await notify_run(UUID(run_id))
    except Exception:  # noqa: BLE001 - a delivery failure must not fail the run
        logger.warning("surface notify failed for run %s", run_id, exc_info=True)


async def handle_execute_action(payload: dict[str, Any]) -> None:
    """Execute an approved action through the deterministic executor (Phase 2)."""
    from .actions import execute_action

    action_id = payload.get("action_id")
    org_id = payload.get("org_id")
    if not action_id:
        raise ValueError("execute_action job missing action_id")
    if not org_id:
        raise ValueError("execute_action job missing org_id")
    await execute_action(UUID(action_id), org_id=UUID(org_id))


async def handle_ingest(payload: dict[str, Any]) -> None:
    """Ingest a local markdown folder into the knowledge store (M6.7). Uses real
    gateway embeddings when a provider key is configured, else the keyless hash
    stand-in."""
    from .ingest import configured_embedder, ingest_directory

    org_id = payload.get("org_id")
    path = payload.get("path")
    if not org_id or not path:
        raise ValueError("ingest job missing org_id or path")
    await ingest_directory(path, org_id=org_id, embedder=configured_embedder())


async def handle_ingest_tickets(payload: dict[str, Any]) -> None:
    """Pull resolved tickets through a vault-credentialed connector and ingest them as
    behaviour observations with origin metadata (M7.5). The reconcile pass then
    decides which observations form an authoritative provenance-disjoint pattern."""
    from .ingest import configured_embedder
    from .tickets import ingest_tickets_from_connector

    org_id = payload.get("org_id")
    connector_id = payload.get("connector_id")
    if not org_id or not connector_id:
        raise ValueError("ingest_tickets job missing org_id or connector_id")
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    await ingest_tickets_from_connector(
        connector,
        org_id=org_id,
        embedder=configured_embedder(),
        since_days=int(payload.get("since_days", 90)),
    )


async def handle_ingest_knowledge(payload: dict[str, Any]) -> None:
    """Pull real documents through a vault-credentialed knowledge connector (Confluence,
    Phase B) and ingest them as document chunks with real provenance. Read-only."""
    from .ingest import configured_embedder
    from .knowledge_sources import ingest_knowledge_from_connector

    org_id = payload.get("org_id")
    connector_id = payload.get("connector_id")
    if not org_id or not connector_id:
        raise ValueError("ingest_knowledge job missing org_id or connector_id")
    connector = await load_connector(UUID(connector_id), org_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    _ids, complete = await ingest_knowledge_from_connector(
        connector, org_id=org_id, embedder=configured_embedder(),
        default_process_key=payload.get("process_key"),
    )
    if not complete:
        # honest partial — surface it, do not report a partial pull as a clean success
        logger.warning("ingest_knowledge for connector %s was PARTIAL", connector_id)


async def handle_reconcile(payload: dict[str, Any]) -> None:
    """Reconcile a process's chunks then (re)generate its validated process (M6.7).
    Uses the LLM contradiction detector when a provider key is configured, else
    the lexical stand-in; the deterministic engine disposes either way."""
    from .processes import configured_drafter, generate_process
    from .reconcile import configured_detector, reconcile_process

    org_id = payload.get("org_id")
    process_key = payload.get("process_key")
    if not org_id or not process_key:
        raise ValueError("reconcile job missing org_id or process_key")
    await reconcile_process(org_id, process_key, detector=await configured_detector(org_id))
    await generate_process(org_id, process_key, drafter=configured_drafter())


async def handle_codify_skill(payload: dict[str, Any]) -> None:
    """Analyze a completed run's events and propose a reusable codified skill.

    Idempotent: a SELECT-before-INSERT check prevents duplicate skills on retry.
    The embedding is computed BEFORE opening any DB transaction (it is an HTTP call
    and cannot participate in a Postgres transaction)."""
    import json
    import re

    from sqlalchemy import text

    from .db import record_audit, scope_to_org, session_factory
    from .gateway import LiteLLMGateway
    from .knowledge import _vector_literal
    from .security import redact

    run_id = payload.get("run_id")
    org_id = payload.get("org_id")
    if not run_id or not org_id:
        raise ValueError("codify_skill job missing run_id or org_id")

    # Load run events (tool results, evidence, proposals, and the final report).
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT kind, payload FROM run_events "
                    "WHERE run_id = :rid "
                    "AND kind IN ('tool_result', 'evidence', 'proposal', 'report') "
                    "ORDER BY seq"
                ),
                {"rid": run_id},
            )
        ).all()

    if not rows:
        logger.warning("codify_skill: no usable events for run %s, skipping", run_id)
        return  # permanent non-retryable: nothing to learn from

    # Build and redact the transcript.
    raw_transcript = "\n".join(f"[{r.kind}] {json.dumps(r.payload or {})}" for r in rows)
    transcript = redact(raw_transcript)

    # Truncate: keep first 4K chars (context/trigger) + last 20K chars (resolution).
    _FIRST, _LAST = 4096, 20480
    if len(transcript) > _FIRST + _LAST:
        transcript = transcript[:_FIRST] + "\n...[truncated]...\n" + transcript[-_LAST:]

    _EXTRACT_TOOL = {
        "type": "function",
        "function": {
            "name": "extract_skill_data",
            "description": "Extract a reusable skill from the agent run transcript.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "URL-safe slug, e.g. disk-space-check"},  # noqa: E501
                    "name": {"type": "string", "description": "Human-readable skill name"},
                    "description": {"type": "string", "description": "What this skill investigates"},  # noqa: E501
                    "instructions_md": {"type": "string", "description": "Full INSTRUCTIONS.md content"},  # noqa: E501
                    "skill_yaml": {"type": "string", "description": "Full skill.yaml content"},
                },
                "required": ["slug", "name", "description", "instructions_md", "skill_yaml"],
            },
        },
    }

    gateway = LiteLLMGateway()
    settings = get_settings()

    # Fetch recent operator review notes to ground the LLM in what this org approves.
    async with session_factory().begin() as s:
        feedback_rows = (
            await s.execute(
                text(
                    "SELECT slug, review_note, "
                    "CASE WHEN enabled THEN 'approved' ELSE 'rejected' END AS verdict "
                    "FROM skills "
                    "WHERE org_id = :org AND source = 'codified' "
                    "AND review_note IS NOT NULL AND review_note != '' "
                    "ORDER BY updated_at DESC LIMIT 5"
                ),
                {"org": org_id},
            )
        ).all()

    feedback_block = ""
    if feedback_rows:
        lines = "\n".join(
            f"- [{r.verdict}] {r.slug}: {r.review_note}" for r in feedback_rows
        )
        feedback_block = (
            "\n\nOperator review feedback from previous skills in this org "
            "(use this to align the extracted skill with what the team approves):\n"
            + lines
        )

    result = await gateway.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an OpsForge skill architect. Analyze this agent run transcript "
                    "and produce a reusable skill definition by calling extract_skill_data. "
                    "Capture the investigation pattern, tools used, and reasoning steps."
                    + feedback_block
                ),
            },
            {
                "role": "user",
                "content": f"Agent run transcript:\n\n{transcript}\n\nExtract a reusable skill.",
            },
        ],
        tools=[_EXTRACT_TOOL],
        model=settings.model,
        tool_choice="required",
    )

    if not result.tool_calls:
        logger.warning("codify_skill: LLM did not call extract_skill_data for run %s", run_id)
        return  # permanent non-retryable: LLM declined

    args = result.tool_calls[0].arguments
    slug_raw = (args.get("slug") or f"codified-{run_id[:8]}").lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug_raw)[:64].strip("-")
    name = args.get("name") or slug
    description = args.get("description") or ""
    instructions_md = args.get("instructions_md") or ""
    skill_yaml_str = args.get("skill_yaml") or ""

    # Idempotency: check if a codified skill for this run already exists.
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        existing = (
            await s.execute(
                text(
                    "SELECT id FROM skills "
                    "WHERE org_id = :org AND source = 'codified' "
                    "AND manifest->>'run_id' = :rid"
                ),
                {"org": org_id, "rid": run_id},
            )
        ).first()

    if existing:
        logger.info("codify_skill: skill for run %s already exists (%s)", run_id, existing.id)
        return

    # Compute embedding BEFORE the transaction (HTTP call cannot be in a DB tx).
    summary = f"{name}: {description}"
    embedding: list[float] | None = None
    try:
        vecs = await gateway.embedding([summary], settings.embedding_model)
        embedding = vecs[0] if vecs else None
    except Exception:  # noqa: BLE001 - pattern skipped, skill still proposed
        logger.warning("codify_skill: embedding failed for run %s, pattern skipped", run_id)

    # Parse skill YAML, inject required fields.
    try:
        import yaml as _yaml
        manifest_raw = _yaml.safe_load(skill_yaml_str) or {}
    except Exception:
        manifest_raw = {}
    manifest_raw.setdefault("schema", "opsforge/skill/v1")
    manifest_raw["slug"] = slug
    manifest_raw["source"] = "codified"
    manifest_raw["run_id"] = run_id  # keyed by the unique partial index

    # Single transaction: skills INSERT + patterns INSERT.
    skill_id_str: str | None = None
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)

        skill_row = (
            await s.execute(
                text(
                    "INSERT INTO skills "
                    "(org_id, slug, version, manifest, instructions, source, enabled) "
                    "VALUES (:org, :slug, '0.1.0', CAST(:manifest AS jsonb), "
                    ":instructions, 'codified', false) "
                    "ON CONFLICT (org_id, slug) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "org": org_id,
                    "slug": slug,
                    "manifest": json.dumps(manifest_raw),
                    "instructions": instructions_md,
                },
            )
        ).first()

        if skill_row is None:
            logger.info("codify_skill: slug %r already taken for run %s", slug, run_id)
            return

        skill_id_str = str(skill_row.id)

        if embedding is not None:
            await s.execute(
                text(
                    "INSERT INTO patterns (org_id, run_id, summary, embedding, resolution, outcome) "  # noqa: E501
                    "VALUES (:org, :rid, :summary, CAST(:emb AS vector), :res, CAST(:outcome AS jsonb))"  # noqa: E501
                ),
                {
                    "org": org_id,
                    "rid": run_id,
                    "summary": summary,
                    "emb": _vector_literal(embedding),
                    "res": description,
                    "outcome": json.dumps({"slug": slug, "skill_id": skill_id_str}),
                },
            )

    await record_audit(
        org_id,
        "system:codify",
        "skill.proposed",
        subject_ref=skill_id_str,
        detail={"slug": slug, "run_id": run_id},
    )
    logger.info("codify_skill: proposed skill %r (id=%s) from run %s", slug, skill_id_str, run_id)
    try:
        from .surfaces.slack import notify_skill_proposed
        await notify_skill_proposed(skill_id_str or "", slug, run_id)
    except Exception:  # noqa: BLE001 - delivery failure must not fail the job
        logger.warning("codify_skill: slack notify failed for %r", slug, exc_info=True)


async def handle_codify_from_url(payload: dict[str, Any]) -> None:
    """Codify a runbook from pre-fetched URL content (E4).

    The API layer already fetched and SSRF-validated the content; this handler
    just runs the same LLM codify prompt and inserts a proposed skill.
    """
    import json
    import re

    from sqlalchemy import text

    from .db import record_audit, scope_to_org, session_factory
    from .gateway import LiteLLMGateway
    from .knowledge import _vector_literal
    from .security import redact

    url = payload.get("url", "unknown")
    org_id = payload.get("org_id")
    content = payload.get("content", "")
    if not org_id or not content:
        raise ValueError("codify_from_url job missing org_id or content")

    content = redact(content)[:24576]  # 24K char cap

    _EXTRACT_TOOL = {
        "type": "function",
        "function": {
            "name": "extract_skill_data",
            "description": "Extract a reusable OpsForge skill from a runbook.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "instructions_md": {"type": "string"},
                    "skill_yaml": {"type": "string"},
                },
                "required": ["slug", "name", "description", "instructions_md", "skill_yaml"],
            },
        },
    }

    gateway = LiteLLMGateway()
    settings = get_settings()

    result = await gateway.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an OpsForge skill architect. Convert this runbook into a reusable "
                    "skill definition by calling extract_skill_data. Capture the investigation "
                    "pattern, tools referenced, and step-by-step reasoning."
                ),
            },
            {"role": "user", "content": f"Runbook from {url}:\n\n{content}"},
        ],
        tools=[_EXTRACT_TOOL],
        model=settings.model,
        tool_choice="required",
    )

    if not result.tool_calls:
        logger.warning("codify_from_url: LLM did not call extract_skill_data for %s", url)
        return

    args = result.tool_calls[0].arguments
    slug_raw = (args.get("slug") or f"runbook-{url.split('/')[-1][:32]}").lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug_raw)[:64].strip("-")
    name = args.get("name") or slug
    description = args.get("description") or ""
    instructions_md = args.get("instructions_md") or ""

    try:
        import yaml as _yaml
        manifest_raw = _yaml.safe_load(args.get("skill_yaml") or "") or {}
    except Exception:
        manifest_raw = {}
    manifest_raw.setdefault("schema", "opsforge/skill/v1")
    manifest_raw["slug"] = slug
    manifest_raw["source"] = "codified"
    manifest_raw["runbook_url"] = url

    summary = f"{name}: {description}"
    embedding: list[float] | None = None
    try:
        vecs = await gateway.embedding([summary], settings.embedding_model)
        embedding = vecs[0] if vecs else None
    except Exception:
        logger.warning("codify_from_url: embedding failed for %s", url, exc_info=True)

    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        skill_row = (
            await s.execute(
                text(
                    "INSERT INTO skills "
                    "(org_id, slug, version, manifest, instructions, source, enabled) "
                    "VALUES (:org, :slug, '0.1.0', CAST(:manifest AS jsonb), "
                    ":instructions, 'codified', false) "
                    "ON CONFLICT (org_id, slug) DO NOTHING RETURNING id"
                ),
                {
                    "org": org_id,
                    "slug": slug,
                    "manifest": json.dumps(manifest_raw),
                    "instructions": instructions_md,
                },
            )
        ).first()

        if skill_row is None:
            logger.info("codify_from_url: slug %r already taken", slug)
            return

        skill_id_str = str(skill_row.id)

        if embedding is not None:
            await s.execute(
                text(
                    "INSERT INTO patterns (org_id, run_id, summary, embedding, resolution, outcome) "
                    "VALUES (:org, gen_random_uuid(), :summary, CAST(:emb AS vector), :res, CAST(:outcome AS jsonb))"
                ),
                {
                    "org": org_id,
                    "summary": summary,
                    "emb": _vector_literal(embedding),
                    "res": description,
                    "outcome": json.dumps({"slug": slug, "skill_id": skill_id_str}),
                },
            )

    await record_audit(
        org_id, "system:codify", "skill.proposed_from_url",
        subject_ref=skill_id_str,
        detail={"slug": slug, "url": url},
    )
    logger.info("codify_from_url: proposed skill %r from %s", slug, url)
    try:
        from .surfaces.slack import notify_skill_proposed
        await notify_skill_proposed(skill_id_str, slug, url)
    except Exception:
        logger.warning("codify_from_url: slack notify failed for %r", slug, exc_info=True)


async def handle_postmortem(payload: dict[str, Any]) -> None:
    """Generate an AI postmortem for a completed run and store it as a pattern.

    1. Loads the run's events (tool_result, evidence, proposal, report).
    2. Asks the LLM to produce a structured postmortem (timeline, root cause,
       action items, confidence).
    3. Persists the result in the patterns table.
    4. Optionally posts a Block Kit summary to the Slack on-call channel.

    Idempotent: checks for an existing postmortem pattern before writing.
    """
    import json

    from sqlalchemy import text

    from .db import scope_to_org, session_factory
    from .gateway import LiteLLMGateway
    from .knowledge import _vector_literal
    from .security import redact

    run_id = payload.get("run_id")
    org_id = payload.get("org_id")
    channel = payload.get("channel")  # optional Slack channel override
    if not run_id or not org_id:
        raise ValueError("postmortem job missing run_id or org_id")

    # Load run report and events.
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        run_row = (
            await s.execute(
                text("SELECT report_md, status FROM runs WHERE id = :id"),
                {"id": run_id},
            )
        ).first()
        event_rows = (
            await s.execute(
                text(
                    "SELECT kind, payload FROM run_events "
                    "WHERE run_id = :rid "
                    "AND kind IN ('tool_result', 'evidence', 'proposal', 'report') "
                    "ORDER BY seq"
                ),
                {"rid": run_id},
            )
        ).all()

    if run_row is None:
        raise ValueError(f"run {run_id} not found")

    # Idempotency: skip if postmortem already exists.
    async with session_factory().begin() as s:
        existing = (
            await s.execute(
                text(
                    "SELECT id FROM patterns WHERE org_id = :org "
                    "AND outcome->>'postmortem_run_id' = :rid"
                ),
                {"org": org_id, "rid": run_id},
            )
        ).first()
    if existing:
        logger.info("postmortem: already exists for run %s, skipping", run_id)
        return

    transcript_parts = []
    if run_row.report_md:
        transcript_parts.append(f"[final_report]\n{run_row.report_md}")
    for r in event_rows:
        transcript_parts.append(f"[{r.kind}] {json.dumps(r.payload or {})}")
    transcript = redact("\n".join(transcript_parts))

    # Keep first 4K + last 20K.
    _FIRST, _LAST = 4096, 20480
    if len(transcript) > _FIRST + _LAST:
        transcript = transcript[:_FIRST] + "\n...[truncated]...\n" + transcript[-_LAST:]

    _POSTMORTEM_TOOL = {
        "type": "function",
        "function": {
            "name": "write_postmortem",
            "description": "Write a structured postmortem for an SRE incident run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "timeline": {"type": "string", "description": "Bullet timeline of key events"},
                    "root_cause": {"type": "string"},
                    "contributing_factors": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "action_items": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "summary_md": {"type": "string", "description": "Full postmortem in markdown"},
                },
                "required": ["title", "root_cause", "action_items", "confidence", "summary_md"],
            },
        },
    }

    gateway = LiteLLMGateway()
    settings = get_settings()
    result = await gateway.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an SRE postmortem author. Analyze this agent run transcript "
                    "and write a blameless postmortem by calling write_postmortem. "
                    "Focus on: what happened, why, what prevented earlier detection, "
                    "and concrete action items to prevent recurrence."
                ),
            },
            {
                "role": "user",
                "content": f"Agent run {run_id} transcript:\n\n{transcript}\n\nWrite the postmortem.",
            },
        ],
        tools=[_POSTMORTEM_TOOL],
        model=settings.model,
        tool_choice="required",
    )

    if not result.tool_calls:
        logger.warning("postmortem: LLM did not call write_postmortem for run %s", run_id)
        return

    args = result.tool_calls[0].arguments
    title = args.get("title", f"Postmortem for run {run_id[:8]}")
    summary_md = args.get("summary_md", "")
    root_cause = args.get("root_cause", "")
    action_items = args.get("action_items", [])
    confidence = args.get("confidence", "low")

    # Embed the postmortem for future similarity search.
    embed_text = f"{title}: {root_cause}"
    embedding: list[float] | None = None
    try:
        vecs = await gateway.embedding([embed_text], settings.embedding_model)
        embedding = vecs[0] if vecs else None
    except Exception:  # noqa: BLE001
        logger.warning("postmortem: embedding failed for run %s", run_id, exc_info=True)

    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO patterns (org_id, run_id, summary, embedding, resolution, outcome) "
                "VALUES (:org, :rid, :summary, CAST(:emb AS vector), :res, CAST(:outcome AS jsonb))"
            ),
            {
                "org": org_id,
                "rid": run_id,
                "summary": embed_text,
                "emb": _vector_literal(embedding) if embedding else None,
                "res": root_cause,
                "outcome": json.dumps({
                    "type": "postmortem",
                    "postmortem_run_id": run_id,
                    "title": title,
                    "confidence": confidence,
                    "action_items": action_items,
                    "summary_md": summary_md,
                }),
            },
        )

    logger.info("postmortem: stored for run %s — %r", run_id, title)

    # Deliver to Slack if a channel is configured (failure must not fail the job).
    target_channel = channel or settings.skill_review_channel
    if target_channel:
        try:
            from .surfaces.slack import post_message

            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"Postmortem: {title}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:* {root_cause}"}},
            ]
            if action_items:
                items_md = "\n".join(f"• {a}" for a in action_items[:5])
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Action items:*\n{items_md}"},
                })
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Confidence: *{confidence}* | Run: `{run_id}`"}],
            })
            await post_message(target_channel, f"Postmortem: {title}", blocks)
        except Exception:  # noqa: BLE001
            logger.warning("postmortem: slack delivery failed for run %s", run_id, exc_info=True)


async def handle_ingest_slack_history(payload: dict[str, Any]) -> None:
    """Ingest past Slack incident threads as behaviour knowledge chunks (C7).

    Fetches message history from a Slack channel via the Web API (using the
    same bot token as the rest of the Slack surface), groups messages into
    incident threads, and stores them as behaviour chunks in the knowledge store.

    Payload keys:
      org_id      — organisation UUID (required)
      channel_id  — Slack channel ID to ingest (required)
      since_days  — how far back to look (default 180)
      process_key — knowledge process key to tag chunks under
    """
    import time as _time
    from datetime import UTC, datetime

    import httpx

    from .ingest import configured_embedder
    from .knowledge import PendingChunk, ProvenanceEnvelope, store_chunks
    from .security import redact

    org_id = payload.get("org_id")
    channel_id = payload.get("channel_id")
    if not org_id or not channel_id:
        raise ValueError("ingest_slack_history job missing org_id or channel_id")

    settings = get_settings()
    if not settings.slack_bot_token:
        logger.warning("ingest_slack_history: no slack_bot_token configured, skipping")
        return

    since_days = int(payload.get("since_days", 180))
    process_key = payload.get("process_key", "slack_incidents")
    embedder = configured_embedder()
    oldest_ts = str(int(_time.time()) - since_days * 86400)
    ingested_at = datetime.now(UTC)

    chunks_ingested = 0
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                params={"channel": channel_id, "oldest": oldest_ts, "limit": 200},
            )
        data = resp.json()
        if not data.get("ok"):
            logger.warning(
                "ingest_slack_history: Slack API error: %s", data.get("error", "unknown")
            )
            return

        messages = data.get("messages", [])

        # Group messages by thread_ts to reconstruct incident threads.
        threads: dict[str, list[dict]] = {}
        for msg in messages:
            ts = msg.get("thread_ts") or msg.get("ts", "")
            if ts not in threads:
                threads[ts] = []
            threads[ts].append(msg)

        # Collect thread texts + refs before embedding.
        thread_data: list[tuple[str, str]] = []
        for ts, msgs in threads.items():
            thread_text = "\n".join(
                f"[{m.get('ts', '')}] {m.get('text', '')}"
                for m in sorted(msgs, key=lambda m: m.get("ts", ""))
            )
            thread_text = thread_text.strip()
            if len(thread_text) < 50:
                continue
            thread_data.append((redact(thread_text[:8192]), f"slack:{channel_id}:{ts}"))

        if thread_data:
            texts = [t[0] for t in thread_data]
            vectors = await embedder(texts)
            pending: list[PendingChunk] = [
                PendingChunk(
                    content=safe_text,
                    envelope=ProvenanceEnvelope(
                        source_kind="behaviour",
                        source_ref=source_ref,
                        observed_at=ingested_at,
                        ingested_at=ingested_at,
                    ),
                    embedding=vectors[i],
                    process_key=process_key,
                )
                for i, (safe_text, source_ref) in enumerate(thread_data)
            ]
            ids = await store_chunks(org_id, pending)
            chunks_ingested = len(ids)

    except Exception:  # noqa: BLE001
        logger.warning(
            "ingest_slack_history: partial failure for channel %s", channel_id, exc_info=True
        )

    logger.info(
        "ingest_slack_history: ingested %d chunks from channel %s", chunks_ingested, channel_id
    )


# Dispatch table by job.kind.
HANDLERS: dict[str, Handler] = {
    "noop": handle_noop,
    "graph_sync": handle_graph_sync,
    "run_agent": handle_run_agent,
    "execute_action": handle_execute_action,
    "ingest": handle_ingest,
    "ingest_tickets": handle_ingest_tickets,
    "ingest_knowledge": handle_ingest_knowledge,
    "reconcile": handle_reconcile,
    "codify_skill": handle_codify_skill,
    "codify_from_url": handle_codify_from_url,
    "postmortem": handle_postmortem,
    "ingest_slack_history": handle_ingest_slack_history,
}


# Enqueue due connector syncs, but only every _TICK_EVERY_S so 3 workers polling
# at 500ms don't hammer the table.
_TICK_EVERY_S = 30.0
_last_tick = 0.0

_ENQUEUE_DUE_SYNCS = text(
    """
    INSERT INTO jobs (org_id, kind, payload, status, run_after, attempts)
    SELECT c.org_id, 'graph_sync',
           jsonb_build_object('connector_id', c.id::text, 'org_id', c.org_id::text),
           'queued', now(), 0
    FROM connectors c
    WHERE c.status = 'healthy'
      AND c.org_id = :org
      AND NOT EXISTS (
          SELECT 1 FROM jobs j
          WHERE j.kind = 'graph_sync' AND j.status IN ('queued', 'running')
            AND j.payload->>'connector_id' = c.id::text
      )
      AND NOT EXISTS (
          SELECT 1 FROM graph_nodes g
          WHERE g.source_connector_id = c.id
            AND g.last_seen_at > now() - make_interval(secs => :interval)
      )
    """
)


async def scheduler_tick() -> None:
    """Enqueue graph_sync jobs for due connectors and run_agent jobs for due
    cron schedules. Throttled to once per _TICK_EVERY_S across the worker pool."""
    global _last_tick
    now = time.monotonic()
    if now - _last_tick < _TICK_EVERY_S:
        return
    _last_tick = now
    org = get_settings().org_id
    async with session_factory().begin() as s:
        # Insert into the RLS-protected jobs table → declare the worker's org.
        await scope_to_org(s, org)
        await s.execute(
            _ENQUEUE_DUE_SYNCS,
            {"interval": get_settings().graph_sync_interval_s, "org": org},
        )
    await _run_due_cron_schedules()
    await _expire_credential_leases()


async def _expire_credential_leases() -> None:
    from .credentials import expire_leases
    expired = await expire_leases()
    if expired:
        logger.debug("expired %d credential leases", expired)


async def _run_due_cron_schedules() -> None:
    from croniter import croniter

    from .dispatch import create_run
    from .skills import get_skill_by_id

    org = get_settings().org_id
    async with session_factory().begin() as s:
        # org-pinned worker: only fire this org's schedules, never a peer org's.
        due = (
            await s.execute(
                text(
                    "SELECT id, skill_id, cron_expr FROM schedules "
                    "WHERE enabled AND trigger_kind='cron' AND next_run_at <= now() "
                    "AND org_id = :org "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"org": org},
            )
        ).all()
        for sched in due:
            # Advance next_run_at inside the same locked txn so peers don't double-fire.
            nxt = croniter(sched.cron_expr, datetime.now(UTC)).get_next(datetime)
            await s.execute(
                text("UPDATE schedules SET next_run_at=:n WHERE id=:id"),
                {"n": nxt, "id": sched.id},
            )

    for sched in due:
        skill = await get_skill_by_id(sched.skill_id)
        if skill is None:
            continue
        result = await create_run(
            skill["slug"],
            {"query": f"scheduled run of {skill['slug']}"},
            trigger_kind="schedule",
        )
        if result:
            async with session_factory().begin() as s:
                await s.execute(
                    text("UPDATE schedules SET last_run_id=:r WHERE id=:id"),
                    {"r": result["run_id"], "id": sched.id},
                )


async def process_one(
    worker_id: str, *, max_attempts: int, org_id: str | None = None
) -> dict[str, Any] | None:
    """Claim and run a single job for this worker's org. Returns the claimed job
    dict, or None if the queue was empty. The claim commits in its own short
    transaction before the handler runs, so the row lock is never held across
    handler execution. The worker is org-pinned (M6.0): it only ever sees its own
    org's jobs."""
    org = org_id or get_settings().org_id
    async with session_factory().begin() as session:
        claimed = await claim_jobs(session, worker_id=worker_id, org_id=org, batch=1)
    if not claimed:
        return None

    job = claimed[0]
    # The org that gates connector/credential access is the AUTHORITATIVE job org —
    # RLS-validated and pinned at claim (claim_jobs RETURNING j.org_id) — never free-form
    # payload JSON. Stamp it over any payload org_id so a poisoned/legacy payload cannot
    # re-scope a handler (defense-in-depth now that connectors holds real credentials).
    payload = dict(job["payload"] or {})
    payload["org_id"] = str(job["org_id"])
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise ValueError(f"no handler for job kind {job['kind']!r}")
        await handler(payload)
        async with session_factory().begin() as session:
            await complete_job(session, job["id"], org_id=org)
    except Exception:
        logger.exception("job %s (%s) failed", job["id"], job["kind"])
        async with session_factory().begin() as session:
            await fail_job(session, job["id"], max_attempts=max_attempts, org_id=org)
    return job


async def run_forever(shutdown: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.worker_poll_interval_ms / 1000
    await assert_restricted_role()
    try:
        from .skills import install_builtin_skills

        await install_builtin_skills()
    except Exception:  # noqa: BLE001 - don't block the worker on skill install
        logger.warning("built-in skill install skipped", exc_info=True)
    logger.info("worker %s started", WORKER_ID)
    while not shutdown.is_set():
        await scheduler_tick()
        job = await process_one(
            WORKER_ID,
            max_attempts=settings.worker_max_attempts,
            org_id=settings.org_id,
        )
        if job is not None:
            # Greedily drain: loop again immediately when work was found.
            continue
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except TimeoutError:
            pass
    logger.info("worker %s stopped", WORKER_ID)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    # psycopg3 async requires a SelectorEventLoop on Windows (native dev only;
    # the container runs Linux). No-op elsewhere.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    shutdown = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_shutdown() -> None:
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows native dev: signal handlers on the loop are unsupported.
            pass

    try:
        loop.run_until_complete(run_forever(shutdown))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
