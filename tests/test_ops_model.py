"""Step A: canonical ops model — translation, validation, starter packs (pure)."""

from __future__ import annotations

from opsforge.ops_model import (
    Incident,
    from_native,
    load_starter_mapping,
    validate_mapping,
)

# A ServiceNow-shaped native incident record (out-of-box field names + choice ids).
SNOW_RAW = {
    "number": "INC0012345",
    "short_description": "payment-svc 5xx spike",
    "priority": "1",
    "state": "2",
    "cmdb_ci": "service://payment-svc",
    "assignment_group": "SRE",
    "sla_due": "2026-06-14T20:00:00Z",
}


def test_from_native_translates_servicenow_to_canonical():
    mapping = load_starter_mapping("servicenow")
    assert mapping is not None
    inc = from_native("incident", SNOW_RAW, mapping)
    assert isinstance(inc, Incident)
    assert inc.ref == "INC0012345"
    assert inc.title == "payment-svc 5xx spike"
    assert inc.priority == "P1"  # value-mapped from "1"
    assert inc.state == "investigating"  # value-mapped from "2"
    assert inc.service_ref == "service://payment-svc"  # = graph natural_key
    assert inc.assignment_group == "SRE"
    assert inc.sla is not None and inc.sla.deadline is not None  # nested path


def test_from_native_tolerates_missing_optionals():
    inc = from_native("incident", {"number": "INC1"}, load_starter_mapping("servicenow"))
    assert inc.ref == "INC1"
    assert inc.priority is None
    assert inc.sla is None


def test_from_native_handles_nested_native_paths():
    # Jira nests under fields.* — the digger walks dotted paths and list indices.
    jira_raw = {
        "key": "OPS-7",
        "fields": {
            "summary": "db latency",
            "priority": {"name": "Highest"},
            "status": {"name": "In Progress"},
            "components": [{"name": "payments"}],
        },
    }
    inc = from_native("incident", jira_raw, load_starter_mapping("jira"))
    assert inc.ref == "OPS-7"
    assert inc.priority == "P1"
    assert inc.state == "investigating"
    assert inc.service_ref == "payments"


def test_validate_mapping_flags_missing_required_fields():
    # An ops connector needs the required incident fields mapped.
    incomplete = {"incident.ref": {"field": "number"}}
    missing = validate_mapping("servicenow", incomplete)
    assert "incident.priority" in missing
    assert "incident.state" in missing
    assert "incident.title" in missing
    assert "incident.ref" not in missing


def test_starter_packs_validate_clean():
    for kind in ("servicenow", "jira", "pagerduty"):
        mapping = load_starter_mapping(kind)
        assert mapping is not None
        assert validate_mapping(kind, mapping) == []  # ops-ready out of the box


def test_non_ops_kind_needs_no_mapping():
    assert validate_mapping("kubernetes", None) == []
