"""Slack surface: inbound events/commands + outbound Block Kit reports.

Raw Events API over httpx (no Bolt, ~150 lines). The same 4-function adapter
shape — on_message, on_action, render_report, notify — is what a future Teams
adapter implements. Phase 1 renders proposals as "suggested fix" text only; no
Approve buttons (those arrive with the executor in M5).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text

from ..actions import approve_action, deny_action, dry_run_action
from ..config import get_settings
from ..db import session_factory
from ..dispatch import create_run, resolve_nl
from ..reports import RcaReport, render_slack_blocks

DEFAULT_SKILL = "incident-investigation"
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# A poster sends a rendered message to a channel. Swappable in tests.
Poster = Callable[[str, str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]


# --------------------------------------------------------------------------- #
# Signature verification (Slack signing secret)
# --------------------------------------------------------------------------- #
def verify_signature(timestamp: str | None, signature: str | None, body: bytes) -> bool:
    secret = get_settings().slack_signing_secret
    if not secret:
        return True  # dev: not configured
    if not timestamp or not signature:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', 'replace')}".encode()
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #
async def post_message(
    channel: str, text_summary: str, blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    token = get_settings().slack_bot_token
    if not token:
        # Dev / tests with no workspace: render but don't send.
        return {"ok": False, "skipped": "no slack_bot_token", "channel": channel}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text_summary, "blocks": blocks},
        )
    return resp.json()


def render_report(report: dict[str, Any], run_id: str | None = None) -> list[dict[str, Any]]:
    """rca_v1 JSON -> Block Kit. Phase 2: each proposal gets Approve / Dry-run /
    Dismiss buttons whose value carries the action id."""
    blocks = render_slack_blocks(RcaReport.model_validate(report))
    for action_id in report.get("proposals", []) or []:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"act:{action_id}",
                "elements": [
                    {
                        "type": "button", "action_id": "approve", "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": action_id,
                    },
                    {
                        "type": "button", "action_id": "dry_run",
                        "text": {"type": "plain_text", "text": "Dry-run"},
                        "value": action_id,
                    },
                    {
                        "type": "button", "action_id": "deny", "style": "danger",
                        "text": {"type": "plain_text", "text": "Dismiss"},
                        "value": action_id,
                    },
                ],
            }
        )
    if run_id:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"OpsForge run `{run_id}`"}],
            }
        )
    return blocks


async def notify_skill_proposed(
    skill_id: str, slug: str, run_id: str, poster: Poster | None = None
) -> dict[str, Any]:
    """Post a Block Kit alert to the skill-review channel when a skill is proposed."""
    channel = get_settings().skill_review_channel
    if not channel:
        return {"ok": False, "skipped": "no skill_review_channel configured"}
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New skill proposed for review* :robot_face:\n"
                    f"`{slug}` was codified from run `{run_id[:8]}`.\n"
                    f"Review it in the workbench under *Skills → Proposed skills*."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"skill id: `{skill_id}` · run: `{run_id}`"}
            ],
        },
    ]
    send = poster or post_message
    result = await send(channel, f"New skill proposed: {slug}", blocks)
    return {"ok": True, "channel": channel, "result": result}


async def notify_run(run_id: UUID, poster: Poster | None = None) -> dict[str, Any]:
    """Post a finished run's RCA to its Slack channel, if it has one."""
    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text(
                    "SELECT trigger, report_json, report_md FROM runs WHERE id=:id"
                ),
                {"id": run_id},
            )
        ).first()
    if row is None:
        return {"ok": False, "error": "run not found"}
    trigger = row.trigger or {}
    if trigger.get("surface") != "slack" or not trigger.get("channel"):
        return {"ok": False, "skipped": "not a slack run"}
    if not row.report_json:
        return {"ok": False, "skipped": "no report"}

    blocks = render_report(row.report_json, str(run_id))
    summary = row.report_json.get("hypothesis", "OpsForge RCA ready")
    send = poster or post_message
    result = await send(trigger["channel"], summary, blocks)
    return {"ok": True, "channel": trigger["channel"], "result": result}


# --------------------------------------------------------------------------- #
# Inbound (Events API + slash command)
# --------------------------------------------------------------------------- #
def _candidate_blocks(nl: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Disambiguation buttons — the surface picks, OpsForge never guesses."""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"Which investigation for “{nl}”?"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button", "action_id": "pick_skill",
                    "text": {"type": "plain_text", "text": c["name"][:75]},
                    "value": f"{c['slug']}::{nl}"[:1900],
                }
                for c in candidates
            ],
        },
    ]


async def handle_events(body: bytes) -> dict[str, Any]:
    payload = json.loads(body or b"{}")
    kind = payload.get("type")

    if kind == "url_verification":
        return {"challenge": payload.get("challenge")}

    if kind == "event_callback":
        event = payload.get("event", {})
        if event.get("type") in ("app_mention", "message") and event.get("text"):
            query = _MENTION_RE.sub("", event["text"]).strip()
            channel = event.get("channel")
            if query and channel:
                resolved = await resolve_nl(
                    query, surface="slack", channel=channel, user_id=event.get("user")
                )
                if resolved.get("status") == "ambiguous":
                    await post_message(
                        channel, "Which investigation?",
                        _candidate_blocks(query, resolved["candidates"]),
                    )
    return {"ok": True}


async def handle_slash(form: dict[str, Any]) -> dict[str, Any]:
    """`/ops <nl>` slash command -> resolve + dispatch (or offer candidates)."""
    query = (form.get("text") or "").strip()
    channel = form.get("channel_id")
    if not query or not channel:
        return {"response_type": "ephemeral", "text": "Usage: /ops <question>"}
    resolved = await resolve_nl(
        query, surface="slack", channel=channel, user_id=form.get("user_id")
    )
    if resolved.get("status") == "ambiguous":
        return {"response_type": "ephemeral",
                "blocks": _candidate_blocks(query, resolved["candidates"])}
    return {
        "response_type": "ephemeral",
        "text": f"On it — investigating “{query}”. I’ll post the RCA here when ready.",
    }


# --------------------------------------------------------------------------- #
# 4-function surface adapter (Teams/others implement the same shape)
# --------------------------------------------------------------------------- #
class SlackSurface:
    async def on_message(self, body: bytes) -> dict[str, Any]:
        return await handle_events(body)

    async def on_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Route an Approve / Dry-run / Dismiss button to the actions executor.

        Slack is a trusted surface configured by an admin, so its approvals run
        with operator authority (mapping Slack users to OpsForge roles is a later
        enhancement)."""
        actions = payload.get("actions", [])
        if not actions:
            return {"ok": True}
        button = actions[0]
        action_id_raw = button.get("value")
        choice = button.get("action_id")
        user = (payload.get("user") or {}).get("id", "unknown")
        actor = f"slack:{user}"

        # Disambiguation pick: dispatch the chosen skill with the original NL.
        if choice == "pick_skill":
            slug, _, nl = (action_id_raw or "").partition("::")
            channel = (payload.get("channel") or {}).get("id") or (
                payload.get("container") or {}
            ).get("channel_id")
            await create_run(
                slug, {"query": nl}, trigger_kind="manual",
                surface="slack", channel=channel, user_id=user,
            )
            return {"ok": True, "dispatched": slug}

        try:
            aid = UUID(action_id_raw)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad action id"}
        try:
            if choice == "approve":
                return await approve_action(aid, actor_role="operator", actor=actor)
            if choice == "dry_run":
                return await dry_run_action(aid, actor=actor)
            if choice == "deny":
                return await deny_action(aid, actor=actor)
        except Exception as exc:  # noqa: BLE001 - surface a friendly ack
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def render_report(self, report: dict[str, Any], run_id: str | None = None):
        return render_report(report, run_id)

    async def notify(self, run_id: UUID, poster: Poster | None = None):
        return await notify_run(run_id, poster)


# --------------------------------------------------------------------------- #
# Inbound routes (mounted by main.create_app). Signature-verified, no bearer.
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/api/v1/webhooks/slack", tags=["slack"])
_surface = SlackSurface()


def _require_signature(timestamp: str | None, signature: str | None, body: bytes) -> None:
    if not verify_signature(timestamp, signature, body):
        raise HTTPException(status_code=401, detail="bad slack signature")


@router.post("/events")
async def slack_events(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    _require_signature(x_slack_request_timestamp, x_slack_signature, body)
    return await _surface.on_message(body)


@router.post("/commands")
async def slack_commands(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    _require_signature(x_slack_request_timestamp, x_slack_signature, body)
    form = dict((await request.form()).items())
    return await handle_slash(form)


@router.post("/interactivity")
async def slack_interactivity(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    _require_signature(x_slack_request_timestamp, x_slack_signature, body)
    form = dict((await request.form()).items())
    raw = form.get("payload", "{}")
    payload = json.loads(raw if isinstance(raw, str) else "{}")
    return await _surface.on_action(payload)
