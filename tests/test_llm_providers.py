"""M7.6 Job A — the LLM as a vault-credentialed, per-workspace connector.

Acceptance, by the numbers:
  * the credential lives in the Fernet vault — never in an API view, never in `.env` for
    production (registry + redaction);
  * resolution is per-workspace and multi-provider (OpenAI-real path + a 2nd provider
    resolved purely at the litellm-routing level with a stub credential);
  * a workspace with NO active binding falls to the keyless lexical floor — NOT a shared
    global key — so LLM isolation holds per workspace;
  * a provider becomes a workspace's detector only through the MEASURED promotion gate
    (proposed → scored → promoted-iff-it-holds-the-baseline).
"""

from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from opsforge.config import get_settings
from opsforge.llm_providers import (
    active_config,
    get_config,
    is_residency_clean,
    list_providers,
    promote_if_holds,
    propose_provider,
    provider_residency,
    set_active,
    store_scorecard,
)
from opsforge.reconcile import LexicalDetector, LLMDetector, configured_detector

pytestmark = pytest.mark.usefixtures("db_required")


@pytest.fixture
def vault():
    """A live Fernet key for the duration of one test (the vault needs a real key to
    encrypt credentials). Restores the prior key + settings cache on teardown."""
    prev = os.environ.get("OPSFORGE_FERNET_KEY")
    os.environ["OPSFORGE_FERNET_KEY"] = Fernet.generate_key().decode()
    get_settings.cache_clear()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OPSFORGE_FERNET_KEY", None)
        else:
            os.environ["OPSFORGE_FERNET_KEY"] = prev
        get_settings.cache_clear()


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(text("DELETE FROM llm_providers WHERE org_id = :o"), {"o": org})


def _no_env_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# provider registry — residency classification (no DB)
# --------------------------------------------------------------------------- #
def test_enterprise_providers_are_residency_clean():
    for p in ("openai", "azure", "anthropic", "bedrock", "vllm", "ollama"):
        assert provider_residency(p) == "enterprise"
        assert is_residency_clean(p)


def test_aggregators_are_flagged_non_residency():
    for p in ("openrouter", "together_ai", "groq"):
        assert provider_residency(p) == "aggregator"
        assert not is_residency_clean(p)


def test_unknown_provider_fails_safe_not_clean():
    # An unrecognised provider is NOT treated as residency-clean — fail safe.
    assert provider_residency("mystery-router") == "unknown"
    assert not is_residency_clean("mystery-router")


# --------------------------------------------------------------------------- #
# the credential lives in the vault, never in the operator view
# --------------------------------------------------------------------------- #
async def test_credential_is_vaulted_and_never_serialized(vault):
    org = str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org, provider="openai", model="gpt-4o-mini",
            credential={"api_key": "sk-super-secret-XYZ"},
        )
        # The decrypted credential resolves only through the internal config path…
        cfg = await get_config(org, pid)
        assert cfg is not None and cfg.api_key == "sk-super-secret-XYZ"

        # …and is absent from the operator-facing view (no api_key field at all).
        out = await list_providers(org)
        assert len(out) == 1
        dumped = out[0].model_dump()
        assert "api_key" not in dumped and "credential" not in dumped
        assert "sk-super-secret-XYZ" not in str(dumped)
        assert out[0].residency == "enterprise"

        # The stored ciphertext is not the plaintext (it really went through Fernet).
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            blob = (
                await s.execute(
                    text("SELECT credential_enc FROM llm_providers WHERE id = :id"),
                    {"id": str(pid)},
                )
            ).scalar_one()
        assert b"sk-super-secret-XYZ" not in bytes(blob)
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# resolution is per-workspace + multi-provider (routing-level)
# --------------------------------------------------------------------------- #
async def test_active_provider_resolves_to_its_detector(vault, monkeypatch):
    """OpenAI-real path: an active binding resolves to an LLMDetector carrying THAT
    workspace's model + vaulted key — no env key involved."""
    _no_env_keys(monkeypatch)
    org = str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org, provider="openai", model="gpt-4o-mini",
            credential={"api_key": "sk-workspace-A"},
        )
        await set_active(org, pid)

        det = await configured_detector(org)
        assert isinstance(det, LLMDetector)
        assert det.model == "gpt-4o-mini"
        assert det.gateway.api_key == "sk-workspace-A"
    finally:
        await _cleanup(org)


async def test_second_provider_resolves_at_routing_level(vault, monkeypatch):
    """The 2nd provider is proven purely at the resolution/litellm-routing level: a
    provider-prefixed model id + a stub credential flow through unchanged. No real call —
    litellm routes on the model prefix."""
    _no_env_keys(monkeypatch)
    org = str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org, provider="anthropic", model="anthropic/claude-3-5-haiku",
            credential={"api_key": "sk-ant-stub"},
        )
        await set_active(org, pid)

        det = await configured_detector(org)
        assert isinstance(det, LLMDetector)
        assert det.model == "anthropic/claude-3-5-haiku"  # routes to Anthropic
        assert det.gateway.api_key == "sk-ant-stub"
    finally:
        await _cleanup(org)


async def test_self_hosted_provider_resolves_with_api_base_no_key(vault, monkeypatch):
    """A self-hosted binding (vLLM/Ollama) carries an api_base and no api_key — still a
    first-class, residency-clean resolution."""
    _no_env_keys(monkeypatch)
    org = str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org, provider="ollama", model="ollama/llama3.1",
            credential={"api_base": "http://gpu-box.internal:11434"},
        )
        await set_active(org, pid)

        det = await configured_detector(org)
        assert isinstance(det, LLMDetector)
        assert det.gateway.api_base == "http://gpu-box.internal:11434"
        assert det.gateway.api_key is None
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# isolation: no binding → lexical floor, NOT a shared global key
# --------------------------------------------------------------------------- #
async def test_no_binding_under_production_falls_to_lexical_floor(vault, monkeypatch):
    """Production (dev_llm_fallback off): a workspace with no active provider gets the
    keyless lexical floor even when a shared env key is present — the env key must NOT
    leak across workspaces."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-global")  # tempting, must be ignored
    monkeypatch.setattr(get_settings(), "dev_llm_fallback", False)
    org = str(uuid.uuid4())
    try:
        det = await configured_detector(org)
        assert isinstance(det, LexicalDetector)
    finally:
        await _cleanup(org)


async def test_active_binding_with_unresolvable_credential_fails_closed(vault, monkeypatch):
    """Review HIGH F-B: an ACTIVE binding whose vaulted credential cannot be decrypted (a
    Fernet-key rotation or a corrupted blob → api_key and api_base both resolve to None) must
    fail CLOSED to the lexical floor. It must NEVER build a key-less litellm gateway, which
    would silently read the ambient shared OPENAI_API_KEY — routing this workspace's data
    through a global key it was never bound to, even with dev_llm_fallback off."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-global")  # present + tempting
    monkeypatch.setattr(get_settings(), "dev_llm_fallback", False)
    org = str(uuid.uuid4())
    try:
        # An active binding whose credential_enc is not a valid Fernet token → decrypt fails
        # → _decrypt_credential swallows it → ProviderConfig(api_key=None, api_base=None).
        from opsforge.db import scope_to_org, session_factory

        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(
                text(
                    "INSERT INTO llm_providers (org_id, provider, model, credential_enc, status) "
                    "VALUES (:org, 'openai', 'gpt-4o-mini', :bad, 'active')"
                ),
                {"org": org, "bad": b"not-a-valid-fernet-token"},
            )
        det = await configured_detector(org)
        assert isinstance(det, LexicalDetector)  # floor — NOT the shared env key
    finally:
        await _cleanup(org)


async def test_one_workspace_binding_does_not_leak_to_another(vault, monkeypatch):
    """Per-workspace isolation: org A's active provider does not become org B's detector;
    B (no binding, production) stays on the lexical floor."""
    _no_env_keys(monkeypatch)
    monkeypatch.setattr(get_settings(), "dev_llm_fallback", False)
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org_a, provider="openai", model="gpt-4o-mini",
            credential={"api_key": "sk-only-A"},
        )
        await set_active(org_a, pid)

        det_a = await configured_detector(org_a)
        det_b = await configured_detector(org_b)
        assert isinstance(det_a, LLMDetector) and det_a.gateway.api_key == "sk-only-A"
        assert isinstance(det_b, LexicalDetector)  # B never sees A's credential
    finally:
        await _cleanup(org_a)
        await _cleanup(org_b)


# --------------------------------------------------------------------------- #
# the measured promotion gate
# --------------------------------------------------------------------------- #
async def test_promotion_is_gated_on_a_holding_scorecard(vault):
    """proposed → scored → promoted. A binding whose scorecard does NOT hold the baseline
    is refused promotion (stays proposed, no active detector); one that holds is promoted
    and becomes the workspace's resolver."""
    org = str(uuid.uuid4())
    try:
        pid = await propose_provider(
            org, provider="openai", model="gpt-4o-mini",
            credential={"api_key": "sk-A"},
        )
        # A failing scorecard → refused.
        await store_scorecard(org, pid, {"accuracy": 0.55, "baseline": 1.0, "holds": False})
        assert await promote_if_holds(org, pid) is False
        assert await active_config(org) is None

        # A holding scorecard → promoted, now resolvable.
        await store_scorecard(org, pid, {"accuracy": 1.0, "baseline": 1.0, "holds": True})
        assert await promote_if_holds(org, pid) is True
        cfg = await active_config(org)
        assert cfg is not None and cfg.id == pid
    finally:
        await _cleanup(org)


async def test_promotion_demotes_the_prior_active_binding(vault):
    """At most one active binding per workspace: promoting a new one demotes the old."""
    org = str(uuid.uuid4())
    try:
        p1 = await propose_provider(
            org, provider="openai", model="gpt-4o-mini", credential={"api_key": "sk-1"}
        )
        p2 = await propose_provider(
            org, provider="azure", model="azure/gpt-4o", credential={"api_key": "sk-2"}
        )
        for pid in (p1, p2):
            await store_scorecard(org, pid, {"holds": True})
        assert await promote_if_holds(org, p1) is True
        assert (await active_config(org)).id == p1
        assert await promote_if_holds(org, p2) is True
        assert (await active_config(org)).id == p2  # p1 demoted

        statuses = {o.id: o.status for o in await list_providers(org)}
        assert statuses[p1] == "rejected" and statuses[p2] == "active"
    finally:
        await _cleanup(org)
