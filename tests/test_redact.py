"""Redaction is a pure function — no DB needed. Doctrine #8."""

from __future__ import annotations

from opsforge.security import redact


def test_masks_secret_keys():
    out = redact(
        {
            "password": "hunter2",
            "api_key": "abc123",
            "Authorization": "Bearer xyz",
            "username": "alice",
            "nested": {"db_secret": "s3cr3t", "host": "db.local"},
        }
    )
    assert out["password"] == "***REDACTED***"
    assert out["api_key"] == "***REDACTED***"
    assert out["Authorization"] == "***REDACTED***"
    assert out["username"] == "alice"
    assert out["nested"]["db_secret"] == "***REDACTED***"
    assert out["nested"]["host"] == "db.local"


def test_masks_fernet_token_substring():
    token = "gAAAAABf" + "A" * 40
    out = redact({"note": f"creds are {token} ok"})
    assert token not in out["note"]
    assert "***REDACTED***" in out["note"]


def test_masks_inline_secret_in_free_text():
    # A log line is a plain string, not a secret-keyed dict value.
    out = redact("connecting with password=hunter2 to db")
    assert "hunter2" not in out
    assert "password=***REDACTED***" in out
    out2 = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out2


def test_recurses_into_lists():
    out = redact([{"token": "x"}, {"keep": "y"}])
    assert out[0]["token"] == "***REDACTED***"
    assert out[1]["keep"] == "y"


def test_idempotent():
    once = redact({"password": "p", "ok": "v"})
    twice = redact(once)
    assert once == twice


def test_non_secret_scalars_unchanged():
    assert redact(42) == 42
    assert redact("plain string") == "plain string"
    assert redact(None) is None
