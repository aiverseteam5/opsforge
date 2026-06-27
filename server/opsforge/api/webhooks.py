"""Webhooks: change events (M1) and alert ingest (M3).

Unauthenticated by bearer token — HMAC-signature-verified instead. The Slack
webhook lives in surfaces/slack.py (its own signed router).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text

from ..config import get_settings
from ..db import session_factory
from ..dispatch import dispatch_from_alert
from ..ratelimit import webhook_rate_limit
from ..security import verify_webhook_signature

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


_INSERT_CHANGE = text(
    """
    INSERT INTO changes (org_id, kind, ref, summary, diff, target_keys, occurred_at,
                         source_connector_id)
    VALUES (:org, :kind, :ref, :summary, :diff, :target_keys,
            COALESCE(CAST(:occurred_at AS timestamptz), now()), NULL)
    ON CONFLICT (source_connector_id, kind, ref) DO UPDATE
        SET summary = EXCLUDED.summary, diff = EXCLUDED.diff,
            target_keys = EXCLUDED.target_keys
    RETURNING id
    """
)


@router.post("/change")
async def webhook_change(
    request: Request,
    x_opsforge_signature: str | None = Header(default=None),
    _rl: None = Depends(webhook_rate_limit),
) -> dict[str, Any]:
    """Ingest a deploy/config change (e.g. from CI/CD) into the change timeline."""
    body = await request.body()
    if not verify_webhook_signature(body, x_opsforge_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    async with session_factory().begin() as s:
        change_id = (
            await s.execute(
                _INSERT_CHANGE,
                {
                    "org": get_settings().org_id,
                    "kind": payload.get("kind", "deploy"),
                    "ref": payload.get("ref"),
                    "summary": payload.get("summary"),
                    "diff": payload.get("diff"),
                    "target_keys": payload.get("target_keys"),
                    "occurred_at": payload.get("occurred_at"),
                },
            )
        ).scalar_one()
    return {"id": str(change_id), "status": "recorded"}


@router.post("/alert")
async def webhook_alert(
    request: Request,
    x_opsforge_signature: str | None = Header(default=None),
    _rl: None = Depends(webhook_rate_limit),
) -> dict[str, Any]:
    """Generic alert ingest. Matches enabled event schedules and dispatches an
    investigation per match, reporting to each schedule's configured surface."""
    body = await request.body()
    if not verify_webhook_signature(body, x_opsforge_signature):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        alert = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not valid JSON") from None
    if not isinstance(alert, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    dispatched = await dispatch_from_alert(alert)
    return {"dispatched": dispatched, "count": len(dispatched)}
