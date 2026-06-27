"""POST /orgs/{org_id}/delegation-tokens — mint A2A delegation JWTs (admin only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session, scope_to_org
from ..delegation import _MAX_EXP_SECONDS, mint_delegation_token
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1", tags=["delegation"])


class MintRequest(BaseModel):
    run_id: str = Field(..., description="UUID of the issuing run")
    sub_run_id: str = Field(..., description="UUID of the delegated (sub) run")
    scope: list[str] = Field(..., min_length=1, description="Allowed tool FQNs")
    exp_seconds: int = Field(default=900, ge=1, le=_MAX_EXP_SECONDS)


class MintResponse(BaseModel):
    token: str
    jti: str
    expires_at: str


@router.post("/orgs/{org_id}/delegation-tokens", response_model=MintResponse)
async def mint_delegation(
    org_id: str,
    body: MintRequest,
    principal: Principal = Depends(require_token),
    session: AsyncSession = Depends(get_session),
) -> MintResponse:
    if principal.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="org mismatch")
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin role required"
        )

    token, jti = mint_delegation_token(
        run_id=body.run_id,
        sub_run_id=body.sub_run_id,
        org_id=org_id,
        scope=body.scope,
        exp_seconds=body.exp_seconds,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=body.exp_seconds)

    await scope_to_org(session, org_id)
    await session.execute(
        text(
            "INSERT INTO delegation_tokens "
            "(jti, org_id, iss, sub, scope, expires_at) "
            "VALUES (:jti, :org_id, :iss, :sub, CAST(:scope AS json), :expires_at)"
        ),
        {
            "jti": jti,
            "org_id": org_id,
            "iss": body.run_id,
            "sub": body.sub_run_id,
            "scope": json.dumps(body.scope),
            "expires_at": expires_at,
        },
    )
    await session.commit()

    return MintResponse(token=token, jti=jti, expires_at=expires_at.isoformat())
