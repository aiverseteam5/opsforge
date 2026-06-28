"""Trust Ladder API: per-tool execution counts and graduation progress (E6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..config import get_settings
from ..db import scope_to_org, session_factory
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/trust-ladder", tags=["trust-ladder"])


@router.get("")
async def get_trust_ladder(principal: Principal = Depends(require_token)):
    """Return per-tool execution stats and graduation progress.

    Graduation uses all-time execution counts (no time window). A tool is
    eligible for auto_with_notify once it has OPSFORGE_GRADUATION_MIN_EXECUTIONS
    clean (executed, not rolled back) runs with 0 rollbacks.
    """
    min_execs = get_settings().graduation_min_executions

    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT tool, action_class, "
                    "count(*) AS total, "
                    "count(*) FILTER (WHERE state = 'succeeded' AND rolled_back_at IS NULL) AS clean, "
                    "count(*) FILTER (WHERE rolled_back_at IS NOT NULL) AS rolled_back "
                    "FROM actions WHERE org_id = :org "
                    "GROUP BY tool, action_class ORDER BY tool"
                ),
                {"org": principal.org_id},
            )
        ).all()

    items = []
    for r in rows:
        clean = int(r.clean)
        rolled_back = int(r.rolled_back)
        eligible = r.action_class != "destructive" and clean >= min_execs and rolled_back == 0
        items.append(
            {
                "tool": r.tool,
                "action_class": r.action_class,
                "total_executions": int(r.total),
                "clean_executions": clean,
                "rollbacks": rolled_back,
                "graduation_threshold": min_execs,
                "eligible_for_graduation": eligible,
            }
        )

    return {"items": items, "graduation_threshold": min_execs}
