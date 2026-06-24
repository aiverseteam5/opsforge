"""Connector-catalog API (A1) — READ-ONLY over the registry + this workspace's status.

Two endpoints, both workspace-scoped by the token principal (token = workspace), so a
caller can never see another workspace's configured/connected status. A1 captures no
credentials and writes no connector instances — that is A2. The "connect" affordance in
the UI merely navigates toward the (A2) config flow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..catalog import catalog_by_zone, catalog_detail
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1", tags=["catalog"])


@router.get("/catalog")
async def get_catalog(principal: Principal = Depends(require_token)):
    """The connector registry grouped by zone, each entry with its capability fields and
    THIS workspace's honest status (available | configured | connected | error |
    coming_soon). Always populated — never an empty state on load."""
    return {"zones": await catalog_by_zone(principal.org_id)}


@router.get("/catalog/{key}")
async def get_catalog_entry(key: str, principal: Principal = Depends(require_token)):
    """One connector's detail + the config requirements A2 will need (read-only)."""
    detail = await catalog_detail(principal.org_id, key)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown connector")
    return detail
