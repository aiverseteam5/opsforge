"""The connector catalog registry (A1) — capability definitions, code, not data.

The catalog is the set of connectors OpsForge CAN offer; it is DISTINCT from the
`connectors` table, which holds the *instances* a workspace has actually configured.
"ServiceNow is connectable" is a catalog entry; "this workspace connected ServiceNow
with these credentials" is an instance row.

Two honesty rules govern this module (the A1 analogue of the gate-can't-be-bypassed
discipline):
  1. The catalog never overstates capability. `implementation_status` tells the truth:
     `implemented` only for paths genuinely wired today (local-file/markdown ingest, the
     ServiceNow connector path); everything else is `stub_coming_soon` and is NOT
     connectable. A stub never resolves to a `connected` status.
  2. Per-workspace status is computed by joining the registry against THIS workspace's
     configured `connectors` instances (+ their health) and its ingested documents — for
     the CALLER's workspace only, never another's. Isolation is by an explicit
     token-derived `org_id` predicate (the org id comes solely from the validated token,
     never client input, so the predicate can only ever match the caller's own rows). Both
     signals are ALSO backstopped by the DB row-level-security net: knowledge_chunks since
     M6, and `connectors` since A1.5 (migration 0016 — FORCE RLS + the NULLIF fail-closed
     policy), so isolation is DB-enforced, with the explicit predicate kept as
     defense-in-depth. A1 is strictly read-only: it writes no instances and captures no
     credentials (that is A2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .db import scope_to_org, session_factory

AuthType = Literal["api_key", "oauth", "vault_credential", "none"]
Ingest = Literal["knowledge", "behaviour", "telemetry", "actions"]
Transport = Literal["mcp_stdio", "mcp_http", "local"]
ImplStatus = Literal["implemented", "stub_coming_soon"]
# Per-workspace status, computed (never stored on the registry):
WorkspaceStatus = Literal["available", "configured", "connected", "error", "coming_soon"]

# Zone display order — grouping reduces the cognitive load of a flat list of forty.
ZONE_ORDER = [
    "Knowledge sources",
    "System-of-record",
    "Observability",
    "Automation engines",
    "Device vendors",
    "SRE / Infrastructure",
]


class CatalogEntry(BaseModel):
    """A capability definition. Frozen — capabilities are code, not mutable data."""

    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str
    zone: str
    auth_type: AuthType
    ingests: list[Ingest]
    transport: Transport
    implementation_status: ImplStatus
    description: str
    # The `connectors.kind` an instance of this connector would carry, when one CAN exist
    # today. None when no instance path exists yet (stubs, or non-instance paths).
    instance_kind: str | None = None
    # Where this entry's live status comes from: a configured connector INSTANCE (default),
    # or the presence of ingested document CHUNKS (the local-file/markdown ingest path,
    # which is genuinely wired but is not a `connectors` instance).
    status_source: Literal["instance", "chunks"] = "instance"


class CatalogEntryStatus(BaseModel):
    """A registry entry plus this workspace's computed status (the API shape)."""

    key: str
    display_name: str
    zone: str
    auth_type: AuthType
    ingests: list[Ingest]
    transport: Transport
    implementation_status: ImplStatus
    description: str
    status: WorkspaceStatus
    connectable: bool  # false for stubs and already-connected — drives the UI affordance


# --------------------------------------------------------------------------- #
# The seed catalog — five zones of real-environment connectors, plus the existing
# wired SRE-era kinds. Status reflects REALITY: only genuinely-wired paths are
# `implemented`; everything else is honestly `stub_coming_soon`.
# --------------------------------------------------------------------------- #
CATALOG: list[CatalogEntry] = [
    # --- Knowledge sources ---
    CatalogEntry(
        key="local_files", display_name="Local files / Markdown", zone="Knowledge sources",
        auth_type="none", ingests=["knowledge"], transport="local",
        implementation_status="implemented", status_source="chunks",
        description="Ingest a server-visible folder of Markdown/text into the knowledge plane.",
    ),
    CatalogEntry(
        key="confluence", display_name="Confluence", zone="Knowledge sources",
        auth_type="api_key", ingests=["knowledge"], transport="mcp_stdio",
        implementation_status="implemented", instance_kind="confluence",
        description="Spaces & pages as knowledge sources — real, read-only (Phase B).",
    ),
    CatalogEntry(
        key="sharepoint", display_name="SharePoint", zone="Knowledge sources",
        auth_type="oauth", ingests=["knowledge"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Document libraries & sites as knowledge sources.",
    ),
    CatalogEntry(
        key="gitlab_markdown", display_name="GitLab / Markdown repos", zone="Knowledge sources",
        auth_type="api_key", ingests=["knowledge"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Runbooks & docs from Git repositories.",
    ),
    CatalogEntry(
        key="word_pdf", display_name="Word / PDF", zone="Knowledge sources",
        auth_type="none", ingests=["knowledge"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Office documents and PDFs as knowledge sources.",
    ),
    # --- System-of-record ---
    CatalogEntry(
        key="servicenow", display_name="ServiceNow", zone="System-of-record",
        auth_type="vault_credential", ingests=["behaviour", "knowledge"], transport="mcp_stdio",
        implementation_status="implemented", instance_kind="servicenow",
        description="Incident / change / problem / request / CMDB — behaviour & CMDB knowledge.",
    ),
    # --- Observability ---
    CatalogEntry(
        key="splunk", display_name="Splunk", zone="Observability",
        auth_type="api_key", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Search & alerts as telemetry signal.",
    ),
    CatalogEntry(
        key="grafana", display_name="Grafana", zone="Observability",
        auth_type="api_key", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Dashboards & alerting as telemetry signal.",
    ),
    CatalogEntry(
        key="prometheus", display_name="Prometheus", zone="Observability",
        auth_type="none", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Metrics & alert rules as telemetry signal.",
    ),
    # --- Automation engines ---
    CatalogEntry(
        key="ansible", display_name="Ansible", zone="Automation engines",
        auth_type="vault_credential", ingests=["actions"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Playbook execution as a gated action engine.",
    ),
    CatalogEntry(
        key="internal_playbooks", display_name="Internal playbooks", zone="Automation engines",
        auth_type="api_key", ingests=["actions"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="In-house automation playbooks as a gated action engine.",
    ),
    CatalogEntry(
        key="management_stack", display_name="Management-Stack APIs", zone="Automation engines",
        auth_type="vault_credential", ingests=["actions"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Management-stack control APIs as a gated action engine.",
    ),
    # --- Device vendors ---
    CatalogEntry(
        key="powerscale", display_name="PowerScale", zone="Device vendors",
        auth_type="vault_credential", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Scale-out NAS telemetry & state.",
    ),
    CatalogEntry(
        key="ppdm", display_name="PPDM", zone="Device vendors",
        auth_type="vault_credential", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="PowerProtect Data Manager telemetry & state.",
    ),
    CatalogEntry(
        key="data_domain", display_name="Data Domain", zone="Device vendors",
        auth_type="vault_credential", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Protection-storage telemetry & state.",
    ),
    CatalogEntry(
        key="idrac", display_name="iDRAC", zone="Device vendors",
        auth_type="vault_credential", ingests=["telemetry"], transport="mcp_http",
        implementation_status="stub_coming_soon",
        description="Server out-of-band management telemetry & state.",
    ),
    # --- SRE / Infrastructure (the existing wired connector kinds) ---
    CatalogEntry(
        key="aws", display_name="AWS", zone="SRE / Infrastructure",
        auth_type="vault_credential", ingests=["telemetry", "actions"], transport="mcp_http",
        implementation_status="implemented", instance_kind="aws",
        description="Cloud resource state & control as telemetry + gated actions.",
    ),
    CatalogEntry(
        key="kubernetes", display_name="Kubernetes", zone="SRE / Infrastructure",
        auth_type="vault_credential", ingests=["telemetry", "actions"], transport="mcp_http",
        implementation_status="implemented", instance_kind="kubernetes",
        description="Cluster state & control as telemetry + gated actions.",
    ),
    CatalogEntry(
        key="datadog", display_name="Datadog", zone="SRE / Infrastructure",
        auth_type="api_key", ingests=["telemetry"], transport="mcp_http",
        implementation_status="implemented", instance_kind="datadog",
        description="Monitors & metrics as telemetry signal.",
    ),
    CatalogEntry(
        key="jira", display_name="Jira", zone="SRE / Infrastructure",
        auth_type="api_key", ingests=["knowledge", "behaviour"], transport="mcp_http",
        implementation_status="implemented", instance_kind="jira",
        description="Issues & projects as knowledge and behaviour signal.",
    ),
    CatalogEntry(
        key="pagerduty", display_name="PagerDuty", zone="SRE / Infrastructure",
        auth_type="api_key", ingests=["behaviour"], transport="mcp_http",
        implementation_status="implemented", instance_kind="pagerduty",
        description="Incidents & on-call as behaviour signal.",
    ),
    CatalogEntry(
        key="slack", display_name="Slack", zone="SRE / Infrastructure",
        auth_type="oauth", ingests=["actions"], transport="mcp_http",
        implementation_status="implemented", instance_kind="slack",
        description="Channels as a surface for notifications & gated actions.",
    ),
]

_BY_KEY = {e.key: e for e in CATALOG}


def _resolve_status(
    entry: CatalogEntry,
    *,
    instance_statuses_by_kind: dict[str, set[str]],
    has_documents: bool,
) -> WorkspaceStatus:
    """Compute this workspace's honest status for one entry. A stub is ALWAYS
    `coming_soon` (it can never be connected); an implemented entry resolves from its
    real signal (a configured instance's health, or — for local files — ingested docs)."""
    if entry.implementation_status == "stub_coming_soon":
        return "coming_soon"
    if entry.status_source == "chunks":
        # local-file/markdown ingest: genuinely connected once knowledge has been ingested.
        return "connected" if has_documents else "available"
    statuses = instance_statuses_by_kind.get(entry.instance_kind or "", set())
    if not statuses:
        return "available"
    if "healthy" in statuses:
        return "connected"
    if "unhealthy" in statuses:
        return "error"
    return "configured"  # configured but never health-checked (e.g. 'unknown')


async def _workspace_signals(org_id: Any) -> tuple[dict[str, set[str]], bool]:
    """One workspace-scoped read of the two status signals this workspace exposes: the
    status of its configured connector instances (by kind) and whether it has ingested
    documents. `org_id` is the caller's token-derived org (never client input). Both reads
    are backstopped by FORCE RLS (knowledge_chunks since M6, connectors since A1.5/migration
    0016), with the explicit `org_id` predicate kept as defense-in-depth. scope_to_org sets
    the GUC the policies read."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        rows = (
            await s.execute(
                text("SELECT kind, status FROM connectors WHERE org_id = :o"),
                {"o": str(org_id)},
            )
        ).all()
        has_docs = (
            await s.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM knowledge_chunks "
                    "WHERE org_id = :o AND source_kind = 'document')"
                ),
                {"o": str(org_id)},
            )
        ).scalar_one()
    by_kind: dict[str, set[str]] = {}
    for r in rows:
        by_kind.setdefault(r.kind, set()).add(r.status)
    return by_kind, bool(has_docs)


def _with_status(entry: CatalogEntry, status: WorkspaceStatus) -> CatalogEntryStatus:
    # Connectable only when there is a config flow to start AND it is not already wired:
    # stubs are never connectable; an already-`connected` entry has nothing to connect.
    connectable = entry.implementation_status == "implemented" and status in (
        "available", "error",
    )
    return CatalogEntryStatus(
        key=entry.key, display_name=entry.display_name, zone=entry.zone,
        auth_type=entry.auth_type, ingests=entry.ingests, transport=entry.transport,
        implementation_status=entry.implementation_status, description=entry.description,
        status=status, connectable=connectable,
    )


async def catalog_by_zone(org_id: Any) -> list[dict[str, Any]]:
    """The full registry grouped by zone, each entry carrying THIS workspace's status.
    Always populated (the registry is static) — the page never opens on an empty state."""
    by_kind, has_docs = await _workspace_signals(org_id)
    entries = [
        _with_status(
            e, _resolve_status(e, instance_statuses_by_kind=by_kind, has_documents=has_docs)
        )
        for e in CATALOG
    ]
    zones: list[dict[str, Any]] = []
    for zone in ZONE_ORDER:
        group = [e for e in entries if e.zone == zone]
        if group:
            zones.append({"zone": zone, "connectors": [e.model_dump() for e in group]})
    return zones


def _config_requirements(entry: CatalogEntry) -> list[str]:
    """What A2 needs to capture — the flat name list (kept for back-compat; the structured
    `config_fields` below is what the A2 form actually renders)."""
    return {
        "api_key": ["endpoint", "api_key"],
        "oauth": ["oauth_client_id", "oauth_client_secret", "endpoint"],
        "vault_credential": ["endpoint", "credential_ref"],
        "none": [],
    }[entry.auth_type]


class ConfigField(BaseModel):
    """A declared config input the A2 form renders. `secret=True` fields are the
    credential: the UI renders them WRITE-ONLY (password, never prefilled, never sent back),
    and they alone flow into the Fernet vault. The backend is the source of truth for what
    is a secret — the UI never guesses from a field name."""

    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str | None = None


def config_fields_for(entry: CatalogEntry) -> list[ConfigField]:
    """The structured config form for a connector, DECLARED (not hardcoded per connector) —
    driven by its transport + auth_type. local-files is the ingest path (a folder, no
    credential); instance connectors take an endpoint + their auth's secret + an allowlist."""
    if entry.transport == "local":
        # local-files: a folder to ingest, no credential, no instance row.
        return [ConfigField(name="path", label="Folder path (server-visible)",
                            placeholder="/data/runbooks")]
    if entry.key == "confluence":
        # The real Confluence knowledge source: the operator gives their Confluence base URL,
        # an API token (Cloud email:api_token, or a Server/DC PAT — secret), and a space key.
        return [
            ConfigField(name="endpoint", label="Confluence base URL",
                        placeholder="https://your-org.atlassian.net"),
            ConfigField(name="api_key", label="API token (email:token for Cloud)", secret=True),
            ConfigField(name="space", label="Space key", required=False,
                        placeholder="OPS"),
        ]
    fields = [ConfigField(name="endpoint", label="Endpoint / URL or command",
                          placeholder="https://… or a stdio command")]
    if entry.auth_type == "api_key":
        fields.append(ConfigField(name="api_key", label="API key", secret=True))
    elif entry.auth_type == "vault_credential":
        fields.append(ConfigField(name="credential", label="Credential / token", secret=True))
    elif entry.auth_type == "oauth":
        fields.append(ConfigField(name="client_id", label="OAuth client ID"))
        fields.append(ConfigField(name="client_secret", label="OAuth client secret", secret=True))
    fields.append(ConfigField(name="tool_allowlist", label="Tool allowlist (comma-separated)",
                              secret=False, required=False,
                              placeholder="get_incident, search_incidents"))
    return fields


def requires_credential(kind: str | None) -> bool:
    """Whether a connector of this kind needs a credential (its catalog auth_type is not
    'none'). Used to fail CLOSED at create — a credential-bearing connector must not flip to
    'connected' with an empty vault. Unknown kinds (not in the catalog) are permissive."""
    entry = next((e for e in CATALOG if e.instance_kind == kind), None)
    return entry is not None and entry.auth_type != "none"


async def _instance_id_for_kind(org_id: Any, kind: str | None) -> str | None:
    """This workspace's configured connector instance id for a catalog entry's kind (the
    one the A2 form edits/tests/disconnects), or None if not yet configured. RLS-scoped.
    Prefers a HEALTHY instance so the id matches the (healthy-wins) status badge — the form
    then edits the same instance the operator saw, not an older broken sibling."""
    if not kind:
        return None
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        row = (
            await s.execute(
                text("SELECT id FROM connectors WHERE org_id = :o AND kind = :k "
                     "ORDER BY (status <> 'healthy'), created_at LIMIT 1"),
                {"o": str(org_id), "k": kind},
            )
        ).first()
    return str(row.id) if row else None


async def catalog_detail(org_id: Any, key: str) -> dict[str, Any] | None:
    """One connector's detail + its declared config fields + (if configured here) the
    instance id the A2 form edits/tests/disconnects. Read-only; never returns a credential."""
    entry = _BY_KEY.get(key)
    if entry is None:
        return None
    by_kind, has_docs = await _workspace_signals(org_id)
    status = _resolve_status(entry, instance_statuses_by_kind=by_kind, has_documents=has_docs)
    out = _with_status(entry, status).model_dump()
    out["config_requirements"] = _config_requirements(entry)
    out["config_fields"] = [f.model_dump() for f in config_fields_for(entry)]
    out["instance_kind"] = entry.instance_kind
    out["instance_id"] = await _instance_id_for_kind(org_id, entry.instance_kind)
    return out
