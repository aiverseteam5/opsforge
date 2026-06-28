"""Phase 4 security-path unit tests (E3-E6 / A2A).

Covers P1 security gaps identified in the /ship coverage audit:
- Slack timestamp replay-attack protection
- SSRF guard (_is_private_ip + _ssrf_safe_fetch)
- Health score label logic
- Trust ladder graduation eligibility
"""

from __future__ import annotations

import ipaddress
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from opsforge.api.health_score import _label
from opsforge.api.skills import _is_private_ip
from opsforge.surfaces import slack


# ---------------------------------------------------------------------------
# Slack: timestamp replay-attack protection (E5)
# ---------------------------------------------------------------------------


class _FakeSettings:
    slack_signing_secret = "shhh"
    environment = "production"


def _make_signature(ts: str, body: bytes, secret: bytes = b"shhh") -> str:
    import hashlib
    import hmac as _hmac

    base = f"v0:{ts}:{body.decode()}".encode()
    return "v0=" + _hmac.new(secret, base, hashlib.sha256).hexdigest()


def test_verify_signature_rejects_old_timestamp(monkeypatch):
    """Timestamps > 5 minutes old must be rejected (replay attack)."""
    monkeypatch.setattr(slack, "get_settings", lambda: _FakeSettings())
    body = b'{"type":"url_verification"}'
    old_ts = str(int(time.time()) - 400)
    sig = _make_signature(old_ts, body)
    assert slack.verify_signature(old_ts, sig, body) is False


def test_verify_signature_rejects_future_timestamp(monkeypatch):
    """Timestamps > 5 minutes in the future are also rejected."""
    monkeypatch.setattr(slack, "get_settings", lambda: _FakeSettings())
    body = b'{"type":"url_verification"}'
    future_ts = str(int(time.time()) + 400)
    sig = _make_signature(future_ts, body)
    assert slack.verify_signature(future_ts, sig, body) is False


def test_verify_signature_rejects_non_numeric_timestamp(monkeypatch):
    """Non-numeric timestamp must not crash — returns False."""
    monkeypatch.setattr(slack, "get_settings", lambda: _FakeSettings())
    body = b'{"type":"url_verification"}'
    assert slack.verify_signature("notanumber", "v0=irrelevant", body) is False


def test_verify_signature_accepts_fresh_timestamp(monkeypatch):
    """A signature with a current timestamp must be accepted."""
    monkeypatch.setattr(slack, "get_settings", lambda: _FakeSettings())
    body = b'{"type":"url_verification"}'
    ts = str(int(time.time()))
    sig = _make_signature(ts, body)
    assert slack.verify_signature(ts, sig, body) is True


# ---------------------------------------------------------------------------
# SSRF guard: _is_private_ip (E4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.0.1",
        "192.168.1.1",
        "192.168.0.0",
        "127.0.0.1",
        "127.255.255.255",
        "169.254.1.1",  # link-local
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 link-local
    ],
)
def test_is_private_ip_blocks_rfc1918(addr):
    assert _is_private_ip(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",
        "1.1.1.1",
        "203.0.113.1",
        "2001:db8::1",
    ],
)
def test_is_private_ip_allows_public(addr):
    assert _is_private_ip(addr) is False


def test_is_private_ip_unparseable_fails_closed():
    assert _is_private_ip("not-an-ip") is True
    assert _is_private_ip("") is True
    assert _is_private_ip("999.999.999.999") is True


# ---------------------------------------------------------------------------
# SSRF guard: _ssrf_safe_fetch behaviour (E4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_blocks_private_ip():
    """DNS resolving to a private IP must raise HTTPException 422."""
    from fastapi import HTTPException

    from opsforge.api.skills import _ssrf_safe_fetch

    mock_loop = MagicMock()
    mock_loop.getaddrinfo = AsyncMock(
        return_value=[(None, None, None, None, ("192.168.1.1", 0))]
    )
    with patch("opsforge.api.skills.asyncio.get_running_loop", return_value=mock_loop):
        with pytest.raises(HTTPException) as exc_info:
            await _ssrf_safe_fetch("https://internal.example.com/runbook.md")
    assert exc_info.value.status_code == 422
    assert "private" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_rejects_http_scheme():
    """Plain http:// URLs must be rejected with 422 (HTTPS-only policy)."""
    from fastapi import HTTPException

    from opsforge.api.skills import _ssrf_safe_fetch

    with pytest.raises(HTTPException) as exc_info:
        await _ssrf_safe_fetch("http://example.com/runbook.md")
    assert exc_info.value.status_code == 422
    assert "https" in str(exc_info.value.detail).lower()


def _mock_ssrf_response(status_code: int, content_type: str, body: bytes):
    """Build a fake httpx.Response-like object for SSRF fetch mocking."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = httpx.Headers({"content-type": content_type})
    resp.content = body
    resp.is_redirect = (status_code in (301, 302, 303, 307, 308))
    return resp


def _async_iter_bytes(data: bytes, chunk_size: int = 8192):
    """Return an async generator that yields chunks of data."""
    async def _gen(chunk_size=chunk_size):
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]
    return _gen()


def _mock_ssrf_client(mock_resp):
    """Build a patched AsyncClient context manager returning mock_resp."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_blocks_redirect():
    """A 301 redirect response raises HTTPException 422."""
    from fastapi import HTTPException

    from opsforge.api.skills import _ssrf_safe_fetch

    mock_resp = _mock_ssrf_response(301, "text/plain", b"")
    mock_resp.aiter_bytes = lambda chunk_size=8192: _async_iter_bytes(b"")

    mock_loop = MagicMock()
    mock_loop.getaddrinfo = AsyncMock(
        return_value=[(None, None, None, None, ("93.184.216.34", 0))]
    )
    with patch("opsforge.api.skills.asyncio.get_running_loop", return_value=mock_loop):
        with patch("opsforge.api.skills._PinnedTransport"):
            with patch("opsforge.api.skills.httpx.AsyncClient", return_value=_mock_ssrf_client(mock_resp)):
                with pytest.raises(HTTPException) as exc_info:
                    await _ssrf_safe_fetch("https://example.com/runbook.md")
    assert exc_info.value.status_code == 422
    assert "redirect" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_blocks_wrong_content_type():
    """Non-text/* content type must be rejected with 422."""
    from fastapi import HTTPException

    from opsforge.api.skills import _ssrf_safe_fetch

    mock_resp = _mock_ssrf_response(200, "application/json", b'{"json": true}')
    mock_resp.is_redirect = False
    mock_resp.aiter_bytes = lambda chunk_size=8192: _async_iter_bytes(b'{"json": true}')

    mock_loop = MagicMock()
    mock_loop.getaddrinfo = AsyncMock(
        return_value=[(None, None, None, None, ("93.184.216.34", 0))]
    )
    with patch("opsforge.api.skills.asyncio.get_running_loop", return_value=mock_loop):
        with patch("opsforge.api.skills._PinnedTransport"):
            with patch("opsforge.api.skills.httpx.AsyncClient", return_value=_mock_ssrf_client(mock_resp)):
                with pytest.raises(HTTPException) as exc_info:
                    await _ssrf_safe_fetch("https://example.com/data.json")
    assert exc_info.value.status_code == 422
    assert "text/" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_blocks_oversized_body():
    """Bodies > 256 KB must be rejected with 422."""
    from fastapi import HTTPException

    from opsforge.api.skills import _ssrf_safe_fetch

    big_body = b"x" * (256 * 1024 + 1)
    mock_resp = _mock_ssrf_response(200, "text/plain; charset=utf-8", big_body)
    mock_resp.is_redirect = False
    mock_resp.aiter_bytes = lambda chunk_size=8192: _async_iter_bytes(big_body)

    mock_loop = MagicMock()
    mock_loop.getaddrinfo = AsyncMock(
        return_value=[(None, None, None, None, ("93.184.216.34", 0))]
    )
    with patch("opsforge.api.skills.asyncio.get_running_loop", return_value=mock_loop):
        with patch("opsforge.api.skills._PinnedTransport"):
            with patch("opsforge.api.skills.httpx.AsyncClient", return_value=_mock_ssrf_client(mock_resp)):
                with pytest.raises(HTTPException) as exc_info:
                    await _ssrf_safe_fetch("https://example.com/bigfile.txt")
    assert exc_info.value.status_code == 422
    assert "256" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Health score: label logic (E3)
# ---------------------------------------------------------------------------


def test_label_healthy():
    assert _label(0.8) == "healthy"
    assert _label(1.0) == "healthy"
    assert _label(0.71) == "healthy"


def test_label_degraded():
    assert _label(0.7) == "degraded"
    assert _label(0.5) == "degraded"
    assert _label(0.41) == "degraded"


def test_label_critical():
    assert _label(0.4) == "critical"
    assert _label(0.0) == "critical"
    assert _label(0.1) == "critical"


def test_label_insufficient_data():
    assert _label(None) == "insufficient_data"


# ---------------------------------------------------------------------------
# Trust ladder: graduation eligibility logic (E6)
# ---------------------------------------------------------------------------


def test_trust_ladder_eligibility_logic():
    """Verify the graduation condition: non-destructive + clean >= threshold + no rollbacks."""
    min_execs = 10

    def eligible(action_class: str, clean: int, rolled_back: int) -> bool:
        return action_class != "destructive" and clean >= min_execs and rolled_back == 0

    # Eligible: reversible tool with enough clean runs
    assert eligible("reversible", 10, 0) is True
    assert eligible("read_only", 15, 0) is True

    # Not eligible: destructive never graduates
    assert eligible("destructive", 100, 0) is False

    # Not eligible: insufficient clean runs
    assert eligible("reversible", 9, 0) is False

    # Not eligible: any rollback disqualifies
    assert eligible("reversible", 20, 1) is False
    assert eligible("reversible", 10, 1) is False

    # Edge: exactly at threshold, zero rollbacks
    assert eligible("reversible", 10, 0) is True
    assert eligible("reversible", 11, 0) is True
