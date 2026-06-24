"""Report assembly + rendering. The rca_v1 contract the agent must satisfy.

The agent's final answer is validated against `RcaReport`; if the model can't
reach `medium` confidence it must say so and list missing evidence — never bluff.
Rendering targets markdown now and Slack Block Kit (M3).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


class Evidence(BaseModel):
    claim: str
    source_tool: str | None = None
    raw_ref: str | None = None


class RcaReport(BaseModel):
    """rca_v1 — hypothesis + confidence + evidence chain + proposals."""

    hypothesis: str
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)
    proposals: list[str] = Field(default_factory=list)  # action ids
    next_checks: list[str] = Field(default_factory=list)
    missing_evidence: str | None = None  # required in spirit when confidence=low


# JSON schema for the reserved submit_report tool the model calls to finish.
SUBMIT_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string", "description": "Most likely root cause."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source_tool": {"type": "string"},
                    "raw_ref": {"type": "string"},
                },
                "required": ["claim"],
            },
        },
        "proposals": {"type": "array", "items": {"type": "string"}},
        "next_checks": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {
            "type": "string",
            "description": "What evidence is missing (required if confidence is low).",
        },
    },
    "required": ["hypothesis", "confidence", "evidence"],
}


def render_markdown(report: RcaReport) -> str:
    lines = [
        f"## RCA: {report.hypothesis}",
        "",
        f"**Confidence:** {report.confidence}",
        "",
        "### Evidence",
    ]
    if report.evidence:
        for i, ev in enumerate(report.evidence, 1):
            src = f" _(via {ev.source_tool})_" if ev.source_tool else ""
            ref = f" `{ev.raw_ref}`" if ev.raw_ref else ""
            lines.append(f"{i}. {ev.claim}{src}{ref}")
    else:
        lines.append("_No evidence gathered._")
    if report.missing_evidence:
        lines += ["", f"**Missing evidence:** {report.missing_evidence}"]
    if report.proposals:
        lines += ["", "### Suggested fixes (not executed)"]
        lines += [f"- proposal `{p}`" for p in report.proposals]
    if report.next_checks:
        lines += ["", "### Next checks"]
        lines += [f"- {c}" for c in report.next_checks]
    return "\n".join(lines)


def render_slack_blocks(report: RcaReport) -> list[dict[str, Any]]:
    """Block Kit rendering (used by the Slack surface in M3)."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "OpsForge RCA"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Hypothesis:* {report.hypothesis}\n*Confidence:* {report.confidence}",
            },
        },
    ]
    if report.evidence:
        ev_text = "\n".join(
            f"{i}. {ev.claim}" + (f" _(via {ev.source_tool})_" if ev.source_tool else "")
            for i, ev in enumerate(report.evidence, 1)
        )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence*\n{ev_text}"}}
        )
    if report.proposals:
        prop_text = "\n".join(f"• suggested fix `{p}`" for p in report.proposals)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": prop_text}}
        )
    return blocks
