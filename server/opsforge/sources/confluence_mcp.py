"""Real Confluence MCP server — the shippable connector a customer points at THEIR
Confluence. It wraps the REST client (sources/confluence.py) so the existing connector model
can spawn it over stdio; credentials are injected into its env at spawn from the Fernet vault
(never `.env`, never logged). Read-only: it lists documents and verifies the credential.

Run: `python -m opsforge.sources.confluence_mcp` (this is the connector `endpoint`). The vault
credential supplies CONFLUENCE_BASE_URL + CONFLUENCE_TOKEN (a Cloud `email:api_token` or a
Server/DC PAT).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from opsforge.sources import confluence as cf

mcp = FastMCP("confluence")


def _cfg() -> tuple[str, str]:
    return os.environ.get("CONFLUENCE_BASE_URL", ""), os.environ.get("CONFLUENCE_TOKEN", "")


@mcp.tool()
async def verify_credential() -> dict:
    """Exercise the credential against the real API → {authenticated: bool}. The connection
    test reads this so a wrong/expired token flips the connector to `error`, not false-green."""
    base, token = _cfg()
    if not base or not token:
        return {"authenticated": False, "error": "missing base url or token"}
    return await cf.verify_credential(base, token)


@mcp.tool()
async def list_documents(space_id: str = "") -> dict:
    """Pull (read-only) the space's pages as normalized documents with REAL provenance
    (url + last-modified). `complete` is False on a partial pull (cap/partial failure)."""
    base, token = _cfg()
    space = space_id or os.environ.get("CONFLUENCE_SPACE", "")
    docs, complete = await cf.fetch_documents(base, token, space_id=space or None)
    return {
        "documents": [
            {"id": d.id, "title": d.title, "text": d.text, "url": d.url,
             "updated_at": d.updated_at}
            for d in docs
        ],
        "complete": complete,
    }


if __name__ == "__main__":
    mcp.run()
