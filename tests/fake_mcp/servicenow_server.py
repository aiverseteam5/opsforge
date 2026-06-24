"""Fake ServiceNow MCP server (stdio) — drives the ITSM connector + mapping tests.

Returns NATIVE ServiceNow field names (number, short_description, priority as
choice ids "1".."5", state as ints, cmdb_ci, sla_due) so the connector's
field_mapping has real translation to do. `describe_schema` powers
POST /connectors/{id}/discover. Incident state is in-process (add_work_note /
update_incident mutate it) — enough for write-back tests.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-servicenow")

# Seeded incident (native shape). cmdb_ci is the SAME natural_key the K8s graph uses.
_INCIDENTS: dict[str, dict] = {
    "INC0012345": {
        "number": "INC0012345",
        "short_description": "payment-svc throwing 5xx after deploy",
        "priority": "1",
        "state": "2",
        "cmdb_ci": "service://payment-svc",
        "assignment_group": "SRE",
        "sla_due": "2026-06-14T20:00:00Z",
        "work_notes": [],
    }
}

_CIS = {
    "service://payment-svc": {
        "natural_key": "service://payment-svc",
        "name": "payment-svc",
        "depends_on": ["service://ledger-svc", "service://auth-svc"],
    }
}


@mcp.tool()
def describe_schema() -> dict:
    return {
        "tables": {
            "incident": {
                "fields": [
                    "number", "short_description", "priority", "state",
                    "cmdb_ci", "assignment_group", "sla_due",
                ],
                "choices": {
                    "priority": {"1": "Critical", "2": "High", "3": "Moderate",
                                 "4": "Low", "5": "Planning"},
                    "state": {"1": "New", "2": "In Progress", "3": "On Hold",
                              "6": "Resolved", "7": "Closed"},
                },
            },
            "cmdb_ci": {"fields": ["name", "natural_key", "depends_on"]},
        }
    }


# Group directory (M7.6 Job B). The AUTHORITATIVE identity of a group is its sys_id —
# a reference an attacker cannot mint by typing a free-text assignment_group. A group
# not in the directory has NO verified identity; an ambiguous group resolves to a
# conflict and also yields no verified identity (fail safe).
_GROUP_DIRECTORY: dict[str, str] = {
    "sre-payments": "grp-sys-0001",
    "sre-checkout": "grp-sys-0002",
    "platform-oncall": "grp-sys-0003",
    "auto://nightly-job": "grp-sys-0099",
}
_AMBIGUOUS_GROUPS: set[str] = {"ambiguous-group"}  # maps to >1 identity → unresolved


def _resolve_identity(group: str | None) -> str | None:
    """The verified directory identity (sys_id) for a group, or None if the group is
    unknown (free-text the attacker invented) or ambiguous (fail safe)."""
    if not group or group in _AMBIGUOUS_GROUPS:
        return None
    return _GROUP_DIRECTORY.get(group)


@mcp.tool()
def resolve_group_identity(group: str = "") -> dict:
    """Authoritative identity lookup: a free-text group → its directory sys_id, or null
    if it is not a real group / is ambiguous. The connector uses this to bind origin to
    verified identity (M7.6)."""
    return {"group": group, "identity": _resolve_identity(group)}


# Resolved ticket history (M7.5 — the real behaviour signal). assignment_group is the
# ORIGIN; assignment_group_id is its VERIFIED directory identity (M7.6). deploy-rollback
# is a GENUINE pattern (three distinct teams resolved it the same way); cache-flush is
# single-origin VOLUME (one automated job, repeated) that must NOT count as a pattern.
_RESOLVED: list[dict] = [
    {"number": "INC0020001", "process_key": "deploy-rollback", "assignment_group": "sre-payments",
     "resolution": "rolled back by draining the node and redeploying the prior image",
     "resolved_at": "2026-05-02T10:00:00Z"},
    {"number": "INC0020002", "process_key": "deploy-rollback", "assignment_group": "sre-checkout",
     "resolution": "drained the node then redeployed the previous image to roll back",
     "resolved_at": "2026-05-10T14:00:00Z"},
    {"number": "INC0020003", "process_key": "deploy-rollback", "assignment_group": "platform-oncall",  # noqa: E501
     "resolution": "rollback: drain node, redeploy the prior image, verify health",
     "resolved_at": "2026-05-18T09:00:00Z"},
    {"number": "INC0030001", "process_key": "cache-flush", "assignment_group": "auto://nightly-job",
     "resolution": "nightly job flushed the cache automatically",
     "resolved_at": "2026-05-01T02:00:00Z"},
    {"number": "INC0030002", "process_key": "cache-flush", "assignment_group": "auto://nightly-job",
     "resolution": "nightly job flushed the cache automatically",
     "resolved_at": "2026-05-02T02:00:00Z"},
    {"number": "INC0030003", "process_key": "cache-flush", "assignment_group": "auto://nightly-job",
     "resolution": "nightly job flushed the cache automatically",
     "resolved_at": "2026-05-03T02:00:00Z"},
]


@mcp.tool()
def list_resolved_incidents(since_days: int = 90) -> list[dict]:
    """Resolved ticket history → behaviour observations (M7.5/M7.6). Each carries its
    origin (assignment_group), the VERIFIED directory identity of that group
    (assignment_group_id, or null if unresolved/ambiguous), resolution text, and
    resolved_at."""
    return [{**t, "assignment_group_id": _resolve_identity(t.get("assignment_group"))}
            for t in _RESOLVED]


@mcp.tool()
def get_incident(number: str) -> dict:
    return _INCIDENTS.get(number, {"error": f"no incident {number}"})


@mcp.tool()
def search_incidents(service: str = "", open_only: bool = True) -> list[dict]:
    out = []
    for inc in _INCIDENTS.values():
        if open_only and inc["state"] in ("6", "7"):
            continue
        if service and service not in inc["cmdb_ci"]:
            continue
        out.append({"number": inc["number"], "short_description": inc["short_description"],
                    "cmdb_ci": inc["cmdb_ci"], "priority": inc["priority"]})
    return out


@mcp.tool()
def get_related_cis(ci: str) -> list[dict]:
    root = _CIS.get(ci)
    if not root:
        return []
    result = [root]
    for dep in root.get("depends_on", []):
        result.append({"natural_key": dep, "name": dep.split("/")[-1]})
    return result


@mcp.tool()
def get_sla(number: str) -> dict:
    inc = _INCIDENTS.get(number, {})
    return {"sla_due": inc.get("sla_due"), "breach_risk": "at_risk"}


@mcp.tool()
def add_work_note(number: str, note: str) -> dict:
    inc = _INCIDENTS.get(number)
    if not inc:
        return {"ok": False, "error": "no such incident"}
    inc["work_notes"].append(note)
    return {"ok": True, "number": number, "note_count": len(inc["work_notes"])}


@mcp.tool()
def update_incident(number: str, fields: dict) -> dict:
    inc = _INCIDENTS.get(number)
    if not inc:
        return {"ok": False, "error": "no such incident"}
    inc.update(fields)
    return {"ok": True, "number": number, "state": inc["state"]}


@mcp.tool()
def create_change(short_description: str, change_type: str = "normal") -> dict:
    return {"ok": True, "number": "CHG0001001", "type": change_type}


if __name__ == "__main__":
    mcp.run()
