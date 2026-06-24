"""M2: deterministic policy engine — pure, no DB."""

from __future__ import annotations

from opsforge.policy import (
    check_tool_call,
    effective_trust,
    resolve_proposal,
    tool_action_class,
)

MANIFEST = {
    "tools": [
        {"tool": "kubernetes.list_pods", "class": "read_only"},
        {"tool": "kubernetes.get_logs", "class": "read_only", "redact": True},
    ],
    "proposals": [
        {"tool": "kubernetes.rollback_deploy", "class": "reversible"},
        {"tool": "kubernetes.delete_namespace", "class": "destructive"},
    ],
    "policy": {"forbidden_targets": ["k8s://prod/ns/vault*"]},
}


def test_effective_trust_ladder():
    assert effective_trust("read_only", "kubernetes.list_pods", {}) == "auto_allow"
    assert effective_trust("reversible", "kubernetes.restart_pod", {}) == "awaiting_approval"
    granted = {"kubernetes.restart_pod": "auto_with_notify"}
    assert effective_trust("reversible", "kubernetes.restart_pod", granted) == "auto_allow"
    assert (
        effective_trust("destructive", "kubernetes.delete_namespace", {})
        == "awaiting_approval"
    )
    # destructive is never gradable in v1
    destructive_grant = {"kubernetes.delete_namespace": "auto_with_notify"}
    assert (
        effective_trust("destructive", "kubernetes.delete_namespace", destructive_grant)
        == "awaiting_approval"
    )


def test_tool_action_class():
    assert tool_action_class(MANIFEST, "kubernetes.list_pods") == "read_only"
    assert tool_action_class(MANIFEST, "kubernetes.rollback_deploy") == "reversible"
    assert tool_action_class(MANIFEST, "nope.tool") is None


def test_check_tool_call_allows_read_only():
    trace = check_tool_call(MANIFEST, "kubernetes.list_pods", {})
    assert trace["allowed"] is True
    assert trace["action_class"] == "read_only"


def test_check_tool_call_blocks_proposals_and_unknown():
    assert check_tool_call(MANIFEST, "kubernetes.rollback_deploy", {})["allowed"] is False
    assert check_tool_call(MANIFEST, "kubernetes.nope", {})["allowed"] is False


def test_check_tool_call_blocks_forbidden_target():
    trace = check_tool_call(
        MANIFEST, "kubernetes.list_pods", {}, target_ref="k8s://prod/ns/vault-prod"
    )
    assert trace["allowed"] is False
    assert "forbidden_target" in trace["rules"]


def test_resolve_proposal_trust_states():
    # Ungraduated reversible tool waits for a human.
    trace = resolve_proposal(MANIFEST, "kubernetes.rollback_deploy", None)
    assert trace["allowed"] is True
    assert trace["state"] == "awaiting_approval"
    assert trace["auto_execute"] is False
    # A graduated (auto_with_notify) reversible tool auto-approves (Phase 2).
    trace2 = resolve_proposal(
        MANIFEST,
        "kubernetes.rollback_deploy",
        {"kubernetes.rollback_deploy": "auto_with_notify"},
    )
    assert trace2["state"] == "approved"
    assert trace2["auto_execute"] is True


def test_resolve_proposal_denies_undeclared():
    assert resolve_proposal(MANIFEST, "kubernetes.list_pods", None)["state"] == "denied"
