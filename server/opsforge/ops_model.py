"""Canonical ops model — the vendor-neutral vocabulary the agent + policy reason over.

Incident / Change / Problem / Service are first-class (OpsForge is an *ops*
product). Specific tools (ServiceNow, Jira SM, PagerDuty) are NOT — they are
connectors that declare a `field_mapping` translating their native schema to
these canonical objects. Core never sees a vendor field name.

This module is (almost) pure: the Pydantic models + `from_native` + `validate_mapping`
do no I/O. `load_starter_mapping` is the single exception — it reads a bundled
YAML starter pack — and is kept here so the mapping vocabulary lives in one place.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

Priority = Literal["P1", "P2", "P3", "P4"]
IncidentState = Literal["new", "investigating", "identified", "resolved", "closed"]
ChangeType = Literal["normal", "emergency", "standard"]
BreachRisk = Literal["none", "at_risk", "breached"]


class Sla(BaseModel):
    deadline: datetime.datetime | None = None
    breach_risk: BreachRisk = "none"


class WorkNote(BaseModel):
    author: str | None = None
    body: str
    created_at: datetime.datetime | None = None


class Incident(BaseModel):
    ref: str
    title: str | None = None
    priority: Priority | None = None
    state: IncidentState | None = None
    service_ref: str | None = None  # natural_key shared with the operational graph
    assignment_group: str | None = None
    sla: Sla | None = None
    work_notes: list[WorkNote] = Field(default_factory=list)


class Change(BaseModel):
    ref: str
    type: ChangeType = "normal"
    summary: str | None = None
    target_keys: list[str] = Field(default_factory=list)
    state: str | None = None


class Problem(BaseModel):
    ref: str
    title: str | None = None
    state: str | None = None


class ServiceCI(BaseModel):
    natural_key: str
    name: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)


_MODELS: dict[str, type[BaseModel]] = {
    "incident": Incident,
    "change": Change,
    "problem": Problem,
    "service": ServiceCI,
}

# The canonical fields a connector MUST map for each object before it is "ops-ready".
REQUIRED_FIELDS: dict[str, list[str]] = {
    "incident": ["ref", "priority", "state", "title"],
    "change": ["ref", "type"],
    "problem": ["ref"],
    "service": ["natural_key"],
}

# Which canonical objects an ITSM/ops connector kind is expected to provide.
KIND_OBJECTS: dict[str, list[str]] = {
    "servicenow": ["incident"],
    "jira": ["incident"],
    "pagerduty": ["incident"],
}


# --------------------------------------------------------------------------- #
# Translation: native record + field_mapping -> canonical object
# --------------------------------------------------------------------------- #
def _dig(raw: Any, dotted: str) -> Any:
    """Read a possibly-nested native value, e.g. 'fields.priority.name' or
    'components.0.name' (numeric segments index into lists)."""
    cur = raw
    for part in dotted.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _set_path(out: dict[str, Any], path: list[str], value: Any) -> None:
    cur = out
    for part in path[:-1]:
        cur = cur.setdefault(part, {})
    cur[path[-1]] = value


def from_native(
    obj_type: str, raw: dict[str, Any], field_mapping: dict[str, Any] | None
) -> BaseModel:
    """Translate a native record into a canonical object using `field_mapping`.

    Mapping shape (keyed by canonical dotted path under the object):
      {"incident.priority": {"field": "u_priority", "values": {"1": "P1"}},
       "incident.sla.deadline": {"field": "sla_due"}}
    A spec may use `const` instead of `field` for a fixed value.
    """
    model = _MODELS[obj_type]
    out: dict[str, Any] = {}
    prefix = f"{obj_type}."
    for key, spec in (field_mapping or {}).items():
        if not key.startswith(prefix) or not isinstance(spec, dict):
            continue
        path = key[len(prefix):].split(".")
        if "const" in spec:
            value: Any = spec["const"]
        else:
            native = spec.get("field")
            value = _dig(raw, native) if native else None
        values_map = spec.get("values")
        if values_map and value is not None and str(value) in values_map:
            value = values_map[str(value)]
        if value is None:
            continue
        _set_path(out, path, value)
    return model.model_validate(out)


# --------------------------------------------------------------------------- #
# Validation: is a connector's mapping complete enough to be ops-ready?
# --------------------------------------------------------------------------- #
def validate_mapping(
    kind: str,
    field_mapping: dict[str, Any] | None,
    discovered_schema: dict[str, Any] | None = None,
) -> list[str]:
    """Return the list of missing required mappings (empty list == valid)."""
    mapping = field_mapping or {}
    missing: list[str] = []
    for obj in KIND_OBJECTS.get(kind, []):
        for field in REQUIRED_FIELDS[obj]:
            if f"{obj}.{field}" not in mapping:
                missing.append(f"{obj}.{field}")
    return missing


# --------------------------------------------------------------------------- #
# Starter packs (bundled out-of-box vendor schema -> canonical)
# --------------------------------------------------------------------------- #
MAPPINGS_DIR = Path("mappings")


def load_starter_mapping(
    kind: str, mappings_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """Load the bundled default field_mapping for a connector kind, if any."""
    path = Path(mappings_dir or MAPPINGS_DIR) / f"{kind}.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
