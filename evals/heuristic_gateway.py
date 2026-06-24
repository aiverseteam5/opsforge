"""An offline ModelGateway stand-in for evals/CI (no LLM key required).

It is NOT a model — it is a deterministic harness that drives the *real* agent
loop: it issues real read-only tool calls, reads the assembled context + tool
results, optionally proposes a (non-executed) fix, then submits a valid rca_v1
report citing the most recent deploy. This proves the pipeline end-to-end. A
certified model replaces it by changing one config string.
"""

from __future__ import annotations

import re
from typing import Any

from opsforge.gateway import ChatResult, ToolCall

_CHANGE_REF_RE = re.compile(r"([A-Za-z0-9_./-]+@rev\d+)")

# Reasonable default args per tool for the fixture topology.
_DEFAULT_ARGS = {
    "kubernetes__get_events": {"namespace": "prod"},
    "kubernetes__list_deployments": {"namespace": "prod"},
    "kubernetes__list_pods": {"namespace": "prod"},
    "kubernetes__get_logs": {"pod": "payment-svc-8a2b"},
    "datadog__query_metrics": {"query": "rate(http_5xx[5m])"},
    "datadog__list_targets": {},
}
_INVESTIGATE_ORDER = [
    "kubernetes__get_events",
    "kubernetes__list_deployments",
    "kubernetes__get_logs",
    "datadog__query_metrics",
]


class HeuristicGateway:
    def __init__(self) -> None:
        self._investigated = False
        self._proposed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> ChatResult:
        names = {t["function"]["name"] for t in (tools or [])}

        # Turn 1: gather evidence with the available read-only tools.
        if not self._investigated:
            self._investigated = True
            calls = [
                ToolCall(id=f"c{i}", name=n, arguments=_DEFAULT_ARGS.get(n, {}))
                for i, n in enumerate(_INVESTIGATE_ORDER)
                if n in names
            ]
            if calls:
                return ChatResult(
                    text="Correlating the change timeline with pod telemetry.",
                    tool_calls=calls,
                )

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        match = _CHANGE_REF_RE.search(system or "")
        change_ref = match.group(1) if match else "unknown"

        # Turn 2: propose a (non-executed) rollback, if the skill allows it.
        if not self._proposed and "propose_action" in names:
            self._proposed = True
            return ChatResult(
                text=f"Recent deploy {change_ref} lines up with the failures; "
                "proposing a rollback for human approval.",
                tool_calls=[
                    ToolCall(
                        id="p0",
                        name="propose_action",
                        arguments={
                            "tool": "kubernetes.rollback_deploy",
                            "params": {"deployment": "payment-svc", "namespace": "prod"},
                            "target_ref": "k8s://prod/deploy/payment-svc",
                            "rationale": f"Roll back {change_ref}",
                        },
                    )
                ],
            )

        # Final turn: submit the rca_v1 report.
        proposals = _collect_action_ids(messages)
        report = {
            "hypothesis": (
                f"payment-svc is failing because of the recent deploy {change_ref}, "
                "which exhausted the database connection pool and put the pod into "
                "CrashLoopBackOff."
            ),
            "confidence": "high",
            "evidence": [
                {
                    "claim": f"A deploy {change_ref} landed just before the failures.",
                    "source_tool": "kubernetes.list_deployments",
                    "raw_ref": change_ref,
                },
                {
                    "claim": "Pod payment-svc-8a2b is in CrashLoopBackOff (7 restarts).",
                    "source_tool": "kubernetes.get_events",
                    "raw_ref": "payment-svc-8a2b",
                },
                {
                    "claim": "Logs show 'connection pool exhausted'.",
                    "source_tool": "kubernetes.get_logs",
                    "raw_ref": "payment-svc-8a2b",
                },
            ],
            "proposals": proposals,
            "next_checks": [
                "Approve the rollback and confirm the 5xx rate subsides.",
            ],
        }
        if "submit_report" in names:
            return ChatResult(
                text="Submitting RCA.",
                tool_calls=[ToolCall(id="submit", name="submit_report", arguments=report)],
            )
        import json

        return ChatResult(text=json.dumps(report))

    async def embedding(self, texts: list[str], model: str) -> list[list[float]]:
        return [[0.0] * 1536 for _ in texts]


def _collect_action_ids(messages: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            found = re.search(r'"action_id":\s*"([0-9a-f-]+)"', m["content"])
            if found:
                ids.append(found.group(1))
    return ids
