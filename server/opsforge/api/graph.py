"""Graph API: neighborhood query for a node (topology + reachable context)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..graph import neighborhood
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


@router.get("/neighborhood")
async def get_neighborhood(
    key: str = Query(..., description="natural_key of the root node"),
    hops: int = Query(2, ge=1, le=4),
    principal: Principal = Depends(require_token),
) -> dict[str, Any]:
    return await neighborhood(key, hops)
