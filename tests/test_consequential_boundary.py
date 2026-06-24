"""G3 — act via the trust ladder: the consequential boundary (the spine).

The signed-off boundary, proven both as a PURE decision and END-TO-END through the agent's
propose path (the agent only PROPOSES; the deterministic engine disposes):

  read_only                                                 -> auto
  reversible + HIGH grounding + non-prod + has rollback     -> AUTO-EXECUTE (safe majority)
  reversible + (low/absent grounding | prod | no rollback)  -> GATE
  destructive                                               -> always GATE
  production-touching (any class)                           -> always GATE
  low grounding (any class)                                 -> always GATE

The chat surface is PROVABLY unable to bypass the gate: it can only emit a propose_action,
which routes through resolve_proposal; a gated action lands in awaiting_approval and is never
enqueued for execution.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge.agent import run_agent
from opsforge.db import scope_to_org, session_factory
from opsforge.knowledge import ProvenanceEnvelope, set_reconciliation, store_chunk
from opsforge.policy import is_production_target, resolve_proposal

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)

_M = {
    "proposals": [
        {"tool": "k8s.rollback", "class": "reversible",
         "rollback": {"tool": "k8s.redeploy", "params": {}}},
        {"tool": "k8s.scale", "class": "reversible"},  # no rollback → irreversible
        {"tool": "k8s.delete_ns", "class": "destructive", "rollback": {"tool": "x"}},
    ],
}
_HIGH = {"low_confidence": False, "grounding_confidence": 0.9, "chunk_count": 3}
_LOW = {"low_confidence": True, "grounding_confidence": 0.2, "chunk_count": 1}


# --------------------------------------------------------------------------- #
# pure decision — the spine
# --------------------------------------------------------------------------- #
def test_reversible_auto_executes_when_safe():
    t = resolve_proposal(_M, "k8s.rollback", None, grounding=_HIGH, production=False,
                         non_prod_attested=True)
    assert t["auto_execute"] is True and t["state"] == "approved"
    assert "reversible_auto_safe" in t["rules"]
    assert t["has_rollback"] is True and t["production"] is False


def test_unattested_environment_gates_safe_path():
    # no explicit non_prod connector vouch → the safe auto path gates (fail-closed)
    t = resolve_proposal(_M, "k8s.rollback", None, grounding=_HIGH, production=False,
                         non_prod_attested=False)
    assert t["auto_execute"] is False
    assert "unattested_environment_gate" in t["rules"]


def test_no_rollback_overrides_admin_grant():
    # irreversible is an always-gate property — a grant cannot auto-execute it (Fix: F-1)
    granted = {"k8s.scale": "auto_with_notify"}
    t = resolve_proposal(_M, "k8s.scale", granted, grounding=_HIGH, production=False,
                         non_prod_attested=True)
    assert t["auto_execute"] is False and "irreversible_gate" in t["rules"]


def test_reversible_without_rollback_gates():
    t = resolve_proposal(_M, "k8s.scale", None, grounding=_HIGH, production=False)
    assert t["auto_execute"] is False and t["state"] == "awaiting_approval"


def test_production_always_gates():
    t = resolve_proposal(_M, "k8s.rollback", None, grounding=_HIGH, production=True)
    assert t["auto_execute"] is False
    assert "production_gate" in t["rules"]


def test_low_grounding_always_gates():
    t = resolve_proposal(_M, "k8s.rollback", None, grounding=_LOW, production=False)
    assert t["auto_execute"] is False
    assert "low_grounding_gate" in t["rules"]


def test_absent_grounding_gates_safe_path():
    # safe-error: no grounding signal is NOT high grounding → gate
    t = resolve_proposal(_M, "k8s.rollback", None, grounding=None, production=False,
                         non_prod_attested=True)
    assert t["auto_execute"] is False
    assert "insufficient_grounding_gate" in t["rules"]


def test_destructive_never_auto_executes():
    t = resolve_proposal(_M, "k8s.delete_ns", None, grounding=_HIGH, production=False)
    assert t["auto_execute"] is False and t["state"] == "awaiting_approval"


def test_production_overrides_admin_grant():
    granted = {"k8s.rollback": "auto_with_notify"}
    t = resolve_proposal(_M, "k8s.rollback", granted, grounding=_HIGH, production=True)
    assert t["auto_execute"] is False and "production_gate" in t["rules"]


def test_low_grounding_overrides_admin_grant():
    granted = {"k8s.rollback": "auto_with_notify"}
    t = resolve_proposal(_M, "k8s.rollback", granted, grounding=_LOW, production=False)
    assert t["auto_execute"] is False and "low_grounding_gate" in t["rules"]


def test_is_production_target():
    assert is_production_target("prod", None) is True
    assert is_production_target("non_prod", "svc://staging/api") is False
    assert is_production_target("non_prod", "customer-acme-db") is True  # glob backstop
    assert is_production_target(None, "prod-cluster-1") is True
    assert is_production_target(None, "svc://dev/x") is False


# --------------------------------------------------------------------------- #
# end-to-end: the agent proposes; the action lands in the boundary-correct state
# --------------------------------------------------------------------------- #
pytestmark_db = pytest.mark.usefixtures("db_required")

_PROP_MANIFEST = {
    "context": {"graph": False},
    "tools": [
        {"tool": "kb.search_knowledge", "class": "read_only"},
        {"tool": "kb.process", "class": "read_only"},
    ],
    "proposals": [
        {"tool": "kubernetes.rollback_deploy", "class": "reversible",
         "rollback": {"tool": "kubernetes.redeploy", "params": {}}},
        {"tool": "kubernetes.delete_namespace", "class": "destructive",
         "rollback": {"tool": "x"}},
    ],
    "policy": {"max_tool_calls": 4},
}


class _ActGateway:
    """Optionally INVESTIGATES a process (a real kb read, so grounding binds to it), then
    proposes the action, then reports."""

    def __init__(self, args: dict, investigate_pk: str | None = None):
        self._args = args
        self._pk = investigate_pk
        self._investigated = False
        self._proposed = False

    async def chat(self, messages, tools, model):
        from opsforge.gateway import ChatResult, ToolCall

        if self._pk and not self._investigated:
            self._investigated = True
            return ChatResult(text="investigating", tool_calls=[
                ToolCall("i", "kb__search_knowledge", {"process_key": self._pk})])
        if not self._proposed:
            self._proposed = True
            return ChatResult(text="proposing",
                              tool_calls=[ToolCall("p", "propose_action", self._args)])
        return ChatResult(text="done", tool_calls=[ToolCall("s", "submit_report", {
            "hypothesis": "done", "confidence": "high", "evidence": []})])

    async def embedding(self, texts, model):
        return [[0.0] * 1536 for _ in texts]


async def _seed_chunk(org, pk, confidence):
    cid = await store_chunk(
        org_id=org, content="well-grounded fact",
        envelope=ProvenanceEnvelope(source_kind="behaviour", source_ref="x://f",
                                    observed_at=AS_OF, ingested_at=AS_OF),
        process_key=pk)
    await set_reconciliation(org, cid, confidence=confidence, corroborated_by=2,
                             contradicted_by=0, reconciliation_id=uuid.uuid4())


async def _seed_connector(org, env):
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        await s.execute(
            text("INSERT INTO connectors (org_id, name, kind, transport, endpoint, environment) "
                 "VALUES (:o,'k8s','kubernetes','http','http://x',:env)"),
            {"o": str(org), "env": env})


async def _cleanup(org):
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("actions", "knowledge_chunks", "connectors", "runs"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def _run_propose(org, args, *, investigate_pk=None):
    skill = {"id": None, "manifest": _PROP_MANIFEST, "instructions": "act",
             "trust_overrides": {}, "model": None}
    trigger = {"kind": "chat", "payload": {"query": "do it"}}
    async with session_factory().begin() as s:
        run_id = (await s.execute(
            text("INSERT INTO runs (org_id, status, trigger) "
                 "VALUES (:o,'queued',CAST(:t AS jsonb)) RETURNING id"),
            {"o": org, "t": json.dumps(trigger)})).scalar_one()
    await run_agent(run_id, skill, _ActGateway(args, investigate_pk=investigate_pk))
    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        row = (await s.execute(
            text("SELECT state, action_class, policy_trace, rollback FROM actions WHERE run_id=:r"),
            {"r": str(run_id)})).first()
    return row


@pytestmark_db
async def test_safe_reversible_auto_executes_end_to_end():
    """reversible + INVESTIGATED high grounding + non_prod connector + rollback → AUTO."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.9)
        await _seed_connector(org, "non_prod")  # explicit operator attestation
        row = await _run_propose(org, {
            "tool": "kubernetes.rollback_deploy", "params": {"deployment": "api"},
            "target_ref": "svc://staging/api", "process_key": pk}, investigate_pk=pk)
        assert row.state == "approved"  # auto-executed (enqueued)
        assert row.policy_trace["auto_execute"] is True
        assert "reversible_auto_safe" in row.policy_trace["rules"]
        assert row.rollback and row.rollback["tool"] == "kubernetes.redeploy"  # rollback persisted
    finally:
        await _cleanup(org)


@pytestmark_db
async def test_production_connector_gates_end_to_end():
    """Same safe action, but the connector is tagged prod → GATE (the chat cannot bypass)."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.9)
        await _seed_connector(org, "prod")
        row = await _run_propose(org, {
            "tool": "kubernetes.rollback_deploy", "params": {}, "target_ref": "svc://x",
            "process_key": pk}, investigate_pk=pk)
        assert row.state == "awaiting_approval"  # gated, NOT auto-executed
        assert row.policy_trace["auto_execute"] is False
        assert "production_gate" in row.policy_trace["rules"]
    finally:
        await _cleanup(org)


@pytestmark_db
async def test_production_in_params_gates_end_to_end():
    """The model puts a BENIGN target_ref but a prod identifier in params; the executor acts
    on params, so production must still gate (F-3/4: params are scanned, not just target_ref)."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.9)
        await _seed_connector(org, "non_prod")  # connector says non_prod...
        row = await _run_propose(org, {
            "tool": "kubernetes.rollback_deploy",
            "params": {"namespace": "customer-acme-prod"},  # ...but params target customer prod
            "target_ref": "svc://looks-fine", "process_key": pk}, investigate_pk=pk)
        assert row.state == "awaiting_approval"
        assert "production_gate" in row.policy_trace["rules"]
    finally:
        await _cleanup(org)


@pytestmark_db
async def test_uninvestigated_process_key_does_not_ground_end_to_end():
    """The agent cites a real high-confidence process_key it NEVER investigated → grounding
    does not count → gate (F-2: grounding binds to investigation, not a model claim)."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.95)  # a genuinely high-confidence process exists
        await _seed_connector(org, "non_prod")
        row = await _run_propose(org, {
            "tool": "kubernetes.rollback_deploy", "params": {}, "target_ref": "svc://staging",
            "process_key": pk})  # NOTE: investigate_pk=None → never read this run
        assert row.state == "awaiting_approval"
        assert row.policy_trace["auto_execute"] is False
        assert "insufficient_grounding_gate" in row.policy_trace["rules"]
    finally:
        await _cleanup(org)


@pytestmark_db
async def test_low_grounding_gates_end_to_end():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.3)  # low confidence
        await _seed_connector(org, "non_prod")  # isolate the grounding gate
        row = await _run_propose(org, {
            "tool": "kubernetes.rollback_deploy", "params": {}, "target_ref": "svc://x",
            "process_key": pk}, investigate_pk=pk)
        assert row.state == "awaiting_approval"
        assert "low_grounding_gate" in row.policy_trace["rules"]
    finally:
        await _cleanup(org)


@pytestmark_db
async def test_destructive_gates_end_to_end():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _seed_chunk(org, pk, 0.95)
        await _seed_connector(org, "non_prod")
        row = await _run_propose(org, {
            "tool": "kubernetes.delete_namespace", "params": {}, "target_ref": "svc://x",
            "process_key": pk}, investigate_pk=pk)
        assert row.state == "awaiting_approval"
        assert row.action_class == "destructive"
        assert row.policy_trace["auto_execute"] is False
    finally:
        await _cleanup(org)
