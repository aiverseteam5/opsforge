"""Phase B — the real Confluence REST client, validated against the real API CONTRACT
(no real endpoint needed): a contract-faithful fake via httpx.MockTransport exercises auth,
pagination, rate-limit, page-shape parsing → real provenance, and robustness. The SAME
client code runs against a customer's real Confluence — only the base URL + token change.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from opsforge.sources import confluence as cf

BASE = "https://acme.atlassian.net"
GOOD = "alice@acme.com:good-token"  # email:api_token → Basic


def _basic(token: str) -> str:
    return "Basic " + base64.b64encode(token.encode()).decode()


def _page(pid: str, title: str, body: str, updated: str, webui: str | None = None) -> dict:
    return {
        "id": pid, "title": title,
        "body": {"storage": {"value": body}},
        "version": {"createdAt": updated},
        "_links": {"webui": webui or f"/spaces/OPS/pages/{pid}"},
    }


def _handler(pages: list[dict], *, page_size: int = 50, rate_limit_once: bool = False):
    """A contract-faithful fake Confluence v2: Basic-auth gate, cursor pagination, and an
    optional one-shot 429. State is closed-over so we can assert backoff + paging."""
    state = {"hit_429": False}

    def handle(request: httpx.Request) -> httpx.Response:
        # AUTH: the real API rejects a bad/absent token with 401 — fail closed.
        if request.headers.get("Authorization") != _basic(GOOD):
            return httpx.Response(401, json={"message": "Unauthorized"})
        if rate_limit_once and not state["hit_429"]:
            state["hit_429"] = True
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        if request.url.path.endswith("/spaces"):
            return httpx.Response(200, json={"results": [{"id": "1", "key": "OPS"}]})
        # /pages with cursor pagination
        cursor = int(request.url.params.get("cursor", "0") or "0")
        chunk = pages[cursor:cursor + page_size]
        nxt = cursor + page_size
        links = {}
        if nxt < len(pages):
            links = {"next": f"/wiki/api/v2/pages?cursor={nxt}"}
        return httpx.Response(200, json={"results": chunk, "_links": links})

    return httpx.MockTransport(handle)


# --------------------------------------------------------------------------- #
# auth fails CLOSED (closes the A2/F2 boundary against the real contract)
# --------------------------------------------------------------------------- #
async def test_verify_credential_distinguishes_good_from_bad_token():
    t = _handler([])
    assert (await cf.verify_credential(BASE, GOOD, transport=t))["authenticated"] is True
    bad = await cf.verify_credential(BASE, "alice@acme.com:WRONG", transport=t)
    assert bad["authenticated"] is False and "rejected" in bad["error"]


async def test_fetch_raises_auth_error_on_bad_token():
    t = _handler([_page("1", "x", "hello", "2026-01-01T00:00:00Z")])
    with pytest.raises(cf.ConfluenceAuthError):
        await cf.fetch_documents(BASE, "bad:token", transport=t)


# --------------------------------------------------------------------------- #
# real provenance: source_ref = page URL, observed_at = the page's real modified date
# --------------------------------------------------------------------------- #
async def test_pages_normalize_with_real_provenance():
    pages = [_page("100", "Rollback Runbook",
                   "<p>To roll back, <strong>drain</strong> the node.</p>",
                   "2026-03-04T09:00:00Z", webui="/spaces/OPS/pages/100/Rollback")]
    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=_handler(pages))
    assert complete and len(docs) == 1
    d = docs[0]
    assert d.title == "Rollback Runbook"
    assert d.text == "To roll back, drain the node."          # HTML stripped to text
    assert d.url == f"{BASE}/wiki/spaces/OPS/pages/100/Rollback"  # the REAL page URL
    assert d.updated_at == "2026-03-04T09:00:00Z"              # the REAL last-modified, not now()


# --------------------------------------------------------------------------- #
# robustness: pagination, rate-limit backoff, empty/malformed skip, partial cap
# --------------------------------------------------------------------------- #
async def test_pagination_follows_cursor_across_pages():
    pages = [_page(str(i), f"p{i}", f"body {i}", "2026-01-01T00:00:00Z") for i in range(120)]
    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=_handler(pages, page_size=50))
    assert complete and len(docs) == 120  # 3 cursor pages stitched


async def test_rate_limit_is_retried_not_crashed():
    pages = [_page("1", "x", "hello", "2026-01-01T00:00:00Z")]
    docs, complete = await cf.fetch_documents(
        BASE, GOOD, transport=_handler(pages, rate_limit_once=True))
    assert complete and len(docs) == 1  # backed off and succeeded, not a crash


async def test_empty_and_nontext_pages_are_skipped_not_fatal():
    pages = [
        _page("1", "good", "<p>real content</p>", "2026-01-01T00:00:00Z"),
        _page("2", "blank", "<p></p>", "2026-01-01T00:00:00Z"),   # empty → skipped
        _page("3", "imageonly", "<ac:image/>", "2026-01-01T00:00:00Z"),  # no text → skipped
    ]
    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=_handler(pages))
    assert complete and [d.id for d in docs] == ["1"]  # the empties skipped, not crashed


async def test_cap_reports_partial_not_false_complete():
    pages = [_page(str(i), f"p{i}", f"body {i}", "2026-01-01T00:00:00Z") for i in range(10)]
    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=_handler(pages), max_pages=4)
    assert len(docs) == 4 and complete is False  # honest partial, NOT a silent false-complete


async def test_pagination_decodes_percent_encoded_cursor():
    """Real v2 cursors are opaque base64, percent-encoded in _links.next (=, +, /). The
    client must DECODE the captured cursor so httpx encodes it exactly once — double-encoding
    silently corrupts paging and drops every page after the first."""
    pages = [_page(str(i), f"p{i}", f"b{i}", "2026-01-01T00:00:00Z") for i in range(60)]
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != _basic(GOOD):
            return httpx.Response(401, json={})
        if request.url.path.endswith("/spaces"):
            return httpx.Response(200, json={"results": []})
        # the server echoes the cursor it RECEIVED so we can assert it was sent un-mangled
        raw = request.url.params.get("cursor")
        seen.append(raw or "")
        start = {"": 0, "tok==/a+b": 50}.get(raw or "", 0)
        chunk = pages[start:start + 50]
        links = {"next": "/wiki/api/v2/pages?cursor=tok%3D%3D%2Fa%2Bb"} if start == 0 else {}
        return httpx.Response(200, json={"results": chunk, "_links": links})

    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=httpx.MockTransport(handle))
    assert complete and len(docs) == 60                       # both pages stitched
    assert "tok==/a+b" in seen  # the server received the DECODED cursor, not tok%253D%253D...


async def test_midpull_rate_limit_giveup_is_partial_not_raise():
    """A persistent 429 (outlasting the retry budget) mid-pull must degrade to a PARTIAL
    (the pages gathered so far, complete=False) — not raise and lose all recoverable pages."""
    pages = [_page(str(i), f"p{i}", f"b{i}", "2026-01-01T00:00:00Z") for i in range(60)]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != _basic(GOOD):
            return httpx.Response(401, json={})
        if request.url.path.endswith("/spaces"):
            return httpx.Response(200, json={"results": []})
        if request.url.params.get("cursor"):  # page 2+ is permanently rate-limited
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={
            "results": pages[:50], "_links": {"next": "/wiki/api/v2/pages?cursor=2"}})

    docs, complete = await cf.fetch_documents(BASE, GOOD, transport=httpx.MockTransport(handle))
    assert len(docs) == 50 and complete is False  # page 1 kept, reported partial — not raised


# --------------------------------------------------------------------------- #
# the token never appears in a doc / error (credential safety on the real path)
# --------------------------------------------------------------------------- #
async def test_token_never_in_results_or_error():
    pages = [_page("1", "x", "hello", "2026-01-01T00:00:00Z")]
    docs, _ = await cf.fetch_documents(BASE, GOOD, transport=_handler(pages))
    assert GOOD not in str([d.__dict__ for d in docs])
    bad = await cf.verify_credential(BASE, "alice@acme.com:SECRET-TOKEN", transport=_handler([]))
    assert "SECRET-TOKEN" not in str(bad)  # the rejected token is not echoed in the error
