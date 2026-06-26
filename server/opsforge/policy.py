"""Deterministic policy engine — pure functions, no LLM (doctrine #3).

LLM output is data; these functions are the code that decides what may happen.
`policy_trace` returns are sufficient to replay a decision. This module must
never import `agent`.
"""

from __future__ import annotations

import datetime
import fnmatch
from typing import Any, Literal

TrustDecision = Literal["auto_allow", "awaiting_approval", "denied"]

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


# --------------------------------------------------------------------------- #
# GAP 3 — ops rules as config (freeze windows + per-priority approval), pure.
# --------------------------------------------------------------------------- #
def freeze_active(
    freeze_windows: list[dict[str, Any]] | None, now: datetime.datetime
) -> bool:
    """True if `now` falls inside any weekly change-freeze window.

    Window: {days_of_week?: [0..6] (Mon=0; omitted = every day),
             start: "HH:MM", end: "HH:MM"} in the same tz as `now`.
    """
    current = now.strftime("%H:%M")
    for w in freeze_windows or []:
        days = w.get("days_of_week")
        if days is not None and now.weekday() not in days:
            continue
        if w.get("start", "00:00") <= current <= w.get("end", "23:59"):
            return True
    return False


def min_approval_role(policy: dict[str, Any], priority: str | None) -> str | None:
    """The role required to approve a remediation given the incident priority."""
    if not priority:
        return None
    return (policy.get("requires_role_for_priority") or {}).get(priority)


def role_allows(actor_role: str | None, required_role: str | None) -> bool:
    """True if actor_role meets or exceeds required_role (viewer<operator<admin)."""
    if not required_role:
        return True
    return _ROLE_RANK.get(actor_role or "", -1) >= _ROLE_RANK.get(required_role, 99)


def case_budget(manifest: dict[str, Any]) -> int:
    """Max number of runs in one iterative-remediation case (Slice 2). The chain hook spawns a
    follow-up run only while the NEXT step is < this, so the iterate loop is hard-bounded and can
    never run away. Pure; reads the skill manifest's policy.max_case_steps (default 3, floor 1)."""
    try:
        n = int((manifest.get("policy") or {}).get("max_case_steps", 3))
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def effective_trust(
    action_class: str,
    tool_fqn: str,
    trust_overrides: dict[str, Any] | None,
) -> TrustDecision:
    """Resolve the trust ladder for a proposed action.

    read_only   -> auto_allow
    reversible  -> awaiting_approval, unless an admin has granted
                   `auto_with_notify` for this exact tool (a recorded human act)
    destructive -> always awaiting_approval (never gradable in v1)
    """
    if action_class == "read_only":
        return "auto_allow"
    if action_class == "reversible":
        grant = (trust_overrides or {}).get(tool_fqn)
        if grant == "auto_with_notify":
            return "auto_allow"
        return "awaiting_approval"
    if action_class == "destructive":
        return "awaiting_approval"
    return "denied"


def _index_tools(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map tool_fqn -> {class, redact, kind: tools|proposals} from the manifest."""
    index: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("tools", []) or []:
        index[entry["tool"]] = {**entry, "_section": "tools"}
    for entry in manifest.get("proposals", []) or []:
        index[entry["tool"]] = {**entry, "_section": "proposals"}
    return index


def tool_action_class(manifest: dict[str, Any], tool_fqn: str) -> str | None:
    entry = _index_tools(manifest).get(tool_fqn)
    return entry.get("class") if entry else None


def _target_forbidden(manifest: dict[str, Any], target_ref: str | None) -> bool:
    if not target_ref:
        return False
    for pattern in manifest.get("policy", {}).get("forbidden_targets", []) or []:
        if fnmatch.fnmatch(target_ref, pattern):
            return True
    return False


# --------------------------------------------------------------------------- #
# G3 — production detection (connector environment tag + target_ref glob backstop)
# --------------------------------------------------------------------------- #
_DEFAULT_PROD_GLOBS = (
    "*prod*", "*production*", "customer-*", "prod-*", "*/prod/*", "*/production/*",
)


def is_production_target(
    environment: str | None,
    target_ref: str | None,
    extra_globs: list[str] | None = None,
) -> bool:
    """Production-touching iff the connector is tagged 'prod' OR the target_ref matches a
    production glob. The glob is the backstop that catches a mis-tagged (or untagged)
    connector aimed at a prod/customer target. Pure — the decision is auditable."""
    if environment == "prod":
        return True
    globs = list(_DEFAULT_PROD_GLOBS) + list(extra_globs or [])
    if target_ref:
        low = target_ref.lower()
        for g in globs:
            if fnmatch.fnmatch(low, g.lower()):
                return True
    return False


def check_tool_call(
    manifest: dict[str, Any],
    tool_fqn: str,
    params: dict[str, Any] | None = None,
    target_ref: str | None = None,
) -> dict[str, Any]:
    """Pre-check a connector tool call the agent wants to make.

    Returns a policy_trace dict: {allowed, reason, rules[], action_class}.
    Only read_only tools listed under manifest `tools:` are callable during a
    run; anything else (proposals, unknown, forbidden target) is blocked.
    """
    rules: list[str] = []
    index = _index_tools(manifest)
    entry = index.get(tool_fqn)

    if entry is None:
        return {
            "allowed": False,
            "reason": f"tool {tool_fqn} not in manifest",
            "rules": ["tool_not_in_manifest"],
            "action_class": None,
        }
    if entry["_section"] != "tools":
        return {
            "allowed": False,
            "reason": f"tool {tool_fqn} is a proposal, not a callable tool",
            "rules": ["tool_is_proposal"],
            "action_class": entry.get("class"),
        }

    action_class = entry.get("class", "read_only")
    rules.append(f"manifest:{action_class}")
    if action_class != "read_only":
        return {
            "allowed": False,
            "reason": "only read_only tools are callable during a run",
            "rules": [*rules, "non_read_only_blocked"],
            "action_class": action_class,
        }
    if _target_forbidden(manifest, target_ref):
        return {
            "allowed": False,
            "reason": f"target {target_ref} is forbidden",
            "rules": [*rules, "forbidden_target"],
            "action_class": action_class,
        }
    return {
        "allowed": True,
        "reason": "ok",
        "rules": rules,
        "action_class": action_class,
    }


def resolve_proposal(
    manifest: dict[str, Any],
    tool_fqn: str,
    trust_overrides: dict[str, Any] | None,
    *,
    grounding: dict[str, Any] | None = None,
    production: bool = False,
    non_prod_attested: bool = False,
) -> dict[str, Any]:
    """Compute the policy_trace + initial state for a proposed action — the G3
    consequential boundary (signed off):

      read_only                                              -> auto
      reversible + HIGH grounding + non-prod + has rollback  -> AUTO-EXECUTE (the safe majority)
      reversible + (low/absent grounding | prod | no rollback) -> GATE
      destructive                                            -> always GATE
      production-touching (any class)                        -> always GATE
      low grounding (any class)                              -> always GATE

    The agent still only PROPOSES; this pure function disposes. Every input
    (grounding, production, has_rollback) is recorded in the trace so the audit
    shows exactly why an action auto-executed or gated.

    Grounding polarity is asymmetric on purpose (safe-error): the NEW reversible
    auto-execute path requires grounding to be EXPLICITLY high (absent grounding
    → gate); an EXPLICITLY low grounding gates any auto decision. A pre-existing
    admin `auto_with_notify` grant (a recorded human act) still auto-executes a
    reversible tool, but production and low grounding gate even a granted tool.
    """
    index = _index_tools(manifest)
    entry = index.get(tool_fqn)
    if entry is None or entry["_section"] != "proposals":
        return {
            "allowed": False,
            "state": "denied",
            "reason": f"{tool_fqn} is not a declared proposal",
            "action_class": entry.get("class") if entry else None,
            "rules": ["proposal_not_in_manifest"],
        }
    action_class = entry.get("class", "reversible")
    has_rollback = bool(entry.get("rollback"))
    decision = effective_trust(action_class, tool_fqn, trust_overrides)
    rules = [f"trust:{decision}"]

    low_grounding = bool(grounding and grounding.get("low_confidence"))
    high_grounding = bool(grounding) and not low_grounding

    # NEW (G3): the safe auto-execute path for a non-granted reversible action — and the
    # auditable REASON it gates when any precondition fails. Auto-execute requires a declared
    # rollback, a NON-production target, an explicit operator non_prod attestation on the
    # connector (fail-closed for an unknown/untagged connector), and EXPLICITLY high grounding.
    # Precedence is deliberate.
    if action_class == "reversible" and decision == "awaiting_approval":
        if not has_rollback:
            rules.append("irreversible_gate")
        elif production:
            rules.append("production_gate")
        elif low_grounding:
            rules.append("low_grounding_gate")
        elif not non_prod_attested:
            rules.append("unattested_environment_gate")
        elif not high_grounding:
            rules.append("insufficient_grounding_gate")
        else:
            decision = "auto_allow"
            rules.append("reversible_auto_safe")

    # ALWAYS-GATE overrides — downgrade ANY auto decision (e.g. an admin-granted reversible)
    # that is irreversible, production-touching, or low-grounded. These three are
    # non-negotiable human gates regardless of how the auto decision was reached. Never upgrade.
    if decision == "auto_allow":
        downgrade = []
        if action_class == "reversible" and not has_rollback:
            downgrade.append("irreversible_gate")
        if production:
            downgrade.append("production_gate")
        if low_grounding:
            downgrade.append("low_grounding_gate")
        if downgrade:
            decision = "awaiting_approval"
            rules.extend(d for d in downgrade if d not in rules)

    state = {
        "auto_allow": "approved",
        "awaiting_approval": "awaiting_approval",
        "denied": "denied",
    }[decision]
    gate_rules = [r for r in rules if r.endswith("_gate")]
    reason = f"trust={decision}"
    if decision == "auto_allow" and "reversible_auto_safe" in rules:
        reason += "; auto:reversible_safe"
    elif gate_rules:
        reason += "; gated:" + ",".join(r.removesuffix("_gate") for r in gate_rules)
    return {
        "allowed": decision != "denied",
        "state": state,
        "auto_execute": decision == "auto_allow",
        "reason": reason,
        "action_class": action_class,
        "rules": rules,
        "grounding": grounding,
        "production": production,
        "non_prod_attested": non_prod_attested,
        "has_rollback": has_rollback,
    }
