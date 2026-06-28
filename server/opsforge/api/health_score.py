"""Health score API: predictive preparedness via ANN on patterns.

Score semantics (E3): similarity to RESOLVED patterns = coverage by known playbooks.
High score = current activity matches something we've fixed before.
Low score = uncharted territory, no playbook.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..config import get_settings
from ..db import scope_to_org, session_factory
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/health-score", tags=["health-score"])
logger = logging.getLogger("opsforge.health_score")

# In-memory cache: org_id -> {"score": float|None, "label": str, "message": str,
#                              "top_patterns": list, "computed_at": float}
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_TTL_S = 300  # 5 minutes


def _label(score: float | None) -> str:
    if score is None:
        return "insufficient_data"
    if score > 0.7:
        return "healthy"
    if score > 0.4:
        return "degraded"
    return "critical"


async def _compute_health(org_id: str) -> dict[str, Any]:
    """Compute health score for an org. Expensive — results are cached."""
    from ..gateway import LiteLLMGateway
    from ..knowledge import _vector_literal

    settings = get_settings()

    # Count patterns first (graceful degradation).
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        pattern_count: int = (
            await s.execute(
                text("SELECT count(*) FROM patterns WHERE org_id=:org"),
                {"org": org_id},
            )
        ).scalar_one()

    if pattern_count < 3:
        return {
            "score": None,
            "label": "insufficient_data",
            "message": "Health scoring requires at least 3 resolved incidents.",
            "top_patterns": [],
        }

    # Embed the last 24h of evidence/proposal events as the "current activity" vector.
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        event_rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events "
                    "WHERE run_id IN (SELECT id FROM runs WHERE org_id=:org) "
                    "AND kind IN ('evidence', 'proposal') "
                    "AND created_at > now() - interval '24 hours' "
                    "ORDER BY created_at DESC LIMIT 200"
                ),
                {"org": org_id},
            )
        ).all()

    if not event_rows:
        return {
            "score": None,
            "label": "insufficient_data",
            "message": "No recent activity to score. Run an investigation first.",
            "top_patterns": [],
        }

    activity_text = " ".join(
        str(r.payload.get("claim") or r.payload.get("summary") or "")
        for r in event_rows
    )[:4096]

    gateway = LiteLLMGateway()
    try:
        vecs = await gateway.embedding([activity_text], settings.embedding_model)
        query_vec = vecs[0] if vecs else None
    except Exception:
        logger.warning("health_score: embedding failed for org %s", org_id, exc_info=True)
        query_vec = None

    if query_vec is None:
        return {
            "score": None,
            "label": "insufficient_data",
            "message": "Embedding unavailable — check LLM provider config.",
            "top_patterns": [],
        }

    vec_lit = _vector_literal(query_vec)
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT id, summary, 1 - (embedding <=> CAST(:qv AS vector)) AS similarity "
                    "FROM patterns WHERE org_id=:org AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:qv AS vector) LIMIT 5"
                ),
                {"org": org_id, "qv": vec_lit},
            )
        ).all()

    if not rows:
        return {
            "score": None,
            "label": "insufficient_data",
            "message": "No patterns with embeddings yet.",
            "top_patterns": [],
        }

    top_score = float(rows[0].similarity) if rows else 0.0
    top_patterns = [
        {"id": str(r.id), "similarity": round(float(r.similarity), 4), "summary": r.summary}
        for r in rows
    ]
    label = _label(top_score)
    if label == "healthy":
        msg = f"{len(top_patterns)} recent pattern(s) match current activity"
    elif label == "degraded":
        msg = "Partial coverage — some current activity is unfamiliar"
    else:
        msg = "Low coverage — current situation has few matching playbooks"

    return {
        "score": round(top_score, 4),
        "label": label,
        "message": msg,
        "top_patterns": top_patterns,
    }


async def get_cached_health(org_id: str) -> dict[str, Any]:
    """Return cached result or compute inline on cache miss."""
    cached = _CACHE.get(org_id)
    if cached and (time.monotonic() - cached["computed_at"]) < _CACHE_TTL_S:
        return {k: v for k, v in cached.items() if k != "computed_at"}
    try:
        result = await asyncio.wait_for(_compute_health(org_id), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("health_score: compute timed out for org %s", org_id)
        result = {
            "score": None,
            "label": "insufficient_data",
            "message": "Health score computation timed out.",
            "top_patterns": [],
        }
    _CACHE[org_id] = {**result, "computed_at": time.monotonic()}
    return result


@router.get("")
async def health_score(principal: Principal = Depends(require_token)):
    """Return the org's predictive health score based on pattern similarity."""
    return await get_cached_health(principal.org_id)
