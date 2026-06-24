"""LLM-provider operator surface (M7.6 Job A) — the proposed → scored → promoted state
machine for a workspace's LLM connector.

The LLM is a vault-credentialed connector, configured here, not in `.env`:
  1. POST   /llm/providers            propose a {provider, model, credential} binding
  2. GET    /llm/providers            list bindings (credential-free; residency-flagged)
  3. POST   /llm/providers/{id}/promote     promote to ACTIVE — REFUSED unless a MEASURED
                                      scorecard holds the baseline (the measured gate)

There is deliberately NO API endpoint to attach a scorecard. The scorecard is the gate's
proof-of-measurement, so it is written ONLY server-side by the eval runner / CI
(`score_provider` → `store_scorecard`), which runs the candidate against the M7.3 golden
set and computes `holds` from the real numbers. Letting an operator POST an arbitrary
`{"holds": true}` would degrade the measured gate to a self-asserted vibe — the exact thing
it exists to forbid — so that surface does not exist. The operator proposes and promotes;
only a measurement can make a binding promotable.

The credential never appears in a response (the operator view is credential-free) and the
heavy golden-set scoring runs in the eval runner / CI, not in the request path. Aggregator
providers are accepted but flagged non-residency; they are never a silent default
(promotion is always explicit, and only after a holding measurement).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..llm_providers import (
    ProviderOut,
    list_providers,
    promote_if_holds,
    propose_provider,
    provider_residency,
)
from ..security import Principal, require_token

router = APIRouter(prefix="/api/v1", tags=["llm-providers"])

_WRITER_ROLES = {"admin", "operator"}


def _require_writer(principal: Principal) -> None:
    if principal.role not in _WRITER_ROLES:
        raise HTTPException(status_code=403, detail="requires admin or operator")


class ProposeProviderBody(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    api_base: str | None = None


@router.post("/llm/providers", status_code=201)
async def propose(body: ProposeProviderBody, principal: Principal = Depends(require_token)):
    """Propose a (not-yet-trusted) provider binding. The credential is vaulted; the
    response carries the residency class so the operator sees an aggregator warning before
    they ever promote it."""
    _require_writer(principal)
    credential = {
        k: v for k, v in (("api_key", body.api_key), ("api_base", body.api_base)) if v
    }
    pid = await propose_provider(
        principal.org_id, provider=body.provider, model=body.model,
        credential=credential or None,
    )
    residency = provider_residency(body.provider)
    return {
        "id": str(pid),
        "provider": body.provider,
        "model": body.model,
        "status": "proposed",
        "residency": residency,
        # An explicit, machine-readable warning — never a silent aggregator default.
        "residency_warning": (
            None
            if residency == "enterprise"
            else "non-residency provider: prompts (and the operational data in them) route "
            "through a third party — dev/non-regulated use only"
        ),
    }


@router.get("/llm/providers", response_model=list[ProviderOut])
async def list_(principal: Principal = Depends(require_token)) -> list[ProviderOut]:
    """List the workspace's provider bindings — credential-free, residency-flagged."""
    return await list_providers(principal.org_id)


@router.post("/llm/providers/{provider_id}/promote")
async def promote(provider_id: UUID, principal: Principal = Depends(require_token)):
    """Promote a binding to ACTIVE — the measured gate. Refused (409) unless a server-side
    measurement (the eval runner's score_provider) has recorded a scorecard that holds the
    baseline; on success the prior active binding is demoted so at most one provider is ever
    active per workspace."""
    _require_writer(principal)
    promoted = await promote_if_holds(principal.org_id, provider_id)
    if not promoted:
        raise HTTPException(
            status_code=409,
            detail="provider not promoted: no measured scorecard, or it does not hold the "
            "baseline (run score_provider against the golden set first)",
        )
    return {"id": str(provider_id), "status": "active"}
