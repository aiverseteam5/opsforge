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


def check_tool_call(
    manifest: dict[str, Any],
    tool_fqn: str,
    params: dict[str, Any] | None = None,
    target_ref: str | None = None,
    *,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """Pre-check a connector tool call the agent wants to make.

    Returns a policy_trace dict: {allowed, reason, rules[], action_class}.
    Only read_only tools listed under manifest `tools:` are callable during a
    run; anything else (proposals, unknown, forbidden target) is blocked.

    When `scope` is provided (delegation token callers), the tool must also
    appear in that list — scope narrows what an already-allowed token may do.
    """
    rules: list[str] = []

    if scope is not None and tool_fqn not in scope:
        return {
            "allowed": False,
            "reason": f"tool {tool_fqn} not permitted by delegation scope",
            "rules": ["scope_not_permitted"],
            "action_class": None,
        }

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
) -> dict[str, Any]:
    """Compute the policy_trace + initial state for a proposed action.

    The trust ladder decides the baseline. On top of it, the low-grounding gate
    (M6.5): a consequential action proposed on low-confidence knowledge is forced
    to awaiting_approval regardless of the trust line — the agent may act
    autonomously only on high-confidence grounding. The `grounding` summary is
    recorded in the trace, so the audit shows how trustworthy the basis was.
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
    decision = effective_trust(action_class, tool_fqn, trust_overrides)
    rules = [f"trust:{decision}"]

    # Low-grounding gate: weak grounding can only downgrade an auto_allow to a
    # human gate — never upgrade. Low grounding → human gate, always. `gated`
    # is true only when the gate actually changed the decision, so the audit
    # reason stays consistent with rules[].
    low_grounding = bool(grounding and grounding.get("low_confidence"))
    gated = low_grounding and decision == "auto_allow"
    if gated:
        decision = "awaiting_approval"
        rules.append("low_grounding_gate")

    state = {
        "auto_allow": "approved",
        "awaiting_approval": "awaiting_approval",
        "denied": "denied",
    }[decision]
    reason = f"trust={decision}"
    if gated:
        reason += "; gated:low_grounding"
    return {
        "allowed": decision != "denied",
        "state": state,
        "auto_execute": decision == "auto_allow",
        "reason": reason,
        "action_class": action_class,
        "rules": rules,
        "grounding": grounding,
    }
