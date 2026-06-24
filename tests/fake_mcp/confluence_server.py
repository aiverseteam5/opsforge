"""Fake Confluence MCP server (stdio) — drives the knowledge-connector pipeline + F2 tests.

Mirrors the REAL confluence_mcp tool interface (verify_credential, list_documents) so the
ingest→reconcile path is exercised with Confluence-shaped data WITHOUT a live endpoint:
  * verify_credential reflects the env token (a wrong token → authenticated False), so the
    connection-test (F2) probe is exercised at the connector level.
  * list_documents returns canned pages with REAL-shaped provenance (url + updated_at).

The two `deploy-rollback` pages deliberately CONTRADICT each other (drain+redeploy-image vs
restore-from-backup) — a FIXTURE inconsistency to demonstrate the connector→ingest→reconcile
MECHANISM. It is NOT the Phase-B "real aha" (that needs a real corpus); it is labelled here so
no one mistakes a planted fixture for a real finding.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-confluence")

_GOOD_TOKEN = "alice@acme.com:good-token"
_BASE = "https://acme.atlassian.net"

# Canned pages. process_key groups them for reconciliation (the real server derives this from
# a page label / connector default — see the mapping note in knowledge_sources.py).
_PAGES: list[dict] = [
    {"id": "101", "title": "Deploy Rollback Runbook", "process_key": "deploy-rollback",
     "text": "To roll back a deploy: drain the node, redeploy the prior image, and verify health.",
     "url": f"{_BASE}/wiki/spaces/OPS/pages/101/Deploy-Rollback-Runbook",
     "updated_at": "2026-01-10T09:00:00Z"},
    {"id": "102", "title": "Incident Recovery Notes", "process_key": "deploy-rollback",
     "text": "Rollback procedure: restore the database from last night's backup and restart the service.",  # noqa: E501
     "url": f"{_BASE}/wiki/spaces/OPS/pages/102/Incident-Recovery-Notes",
     "updated_at": "2026-05-20T14:00:00Z"},
    {"id": "103", "title": "Cache Flush", "process_key": "cache-flush",
     "text": "Flush the cache by running the cache-clear job from the ops console.",
     "url": f"{_BASE}/wiki/spaces/OPS/pages/103/Cache-Flush",
     "updated_at": "2026-04-01T10:00:00Z"},
]


@mcp.tool()
def verify_credential() -> dict:
    token = os.environ.get("CONFLUENCE_TOKEN", "")
    if token == _GOOD_TOKEN:
        return {"authenticated": True}
    return {"authenticated": False, "error": "credential rejected (401)"}


@mcp.tool()
def list_documents(space_id: str = "") -> dict:
    # A wrong token fails CLOSED here too (no documents leak without a valid credential).
    if os.environ.get("CONFLUENCE_TOKEN", "") != _GOOD_TOKEN:
        return {"documents": [], "complete": False, "error": "credential rejected (401)"}
    pages = list(_PAGES)
    if os.environ.get("CONFLUENCE_DATELESS"):
        # a real page with content but NO last-modified — ingest must SKIP it (never fabricate
        # observed_at=now()), not store it as falsely fresh.
        pages.append({"id": "199", "title": "Undated", "process_key": "cache-flush",
                      "text": "An undated page with real content.",
                      "url": f"{_BASE}/wiki/spaces/OPS/pages/199/Undated", "updated_at": ""})
    return {"documents": pages, "complete": True}


if __name__ == "__main__":
    mcp.run()
