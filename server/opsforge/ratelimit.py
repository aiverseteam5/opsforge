"""Sliding-window rate limiter for webhook endpoints.

In-memory, per-IP, thread-safe. Correct for the current single-API-process
deployment model. At multi-replica scale, move this to an Nginx ingress rule
or a Postgres counter — the FastAPI dependency interface stays the same.

Stale buckets are pruned on every check so memory stays bounded even under
sustained load from many distinct IPs.
"""

from __future__ import annotations

import threading
from collections import deque
from time import monotonic

from fastapi import HTTPException, Request, status

from .config import get_settings


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
    """Extract the real client IP, respecting X-Forwarded-For from a trusted proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
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
