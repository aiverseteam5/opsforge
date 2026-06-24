"""Auth, the Fernet credential vault, and the single redaction chokepoint.

Doctrine #8: every secret is encrypted at rest; plaintext credentials must never
reach logs, run_events, or LLM context. `redact()` is the one function every
boundary calls before persisting or logging external data.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session

# --------------------------------------------------------------------------- #
# Fernet vault
# --------------------------------------------------------------------------- #


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("OPSFORGE_FERNET_KEY is not set")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> bytes:
    """Encrypt a credential into a Fernet envelope for `credentials_enc`."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    """Decrypt a Fernet envelope. Called only at MCP spawn time."""
    return _fernet().decrypt(token).decode()


# --------------------------------------------------------------------------- #
# API token hashing
# --------------------------------------------------------------------------- #


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token() -> tuple[str, str]:
    """Return (raw_token_to_show_once, hash_to_store)."""
    raw = "ofg_" + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 check for inbound webhooks.

    The signature header is `sha256=<hexdigest>` over the raw body using the
    configured webhook secret. If no secret is configured (dev), verification is
    skipped (returns True) so local testing isn't blocked.
    """
    secret = get_settings().webhook_secret
    if not secret:
        return True
    if not signature:
        return False
    provided = signature.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


# --------------------------------------------------------------------------- #
# Redaction (the single chokepoint)
# --------------------------------------------------------------------------- #

_SECRET_KEY_RE = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|credential|"
    r"private[_-]?key|access[_-]?key|bearer|fernet)",
    re.IGNORECASE,
)
# A Fernet token: urlsafe-base64, starts with the version byte 'gAAAAA'.
_FERNET_RE = re.compile(r"gAAAAA[0-9A-Za-z_\-=]{20,}")
# Inline `secret = value` / `token: value` assignments embedded in free text
# (e.g. a log line). The value (group 3) is masked; the label is kept.
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"authorization|credential|bearer)\b(\s*[=:]\s*)"
    r"((?:bearer|basic|token|jwt)\s+)?(\S+)"  # optional auth scheme + the secret
)
_REDACTED = "***REDACTED***"


def redact(value: Any) -> Any:
    """Recursively mask secret-like content. Idempotent and pure.

    - dict: any key matching a secret pattern has its value replaced wholesale;
      other values are recursed into.
    - list/tuple: each element recursed.
    - str: any embedded Fernet token substring is masked.
    Anything else is returned unchanged.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        masked = _FERNET_RE.sub(_REDACTED, value)
        masked = _INLINE_SECRET_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", masked
        )
        return masked
    return value


# --------------------------------------------------------------------------- #
# Bearer-token auth dependency
# --------------------------------------------------------------------------- #


class Principal:
    """The authenticated caller resolved from an API token."""

    def __init__(self, user_id: str | None, org_id: str, role: str | None):
        self.user_id = user_id
        self.org_id = org_id
        self.role = role


_LOOKUP_TOKEN_SQL = text(
    """
    SELECT t.user_id, t.org_id, u.role
    FROM api_tokens t
    LEFT JOIN users u ON u.id = t.user_id
    WHERE t.token_hash = :token_hash
    """
)
_TOUCH_TOKEN_SQL = text(
    "UPDATE api_tokens SET last_used_at = now() WHERE token_hash = :token_hash"
)


async def require_token(
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    token_hash = hash_token(authorization.removeprefix("Bearer ").strip())
    row = (
        await session.execute(_LOOKUP_TOKEN_SQL, {"token_hash": token_hash})
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )
    # Best-effort touch; never block auth on it.
    await session.execute(_TOUCH_TOKEN_SQL, {"token_hash": token_hash})
    await session.commit()
    return Principal(
        user_id=str(row.user_id) if row.user_id else None,
        org_id=str(row.org_id),
        role=row.role,
    )
