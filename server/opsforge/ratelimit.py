"""Sliding-window rate limiters for webhook and run-dispatch endpoints.

In-memory, per-key (IP or token), thread-safe. Correct for single-API-process
deployment. At multi-replica scale, move to an Nginx ingress rule or a Postgres
counter — the FastAPI dependency interface stays the same.

Stale buckets are pruned on every check so memory stays bounded even under
sustained load from many distinct IPs.
"""

from __future__ import annotations

import threading
from collections import deque
from time import monotonic

from fastapi import Depends, HTTPException, Request, status

from .config import get_settings
from .security import Principal, require_token


class SlidingWindowLimiter:
    """Thread-safe sliding-window counter keyed by arbitrary string (e.g. IP)."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = float(window_seconds)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            self._prune(cutoff)
            return True

    def _prune(self, cutoff: float) -> None:
        """Remove buckets that have been empty since the cutoff."""
        dead = [k for k, v in self._buckets.items() if not v or v[-1] < cutoff]
        for k in dead:
            del self._buckets[k]

    def reset(self, key: str) -> None:
        """Clear the bucket for a key. Used in tests."""
        with self._lock:
            self._buckets.pop(key, None)


# Module-level singleton — shared across all requests in the process.
_limiter: SlidingWindowLimiter | None = None


def _get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = SlidingWindowLimiter(
            max_requests=s.webhook_rate_limit_requests,
            window_seconds=s.webhook_rate_limit_window_s,
        )
    return _limiter


def _client_ip(request: Request) -> str:
    """Extract the client IP for rate-limit keying.

    X-Forwarded-For is only trusted when OPSFORGE_TRUSTED_PROXY=true — i.e.
    when OpsForge sits behind a proxy that sets the header reliably. In the
    default (False) case the header is ignored: a caller cannot spoof XFF to
    rotate their apparent IP and bypass per-IP rate limits.
    """
    if get_settings().trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Take the rightmost entry — the IP the proxy directly observed.
            # Standard proxies (nginx proxy_add_x_forwarded_for, ALB) append, so
            # the leftmost is client-controlled. Rightmost is proxy-certified.
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def webhook_rate_limit(request: Request) -> None:
    """FastAPI dependency: raises 429 if the caller exceeds the webhook rate limit."""
    ip = _client_ip(request)
    if not _get_limiter().is_allowed(ip):
        settings = get_settings()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"rate limit exceeded: "
                f"max {settings.webhook_rate_limit_requests} requests "
                f"per {settings.webhook_rate_limit_window_s}s"
            ),
            headers={"Retry-After": str(settings.webhook_rate_limit_window_s)},
        )


# --------------------------------------------------------------------------- #
# Per-token run dispatch limiter (F6: unbounded LLM cost amplification)
# --------------------------------------------------------------------------- #
_dispatch_limiter: SlidingWindowLimiter | None = None


def _get_dispatch_limiter() -> SlidingWindowLimiter:
    global _dispatch_limiter
    if _dispatch_limiter is None:
        s = get_settings()
        _dispatch_limiter = SlidingWindowLimiter(
            max_requests=s.run_dispatch_rate_limit_requests,
            window_seconds=s.run_dispatch_rate_limit_window_s,
        )
    return _dispatch_limiter


def run_dispatch_rate_limit(principal: Principal = Depends(require_token)) -> None:
    """FastAPI dependency: raises 429 if the token exceeds the run dispatch rate limit.

    Keyed by token id (not IP) so distributed clients behind NAT each get their
    own quota — an attacker cannot amplify by parallelising from one network.
    """
    key = str(principal.token_id) if principal.token_id else principal.org_id
    if not _get_dispatch_limiter().is_allowed(key):
        settings = get_settings()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"run dispatch rate limit exceeded: "
                f"max {settings.run_dispatch_rate_limit_requests} requests "
                f"per {settings.run_dispatch_rate_limit_window_s}s"
            ),
            headers={"Retry-After": str(settings.run_dispatch_rate_limit_window_s)},
        )
