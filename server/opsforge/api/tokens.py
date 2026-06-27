"""Token management API: list, create, and revoke API tokens. Admin-only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from ..db import record_audit, session_factory
from ..security import Principal, generate_token, require_token

router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"])


def _require_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="requires admin")


class CreateTokenBody(BaseModel):
    name: str | None = None
    expires_at: datetime | None = None


@router.get("")
async def list_tokens(
    principal: Principal = Depends(require_token),
) -> list[dict[str, Any]]:
    _require_admin(principal)
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, name, last_used_at, expires_at, created_at "
                    "FROM api_tokens WHERE org_id = :org ORDER BY created_at DESC"
                ),
                {"org": principal.org_id},
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_token(
    body: CreateTokenBody = Body(default_factory=CreateTokenBody),
    principal: Principal = Depends(require_token),
) -> dict[str, Any]:
    _require_admin(principal)
    raw, token_hash = generate_token()
    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text(
                    "INSERT INTO api_tokens (org_id, token_hash, name, expires_at) "
                    "VALUES (:org, :hash, :name, :expires_at) "
                    "RETURNING id, created_at"
                ),
                {
                    "org": principal.org_id,
                    "hash": token_hash,
                    "name": body.name,
                    "expires_at": body.expires_at,
                },
            )
        ).first()
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id, actor, "token.created",
        subject_ref=str(row.id), detail={"name": body.name},
    )
    return {
        "id": str(row.id),
        "name": body.name,
        "token": raw,  # shown once — never stored in plaintext, not retrievable
        "expires_at": body.expires_at.isoformat() if body.expires_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.delete("/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    principal: Principal = Depends(require_token),
) -> None:
    _require_admin(principal)
    async with session_factory().begin() as s:
        result = await s.execute(
            text(
                "DELETE FROM api_tokens WHERE id = :id AND org_id = :org RETURNING id"
            ),
            {"id": token_id, "org": principal.org_id},
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id, actor, "token.revoked", subject_ref=token_id
    )
