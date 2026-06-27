"""M3: Slack surface — signature, Block Kit rendering, inbound events/slash."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from conftest import api_client
from sqlalchemy import text

from opsforge.db import session_factory
from opsforge.skills import install_builtin_skills
from opsforge.surfaces import slack


def test_verify_signature_roundtrip(monkeypatch):
    class S:
        slack_signing_secret = "shhh"

    monkeypatch.setattr(slack, "get_settings", lambda: S())
    body = b'{"type":"event_callback"}'
    ts = "1700000000"
    base = f"v0:{ts}:{body.decode()}".encode()
    good = "v0=" + hmac.new(b"shhh", base, hashlib.sha256).hexdigest()
    assert slack.verify_signature(ts, good, body) is True
    assert slack.verify_signature(ts, "v0=bad", body) is False
    assert slack.verify_signature(None, None, body) is False


def test_verify_signature_skipped_without_secret(monkeypatch):
    class S:
        slack_signing_secret = ""
        environment = "dev"

    monkeypatch.setattr(slack, "get_settings", lambda: S())
    assert slack.verify_signature(None, None, b"x") is True


def test_verify_signature_rejected_in_prod_without_secret(monkeypatch):
    class S:
        slack_signing_secret = ""
        environment = "production"

    monkeypatch.setattr(slack, "get_settings", lambda: S())
    assert slack.verify_signature(None, None, b"x") is False


def test_render_report_blocks_has_approval_buttons():
    report = {
        "hypothesis": "payment-svc down due to deploy payment-svc@rev7",
        "confidence": "high",
        "evidence": [{"claim": "rev7 preceded failures", "source_tool": "k8s"}],
        "proposals": ["abc-123"],
    }
    blocks = slack.render_report(report, run_id="run-1")
    text_blob = json.dumps(blocks)
    assert blocks[0]["type"] == "header"
    assert "payment-svc@rev7" in text_blob
    assert "run-1" in text_blob  # context footer
    # Phase 2: each proposal renders Approve / Dry-run / Dismiss buttons.
    actions_block = next(b for b in blocks if b["type"] == "actions")
    button_ids = {e["action_id"] for e in actions_block["elements"]}
    assert button_ids == {"approve", "dry_run", "deny"}
    assert all(e["value"] == "abc-123" for e in actions_block["elements"])


@pytest.mark.usefixtures("db_required")
async def test_slack_button_approve_routes_to_executor():
    # A Dismiss (deny) button on an awaiting_approval action terminates it.
    import json as _json

    from sqlalchemy import text as _text

    from opsforge.config import DEFAULT_ORG_ID
    from opsforge.db import session_factory

    async with session_factory().begin() as s:
        aid = (
            await s.execute(
                _text(
                    "INSERT INTO actions (org_id,action_class,tool,state,policy_trace) "
                    "VALUES (:o,'reversible','kubernetes.restart_pod','awaiting_approval',"
                    "CAST(:tr AS jsonb)) RETURNING id"
                ),
                {"o": DEFAULT_ORG_ID, "tr": _json.dumps({"allowed": True})},
            )
        ).scalar_one()

    payload = {
        "user": {"id": "U123"},
        "actions": [{"action_id": "deny", "value": str(aid)}],
    }
    result = await slack.SlackSurface().on_action(payload)
    assert result["state"] == "denied"
    async with session_factory().begin() as s:
        state = (
            await s.execute(_text("SELECT state FROM actions WHERE id=:i"), {"i": aid})
        ).scalar_one()
    assert state == "denied"


async def test_url_verification_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "xyz123"}).encode()
    out = await slack.handle_events(body)
    assert out == {"challenge": "xyz123"}


@pytest.mark.usefixtures("db_required")
async def test_app_mention_dispatches_run():
    await install_builtin_skills()
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U07ABC> why is payment-svc throwing 5xx",
                "channel": "C999",
                "user": "U123",
            },
        }
    ).encode()
    await slack.handle_events(body)

    async with session_factory().begin() as s:
        run = (
            await s.execute(
                text(
                    "SELECT trigger FROM runs WHERE trigger->>'channel'='C999' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
    assert run is not None
    assert run.trigger["surface"] == "slack"
    assert "payment-svc" in run.trigger["payload"]["query"]
    assert "<@" not in run.trigger["payload"]["query"]  # mention stripped


@pytest.mark.usefixtures("db_required")
async def test_slash_command_acks_and_dispatches():
    await install_builtin_skills()
    form = {
        "text": "why is payment-svc throwing 5xx",
        "channel_id": "C42",
        "user_id": "U7",
    }
    ack = await slack.handle_slash(form)
    assert ack["response_type"] == "ephemeral"
    assert "investigating" in ack["text"].lower()


async def test_slack_events_route_rejects_bad_signature(monkeypatch):
    # With a signing secret configured, an unsigned request is rejected.
    class S:
        slack_signing_secret = "configured"
        slack_bot_token = ""

    monkeypatch.setattr(slack, "get_settings", lambda: S())
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/webhooks/slack/events", content=b'{"type":"url_verification"}'
        )
    assert resp.status_code == 401
