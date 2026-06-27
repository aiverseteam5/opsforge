"""Unit tests for the sliding-window webhook rate limiter."""

from __future__ import annotations

from time import monotonic
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from opsforge.ratelimit import (
    SlidingWindowLimiter,
    _client_ip,
    webhook_rate_limit,
)

# --------------------------------------------------------------------------- #
# SlidingWindowLimiter
# --------------------------------------------------------------------------- #


def test_allows_requests_within_limit() -> None:
    limiter = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    assert limiter.is_allowed("ip1") is True
    assert limiter.is_allowed("ip1") is True
    assert limiter.is_allowed("ip1") is True


def test_blocks_on_limit_exceeded() -> None:
    limiter = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.is_allowed("ip1")
    assert limiter.is_allowed("ip1") is False


def test_different_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    limiter.is_allowed("ip1")
    limiter.is_allowed("ip1")
    assert limiter.is_allowed("ip1") is False
    assert limiter.is_allowed("ip2") is True


def test_window_expiry_allows_new_requests() -> None:
    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=1)
    limiter.is_allowed("ip1")
    limiter.is_allowed("ip1")
    assert limiter.is_allowed("ip1") is False

    # Simulate time passing beyond the window by manipulating the bucket directly.
    with limiter._lock:
        bucket = limiter._buckets["ip1"]
        old_time = monotonic() - 2  # 2 seconds ago, past the 1s window
        bucket.clear()
        bucket.append(old_time)
        bucket.append(old_time)

    # Now the two old timestamps should be pruned, allowing a new request.
    assert limiter.is_allowed("ip1") is True


def test_reset_clears_bucket() -> None:
    limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    limiter.is_allowed("ip1")
    assert limiter.is_allowed("ip1") is False
    limiter.reset("ip1")
    assert limiter.is_allowed("ip1") is True


# --------------------------------------------------------------------------- #
# _client_ip
# --------------------------------------------------------------------------- #


def _mock_request(remote: str, forwarded: str | None = None) -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = remote
    headers: dict[str, str] = {}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    req.headers = headers
    return req


def test_client_ip_uses_direct_connection() -> None:
    req = _mock_request("1.2.3.4")
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = False
        assert _client_ip(req) == "1.2.3.4"


def test_client_ip_respects_forwarded_for_when_trusted() -> None:
    # Chain: "client-spoofed, proxy-observed" — rightmost is proxy-certified.
    req = _mock_request("10.0.0.1", forwarded="203.0.113.5, 10.0.0.1")
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = True
        assert _client_ip(req) == "10.0.0.1"


def test_client_ip_trusted_proxy_no_xff_falls_back_to_direct() -> None:
    req = _mock_request("1.2.3.4")  # no XFF header
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = True
        assert _client_ip(req) == "1.2.3.4"


def test_client_ip_ignores_forwarded_for_when_untrusted() -> None:
    req = _mock_request("10.0.0.1", forwarded="203.0.113.5, 10.0.0.1")
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = False
        assert _client_ip(req) == "10.0.0.1"


def test_client_ip_no_client() -> None:
    req = MagicMock()
    req.client = None
    req.headers = {}
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = False
        assert _client_ip(req) == "unknown"


def test_client_ip_no_client_trusted_proxy() -> None:
    req = MagicMock()
    req.client = None
    req.headers = {}
    with patch("opsforge.ratelimit.get_settings") as mock_settings:
        mock_settings.return_value.trusted_proxy = True
        assert _client_ip(req) == "unknown"


# --------------------------------------------------------------------------- #
# webhook_rate_limit dependency
# --------------------------------------------------------------------------- #


def test_dependency_allows_under_limit() -> None:
    req = _mock_request("5.5.5.5")
    limiter = SlidingWindowLimiter(max_requests=10, window_seconds=60)
    with patch("opsforge.ratelimit._get_limiter", return_value=limiter):
        webhook_rate_limit(req)  # should not raise


def test_dependency_raises_429_on_limit() -> None:
    req = _mock_request("6.6.6.6")
    limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    limiter.is_allowed("6.6.6.6")  # exhaust the limit
    with patch("opsforge.ratelimit._get_limiter", return_value=limiter):
        with pytest.raises(HTTPException) as exc_info:
            webhook_rate_limit(req)
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
