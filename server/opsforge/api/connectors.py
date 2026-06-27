"""Connectors API: CRUD + test-connection. Credentials are never serialized out."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..catalog import requires_credential
from ..connectors import (
    _decrypt_credentials,
    discover,
    health_check,
    load_connector,
    record_health,
)
from ..db import record_audit, scope_to_org, session_factory
from ..ops_model import load_starter_mapping, validate_mapping
from ..security import Principal, encrypt, require_token

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

ConnectorKind = Literal[
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack",
    "confluence", "custom",
]


def _confluence_env(form: dict[str, str]) -> dict[str, str]:
    """Translate the Confluence config-form fields to the env keys the MCP server reads. The
    secret (api_key) only overwrites the token when actually supplied (blank = keep)."""
    out: dict[str, str] = {}
    if form.get("api_key"):
        out["CONFLUENCE_TOKEN"] = form["api_key"]
    if "space" in form:
        out["CONFLUENCE_SPACE"] = form.get("space", "")
    return out


def _map_knowledge_connector(body: ConnectorCreate) -> ConnectorCreate:
    """Phase B: a shipped knowledge connector (Confluence) wraps an in-repo stdio MCP server.
    The operator gives their base URL + token + space; we point the connector at the MCP
    server (the fixed `endpoint`/`transport`) and move the base URL/token/space into the vault
    env the server reads at spawn — so the credential is vaulted, never `.env`, never the
    connector endpoint."""
    if body.kind != "confluence":
        return body
    import sys

    return body.model_copy(update={
        # NB: the spawn endpoint embeds sys.executable; in the deployed container that path has
        # no spaces. (A host install at a spaced Python path would need a structured-argv spawn
        # — fails CLOSED to 'unhealthy', never false-connected.)
        "endpoint": f"{sys.executable} -m opsforge.sources.confluence_mcp",
        "transport": "stdio",  # always stdio — never trust a client-supplied transport here
        "credentials": {
            "CONFLUENCE_BASE_URL": body.endpoint,
            "CONFLUENCE_TOKEN": (body.credentials or {}).get("api_key", ""),
            "CONFLUENCE_SPACE": (body.credentials or {}).get("space", ""),
        },
        "tool_allowlist": ["list_documents", "verify_credential"],
    })


class ConnectorCreate(BaseModel):
    name: str
    kind: ConnectorKind
    transport: Literal["stdio", "http"]
    endpoint: str
    tool_allowlist: list[str] = Field(default_factory=list)
    # Declarative native→canonical map (ops connectors). Defaults to the kind's
    # starter pack when omitted; editable later via PUT /{id}/mapping.
    field_mapping: dict | None = None
    # Secret env vars (stdio) or headers (http). Encrypted at rest; never returned.
    credentials: dict[str, str] | None = None


class ConnectorOut(BaseModel):
    id: UUID
    name: str
    kind: str
    transport: str
    endpoint: str
    tool_allowlist: list[str]
    field_mapping: dict | None
    discovered_schema: dict | None
    status: str
    last_health_at: datetime | None
    created_at: datetime


class TestResult(BaseModel):
    status: str
    tools: list[str] | None = None
    error: str | None = None


_SELECT_COLS = (
    "id, name, kind, transport, endpoint, tool_allowlist, field_mapping, "
    "discovered_schema, status, last_health_at, created_at"
)


@router.get("", response_model=list[ConnectorOut])
async def list_connectors(principal: Principal = Depends(require_token)):
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        rows = (
            await s.execute(
                text(
                    f"SELECT {_SELECT_COLS} FROM connectors "
                    "WHERE org_id = :org ORDER BY created_at"
                ),
                {"org": principal.org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


@router.post("", response_model=ConnectorOut, status_code=status.HTTP_201_CREATED)
async def create_connector(
    body: ConnectorCreate, principal: Principal = Depends(require_token)
):
    if principal.role not in ("admin",):
        raise HTTPException(status_code=403, detail="connector management requires admin role")
    # Fail CLOSED: a credential-bearing connector must not be created (and flip to
    # 'connected') with an empty vault. Reachability alone is not a configured credential.
    if requires_credential(body.kind) and not body.credentials:
        raise HTTPException(
            status_code=400,
            detail=f"a {body.kind} connector requires a credential",
        )
    body = _map_knowledge_connector(body)
    creds_enc = encrypt(json.dumps(body.credentials)) if body.credentials else None
    # Default an ops connector's mapping to the bundled starter pack for its kind.
    mapping = body.field_mapping or load_starter_mapping(body.kind)
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        row = (
            await s.execute(
                text(
                    "INSERT INTO connectors "
                    "(org_id, name, kind, transport, endpoint, credentials_enc, "
                    " tool_allowlist, field_mapping, status) "
                    "VALUES (:org, :name, :kind, :transport, :endpoint, :creds, "
                    " CAST(:allow AS jsonb), CAST(:mapping AS jsonb), 'unknown') "
                    f"RETURNING {_SELECT_COLS}"
                ),
                {
                    "org": principal.org_id,
                    "name": body.name,
                    "kind": body.kind,
                    "transport": body.transport,
                    "endpoint": body.endpoint,
                    "creds": creds_enc,
                    "allow": json.dumps(body.tool_allowlist),
                    "mapping": json.dumps(mapping) if mapping else None,
                },
            )
        ).one()
    out = dict(row._mapping)

    # Immediately health-check the new connector and persist the result.
    connector = await load_connector(out["id"], principal.org_id)
    if connector is not None:
        result = await health_check(connector)
        await record_health(out["id"], result["status"], principal.org_id)
        out["status"] = result["status"]
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id,
        actor,
        "connector.created",
        subject_ref=str(out["id"]),
        detail={"name": body.name, "kind": body.kind},
    )
    return out


@router.post("/{connector_id}/test", response_model=TestResult)
async def test_connector(
    connector_id: UUID, principal: Principal = Depends(require_token)
):
    connector = await load_connector(connector_id, principal.org_id)
    if connector is None or str(connector["org_id"]) != principal.org_id:
        raise HTTPException(status_code=404, detail="connector not found")
    result = await health_check(connector)
    await record_health(connector_id, result["status"], principal.org_id)
    return result


class MappingBody(BaseModel):
    field_mapping: dict


def _actor(principal: Principal) -> str:
    return f"user:{principal.user_id}" if principal.user_id else "system"


@router.post("/{connector_id}/discover")
async def discover_connector(
    connector_id: UUID, principal: Principal = Depends(require_token)
):
    """GAP-1: introspect the connector's native schema → cache discovered_schema."""
    connector = await load_connector(connector_id, principal.org_id)
    if connector is None or str(connector["org_id"]) != principal.org_id:
        raise HTTPException(status_code=404, detail="connector not found")
    schema = await discover(connector)
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        await s.execute(
            text(
                "UPDATE connectors SET discovered_schema = CAST(:sc AS jsonb) "
                "WHERE id = :id AND org_id = :org"
            ),
            {"sc": json.dumps(schema), "id": connector_id, "org": principal.org_id},
        )
    await record_audit(
        principal.org_id, _actor(principal), "connector.discovered",
        subject_ref=str(connector_id), detail={"tables": list(schema.get("tables", {}))},
    )
    return {"discovered_schema": schema}


@router.put("/{connector_id}/mapping")
async def set_mapping(
    connector_id: UUID,
    body: MappingBody,
    principal: Principal = Depends(require_token),
):
    """GAP-1: set + validate field_mapping against the canonical ops model.

    The connector is "ops-ready" only when its mapping validates (config, no code).
    """
    connector = await load_connector(connector_id, principal.org_id)
    if connector is None or str(connector["org_id"]) != principal.org_id:
        raise HTTPException(status_code=404, detail="connector not found")
    missing = validate_mapping(
        connector["kind"], body.field_mapping, connector.get("discovered_schema")
    )
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"error": "mapping incomplete", "missing": missing},
        )
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        await s.execute(
            text(
                "UPDATE connectors SET field_mapping = CAST(:fm AS jsonb) "
                "WHERE id = :id AND org_id = :org"
            ),
            {"fm": json.dumps(body.field_mapping), "id": connector_id,
             "org": principal.org_id},
        )
    await record_audit(
        principal.org_id, _actor(principal), "connector.mapped",
        subject_ref=str(connector_id), detail={"keys": sorted(body.field_mapping)},
    )
    return {"status": "ops-ready", "field_mapping": body.field_mapping}


class ConnectorUpdate(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    tool_allowlist: list[str] | None = None
    # Re-capture credential: a non-empty dict OVERWRITES the vault; omitted/None/empty KEEPS
    # the existing one. Write-only — the stored credential is never returned, so the form
    # sends a fresh secret or nothing. The old value is never displayed or echoed.
    credentials: dict[str, str] | None = None


@router.patch("/{connector_id}", response_model=ConnectorOut)
async def update_connector(
    connector_id: UUID,
    body: ConnectorUpdate,
    principal: Principal = Depends(require_token),
):
    """Edit an existing connector's config and/or rotate its credential. Org-scoped (the
    A1.5 FORCE-RLS net + scope_to_org + explicit predicate); a write can only ever touch the
    caller's own workspace. The credential is write-only and never returned/audited."""
    if principal.role not in ("admin",):
        raise HTTPException(status_code=403, detail="connector management requires admin role")
    existing = await load_connector(connector_id, principal.org_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="connector not found")
    # For a shipped knowledge connector (Confluence) the form's `endpoint` is the BASE URL
    # (a vault env value) and the form keys (api_key/space) are the env keys the MCP server
    # reads — NOT the connector endpoint (which is the fixed spawn command). The SAME mapping
    # MUST run on edit, or a credential rotation lands under a dead key and the OLD token
    # silently stays in effect (a false-green rotation).
    is_confluence = existing.get("kind") == "confluence"
    sets: list[str] = []
    params: dict[str, object] = {"id": connector_id, "org": principal.org_id}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name
    if body.endpoint is not None and not is_confluence:
        sets.append("endpoint = :endpoint")  # confluence endpoint stays the spawn command
        params["endpoint"] = body.endpoint
    if body.tool_allowlist is not None and not is_confluence:
        sets.append("tool_allowlist = CAST(:allow AS jsonb)")  # confluence allowlist is fixed
        params["allow"] = json.dumps(body.tool_allowlist)

    # Translate the supplied credential fields to the env namespace the connector reads.
    incoming: dict[str, str] = {}
    if is_confluence:
        if body.endpoint is not None:
            incoming["CONFLUENCE_BASE_URL"] = body.endpoint  # new base URL → the vault env
        incoming.update(_confluence_env(body.credentials or {}))
    elif body.credentials:
        incoming = dict(body.credentials)
    credential_rotated = bool(body.credentials)
    if incoming:
        # MERGE the supplied keys into the existing envelope (decrypt → overlay → re-encrypt)
        # so rotating ONE field of a multi-field credential does not drop the un-supplied keys.
        merged = {**_decrypt_credentials(existing.get("credentials_enc")), **incoming}
        sets.append("credentials_enc = :creds")
        params["creds"] = encrypt(json.dumps(merged))
    # A connectivity-affecting change (endpoint/base-url or credential) INVALIDATES the prior
    # health verdict — reset to 'unknown' so the badge stops claiming 'connected' until a
    # fresh test. (A name/allowlist-only edit leaves the verdict intact.)
    if body.endpoint is not None or incoming:
        sets.append("status = 'unknown'")
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        if sets:
            row = (
                await s.execute(
                    text(f"UPDATE connectors SET {', '.join(sets)} "
                         f"WHERE id = :id AND org_id = :org RETURNING {_SELECT_COLS}"),
                    params,
                )
            ).one()
        else:
            row = (
                await s.execute(
                    text(f"SELECT {_SELECT_COLS} FROM connectors WHERE id = :id AND org_id = :org"),
                    {"id": connector_id, "org": principal.org_id},
                )
            ).one()
    # audit records WHICH fields changed (names only) + whether the credential rotated —
    # NEVER the credential value.
    changed = [f for f, p in (("name", body.name), ("endpoint", body.endpoint),
                              ("tool_allowlist", body.tool_allowlist)) if p is not None]
    await record_audit(
        principal.org_id, _actor(principal), "connector.updated",
        subject_ref=str(connector_id),
        detail={"fields": changed, "credential_rotated": credential_rotated},
    )
    return dict(row._mapping)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: UUID, principal: Principal = Depends(require_token)
):
    if principal.role not in ("admin",):
        raise HTTPException(status_code=403, detail="connector management requires admin role")
    async with session_factory().begin() as s:
        await scope_to_org(s, principal.org_id)
        res = await s.execute(
            text(
                "DELETE FROM connectors WHERE id = :id AND org_id = :org"
            ),
            {"id": connector_id, "org": principal.org_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="connector not found")
