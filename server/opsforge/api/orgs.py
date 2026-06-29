"""Org hierarchy API — ancestor chain read and write.

The org_ancestors table pre-materializes ancestor chains for the multi-org
control plane. Phase 5b migration 0030 enables FORCE RLS on this table with
an ancestor-chain isolation policy; these endpoints are the first callers.

Only admin principals may write ancestor relationships. Any authenticated
principal may read the ancestor chain for their own org.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from ..db import record_audit, scope_to_org, session_factory
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


class AncestorRelationship(BaseModel):
    ancestor_id: UUID


@router.get("/{org_id}/ancestors")
async def list_ancestors(org_id: UUID, principal: Principal = Depends(require_token)):
    """Return the ancestor chain for an org (direct + transitive).

    The caller may only query ancestors for their own org.
    """
    if str(org_id) != principal.org_id:
        raise HTTPException(
            status_code=403, detail="can only query ancestors for your own org"
        )
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT ancestor_id FROM org_ancestors "
                    "WHERE org_id = :org ORDER BY ancestor_id"
                ),
                {"org": principal.org_id},
            )
        ).all()
    return {"org_id": str(org_id), "ancestors": [str(r.ancestor_id) for r in rows]}


@router.post("/{org_id}/ancestors", status_code=201)
async def add_ancestor(
    org_id: UUID,
    body: AncestorRelationship,
    principal: Principal = Depends(require_token),
):
    """Declare that ancestor_id is an ancestor of org_id.

    Admin only. The org_id must match the caller's org (enforced by RLS
    WITH CHECK as a second line of defence).
    """
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="requires admin role")
    if str(org_id) != principal.org_id:
        raise HTTPException(
            status_code=403, detail="can only add ancestors for your own org"
        )
    if str(body.ancestor_id) == principal.org_id:
        raise HTTPException(status_code=400, detail="an org cannot be its own ancestor")

    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        await s.execute(
            text(
                "INSERT INTO org_ancestors (org_id, ancestor_id) "
                "VALUES (:org, :anc) ON CONFLICT DO NOTHING"
            ),
            {"org": str(org_id), "anc": str(body.ancestor_id)},
        )

    await record_audit(
        principal.org_id,
        principal.user_id or "system",
        "org.ancestor_added",
        subject_ref=str(org_id),
        detail={"ancestor_id": str(body.ancestor_id)},
    )
    return {"org_id": str(org_id), "ancestor_id": str(body.ancestor_id)}
