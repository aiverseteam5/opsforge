"""Real Confluence REST client (Phase B — first real knowledge source).

Generic and READ-ONLY: given a base URL + an API token, it talks the real Confluence Cloud
v2 REST API (`/wiki/api/v2/...`). The SAME code runs against a customer's real Confluence or
a contract-faithful fake — only the base URL + token change. Phase B validates this code
against the real API CONTRACT (auth, pagination, rate-limit, page shape); a customer points
it at their own instance when they have one.

Auth: a Confluence Cloud token is used as `email:api_token` (HTTP Basic); a Confluence
Server/DC Personal Access Token is used as a Bearer token. We detect which by the ':' —
one secret field, either deployment.

Fail CLOSED on credentials: a 401/403 raises ConfluenceAuthError (the connection test maps
it to `error`, never false-`connected` — closing the A2/F2 boundary against a real upstream).
The token is only ever sent in the Authorization header; it is never returned or logged.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx

_API = "/wiki/api/v2"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
# Defensive cap so a pathologically huge page can't blow up the chunker/embedder.
_MAX_DOC_CHARS = 200_000
_MAX_RETRIES = 3


class ConfluenceError(Exception):
    """A Confluence call failed in a way the operator should see (honest error)."""


class ConfluenceAuthError(ConfluenceError):
    """The credential was rejected (401/403) — reachable but bad/expired token."""


@dataclass(frozen=True)
class ConfluenceDoc:
    """A page normalized for the ingest pipeline, carrying REAL provenance."""

    id: str
    title: str
    text: str
    url: str            # the real page URL → source_ref
    updated_at: str     # the page's real last-modified (version.createdAt) → observed_at


def _auth_header(token: str) -> dict[str, str]:
    """Confluence Cloud (email:api_token → Basic) or Server/DC PAT (→ Bearer)."""
    token = token.strip()
    if ":" in token:  # email:api_token → Basic
        return {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
    return {"Authorization": f"Bearer {token}"}


def _strip_html(storage: str) -> str:
    """Confluence 'storage' bodies are XHTML — reduce to readable text for ingest."""
    text = _TAG_RE.sub(" ", storage or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return _WS_RE.sub(" ", text).strip()[:_MAX_DOC_CHARS]


async def _get(client: httpx.AsyncClient, base: str, token: str, path: str,
               params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One GET with auth, honest auth-failure mapping, and bounded 429 backoff."""
    url = f"{base.rstrip('/')}{path}"
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.get(url, params=params, headers=_auth_header(token))
        if resp.status_code in (401, 403):
            # never include the token or the body verbatim — just the honest reason
            raise ConfluenceAuthError(f"credential rejected ({resp.status_code})")
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            # respect Retry-After (real systems rate-limit); back off, do not crash
            delay = float(resp.headers.get("Retry-After", "1") or "1")
            await asyncio.sleep(min(delay, 10.0))
            continue
        if resp.status_code >= 400:
            raise ConfluenceError(f"confluence {resp.status_code} for {path}")
        return resp.json()
    raise ConfluenceError("confluence rate-limited; gave up after retries")


async def verify_credential(
    base: str, token: str, *, timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Exercise the credential against the real API (closes A2/F2): a reachable endpoint
    with a WRONG/expired token returns {authenticated: False}, a valid one True. Never
    raises on auth — returns the verdict so the connection test can flip status honestly.
    (`transport` is for contract tests — None uses the real network.)"""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        try:
            await _get(client, base, token, f"{_API}/spaces", {"limit": 1})
            return {"authenticated": True}
        except ConfluenceError as exc:
            return {"authenticated": False, "error": str(exc)}
        except httpx.HTTPError as exc:
            # a network/timeout failure is NOT authenticated — fail closed, never raise our
            # way to a misread. The reason is the exception type, never the token.
            return {"authenticated": False, "error": f"unreachable: {type(exc).__name__}"}


def _normalize(page: dict[str, Any], base: str) -> ConfluenceDoc | None:
    """A v2 page → ConfluenceDoc, or None if it carries no usable text (skip honestly)."""
    body = (((page.get("body") or {}).get("storage") or {}).get("value")) or ""
    text = _strip_html(body)
    if not text:
        return None  # empty / non-text page → skipped, not a silent failure
    webui = ((page.get("_links") or {}).get("webui")) or f"/pages/{page.get('id')}"
    updated = ((page.get("version") or {}).get("createdAt")) or page.get("createdAt") or ""
    return ConfluenceDoc(
        id=str(page.get("id", "")),
        title=str(page.get("title", "") or "untitled"),
        text=text,
        url=f"{base.rstrip('/')}/wiki{webui}",
        updated_at=str(updated),
    )


async def fetch_documents(
    base: str, token: str, *, space_id: str | None = None, max_pages: int = 200,
    timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[ConfluenceDoc], bool]:
    """Pull pages (paginated) and normalize them. Returns (docs, complete) — `complete` is
    False when the pull stopped early (cap hit) or a partial failure occurred mid-pull, so
    the caller can report a PARTIAL ingest honestly rather than as a false-complete.

    Auth failure propagates (the whole pull fails closed). A single malformed/empty page is
    skipped, not fatal."""
    docs: list[ConfluenceDoc] = []
    complete = True
    params: dict[str, Any] = {"limit": 50, "body-format": "storage"}
    if space_id:
        params["space-id"] = space_id
    # Bound the number of REQUESTS (not just docs) and require the cursor to advance, so a
    # stuck/repeating cursor or an all-empty stream can never loop forever.
    max_requests = max(8, max_pages // 25 + 4)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        cursor: str | None = None
        for _req in range(max_requests):
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            try:
                # ConfluenceAuthError (bad/expired token) MUST propagate → the whole pull
                # fails closed. A transport-level mid-pull failure (rate-limit give-up, 5xx)
                # degrades to a PARTIAL: keep the pages gathered so far, report complete=False.
                payload = await _get(client, base, token, f"{_API}/pages", q)
            except ConfluenceAuthError:
                raise
            except ConfluenceError:
                return docs, False
            for page in payload.get("results", []) or []:
                try:
                    doc = _normalize(page, base)
                except Exception:  # noqa: BLE001 - one bad page must not abort the whole pull
                    complete = False
                    continue
                if doc is not None:
                    docs.append(doc)
                if len(docs) >= max_pages:
                    return docs, False  # cap hit → partial, reported as such
            nxt = ((payload.get("_links") or {}).get("next")) or ""
            m = re.search(r"cursor=([^&]+)", nxt)
            if not m:
                return docs, complete  # last page → done
            # The cursor in _links.next is percent-ENCODED; decode it so httpx encodes it
            # exactly once (double-encoding silently corrupts real Confluence paging).
            nxt_cursor = unquote(m.group(1))
            if nxt_cursor == cursor:  # cursor not advancing → stop, report partial
                return docs, False
            cursor = nxt_cursor
        return docs, False  # request bound hit → partial, never a false-complete
