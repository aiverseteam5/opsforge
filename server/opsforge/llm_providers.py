"""Per-workspace LLM provider bindings (M7.6 Job A).

The LLM is a vault-credentialed connector: each workspace binds {provider, model,
credential}. The credential lives in the Fernet vault (credential_enc), decrypted only
here at call time, never in `.env` for production. A binding is `proposed`, SCORED
against the M7.3 golden sets, and only `promote`d to `active` if it holds the baseline —
provider choice is a measured decision, not a vibe. At most one `active` per workspace;
no active binding → the keyless lexical floor (NOT a shared global key), so LLM
isolation holds per workspace.

Enterprise-credible providers (own-tenant OpenAI, Azure OpenAI, Anthropic, Bedrock,
self-hosted vLLM/Ollama) are first-class — direct relationships, attestable residency.
Aggregators (OpenRouter, …) route the prompt — and the operational data in it — through
a third party, a data-residency problem for the regulated buyer; they are SELECTABLE but
flagged non-residency and must never be a workspace's silent default.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .db import scope_to_org, session_factory
from .security import decrypt, encrypt

# Residency-clean, enterprise-credible providers (direct relationship / your infra).
ENTERPRISE_PROVIDERS = frozenset({
    "openai", "azure", "anthropic", "bedrock", "vertex_ai", "vllm", "ollama",
    "openai_compatible",
})
# Aggregators route prompts through a third party → a data-residency problem; dev only.
AGGREGATOR_PROVIDERS = frozenset({"openrouter", "together_ai", "fireworks_ai", "groq"})


def provider_residency(provider: str) -> str:
    """'enterprise' (residency-clean, first-class), 'aggregator' (routes data through a
    third party — dev / non-residency), or 'unknown' (treated as aggregator: fail safe)."""
    p = (provider or "").strip().lower()
    if p in ENTERPRISE_PROVIDERS:
        return "enterprise"
    if p in AGGREGATOR_PROVIDERS:
        return "aggregator"
    return "unknown"


def is_residency_clean(provider: str) -> bool:
    return provider_residency(provider) == "enterprise"


class ProviderConfig(BaseModel):
    """The resolved binding the gateway needs — with the decrypted credential. Never
    serialized to an API response or a log (redact covers it at every boundary)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    provider: str
    model: str
    api_key: str | None = None
    api_base: str | None = None


class ProviderOut(BaseModel):
    """The safe (credential-free) view for the operator surface."""

    id: UUID
    provider: str
    model: str
    status: str
    residency: str
    scorecard: dict[str, Any] | None = None


def _decrypt_credential(blob: bytes | None) -> dict[str, str]:
    if not blob:
        return {}
    try:
        data = json.loads(decrypt(blob))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a bad/empty credential resolves to no-credential
        return {}


async def propose_provider(
    org_id: Any, *, provider: str, model: str, credential: dict[str, str] | None = None
) -> UUID:
    """Record a PROPOSED provider binding (not yet trusted). The credential (api_key /
    api_base) is encrypted into the vault; provider+model are not secret."""
    blob = encrypt(json.dumps(credential)) if credential else None
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(
                    "INSERT INTO llm_providers (org_id, provider, model, credential_enc, status) "
                    "VALUES (:org, :provider, :model, :cred, 'proposed') RETURNING id"
                ),
                {"org": str(org_id), "provider": provider, "model": model, "cred": blob},
            )
        ).one()
    return row.id


async def store_scorecard(org_id: Any, provider_id: UUID, scorecard: dict[str, Any]) -> None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "UPDATE llm_providers SET scorecard = CAST(:sc AS jsonb) "
                "WHERE id = :id AND org_id = :org"
            ),
            {"sc": json.dumps(scorecard), "id": str(provider_id), "org": str(org_id)},
        )


async def set_active(org_id: Any, provider_id: UUID) -> None:
    """Make this binding the workspace's ACTIVE detector, demoting any prior active one.
    The partial unique index guarantees at most one active per workspace."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "UPDATE llm_providers SET status = 'rejected' "
                "WHERE org_id = :org AND status = 'active' AND id <> :id"
            ),
            {"org": str(org_id), "id": str(provider_id)},
        )
        await s.execute(
            text(
                "UPDATE llm_providers SET status = 'active' WHERE id = :id AND org_id = :org"
            ),
            {"id": str(provider_id), "org": str(org_id)},
        )


async def promote_if_holds(org_id: Any, provider_id: UUID) -> bool:
    """The MEASURED PROMOTION GATE: promote a binding to ACTIVE only if its stored
    scorecard HOLDS the baseline (run score_provider + store_scorecard first). Returns
    whether it was promoted. A provider only becomes a workspace's detector by the
    numbers — never a vibe."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text("SELECT scorecard FROM llm_providers WHERE id = :id AND org_id = :org"),
                {"id": str(provider_id), "org": str(org_id)},
            )
        ).first()
    if row is None or not row.scorecard or not row.scorecard.get("holds"):
        return False
    await set_active(org_id, provider_id)
    return True


async def get_config(org_id: Any, provider_id: UUID) -> ProviderConfig | None:
    """The full binding (with decrypted credential) for a specific id — for SCORING a
    proposed provider before promotion."""
    return await _config_query(
        org_id, "WHERE id = :id AND org_id = :org", {"id": str(provider_id), "org": str(org_id)}
    )


async def active_config(org_id: Any) -> ProviderConfig | None:
    """The workspace's ACTIVE provider binding (with decrypted credential), or None →
    the gateway falls back to the keyless lexical floor."""
    return await _config_query(
        org_id, "WHERE org_id = :org AND status = 'active'", {"org": str(org_id)}
    )


async def _config_query(org_id: Any, where: str, params: dict) -> ProviderConfig | None:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text(f"SELECT id, provider, model, credential_enc FROM llm_providers {where}"),
                params,
            )
        ).first()
    if row is None:
        return None
    creds = _decrypt_credential(row.credential_enc)
    return ProviderConfig(
        id=row.id, provider=row.provider, model=row.model,
        api_key=creds.get("api_key"), api_base=creds.get("api_base"),
    )


async def list_providers(org_id: Any) -> list[ProviderOut]:
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text(
                    "SELECT id, provider, model, status, scorecard FROM llm_providers "
                    "WHERE org_id = :org ORDER BY created_at"
                ),
                {"org": str(org_id)},
            )
        ).all()
    return [
        ProviderOut(
            id=r.id, provider=r.provider, model=r.model, status=r.status,
            residency=provider_residency(r.provider), scorecard=r.scorecard,
        )
        for r in rows
    ]
