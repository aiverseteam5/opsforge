"""M1: the change webhook lands deploy events in the change timeline + HMAC."""

from __future__ import annotations

import hashlib
import hmac

from conftest import api_client
from sqlalchemy import text

from opsforge import security
from opsforge.db import session_factory


async def test_change_webhook_records_change(db_required):
    ref = "ci-build-12345"
    async with api_client() as client:
        resp = await client.post(
            "/api/v1/webhooks/change",
            json={
                "kind": "deploy",
                "ref": ref,
                "summary": "Deployed payment-svc v43 via CI",
                "target_keys": ["service://payment-svc"],
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "recorded"

    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text("SELECT kind, summary, target_keys FROM changes WHERE ref = :r"),
                {"r": ref},
            )
        ).first()
    assert row is not None
    assert row.kind == "deploy"
    assert "service://payment-svc" in row.target_keys


def test_hmac_verification(monkeypatch):
    class FakeSettings:
        webhook_secret = "topsecret"

    monkeypatch.setattr(security, "get_settings", lambda: FakeSettings())
    body = b'{"ref":"x"}'
    good = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    assert security.verify_webhook_signature(body, good) is True
    assert security.verify_webhook_signature(body, "sha256=deadbeef") is False
    assert security.verify_webhook_signature(body, None) is False


def test_hmac_skipped_when_no_secret(monkeypatch):
    class FakeSettings:
        webhook_secret = ""

    monkeypatch.setattr(security, "get_settings", lambda: FakeSettings())
    assert security.verify_webhook_signature(b"anything", None) is True
