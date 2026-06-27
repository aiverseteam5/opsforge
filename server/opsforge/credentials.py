"""JIT credential resolver — the single chokepoint for credential materialisation.

Doctrine #8 extension: for `static` connectors the existing Fernet vault path
is used unchanged. For OIDC / AppRole connectors a short-lived credential is
minted at spawn time, used for the duration of one MCP session, then discarded —
the materialised secret is NEVER persisted.

A `credential_leases` row (metadata only, no token) is recorded for every
non-static issuance so the trust ladder and audit trail stay complete.

Supported providers
-------------------
static      Current behaviour: decrypt credentials_enc from the Fernet vault.
oidc_aws    Call STS AssumeRole using a long-lived IAM key stored in oidc_config_enc.
            The connector's MCP server receives short-lived STS session credentials.
            oidc_config fields: role_arn, session_name (opt), duration_seconds (opt),
                                aws_access_key_id, aws_secret_access_key, region (opt)
vault_approle  Log in to HashiCorp Vault with AppRole, then read a KV-v2 secret.
            oidc_config fields: vault_addr, role_id, secret_id, secret_path,
                                duration_seconds (opt)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .db import scope_to_org, session_factory
from .security import decrypt, encrypt, redact

logger = logging.getLogger("opsforge.credentials")

CredentialKind = str  # "static" | "oidc_aws" | "vault_approle"

_DEFAULT_LEASE_TTL_S = 3600  # 1 hour fallback if not in config


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def resolve(
    connector: dict[str, Any],
    run_id: UUID | None = None,
) -> dict[str, str]:
    """Return a dict of env vars / headers for an MCP session.

    For `static` this is the decrypted Fernet vault. For JIT providers it is a
    freshly-minted short-lived credential. A lease audit row is written for every
    non-static issuance.
    """
    kind: CredentialKind = connector.get("credential_kind") or "static"

    if kind == "static":
        return _resolve_static(connector)
    if kind == "oidc_aws":
        return await _resolve_aws(connector, run_id)
    if kind == "vault_approle":
        return await _resolve_vault(connector, run_id)

    raise ValueError(f"unknown credential_kind {kind!r}")


async def expire_leases() -> int:
    """Delete leases that have expired or been revoked. Called by the worker tick.
    Returns the number of rows deleted."""
    async with session_factory().begin() as s:
        result = await s.execute(
            text(
                "DELETE FROM credential_leases "
                "WHERE expires_at < now() OR revoked_at IS NOT NULL"
            )
        )
        return result.rowcount  # type: ignore[attr-defined]


async def revoke_run_leases(run_id: UUID, org_id: Any) -> None:
    """Mark all active leases for a run as revoked when the run finishes."""
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "UPDATE credential_leases SET revoked_at = now() "
                "WHERE run_id = :run_id AND org_id = :org AND revoked_at IS NULL"
            ),
            {"run_id": run_id, "org": str(org_id)},
        )


# --------------------------------------------------------------------------- #
# Static (Fernet vault) — unchanged behaviour
# --------------------------------------------------------------------------- #


def _resolve_static(connector: dict[str, Any]) -> dict[str, str]:
    creds_enc = connector.get("credentials_enc")
    if not creds_enc:
        return {}
    raw = decrypt(creds_enc)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("static credentials envelope is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


# --------------------------------------------------------------------------- #
# AWS STS AssumeRole
# --------------------------------------------------------------------------- #


async def _resolve_aws(
    connector: dict[str, Any],
    run_id: UUID | None,
) -> dict[str, str]:
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for oidc_aws connectors: "
            "pip install 'opsforge[cloud]'"
        ) from exc

    cfg = _oidc_config(connector)
    role_arn: str = cfg["role_arn"]
    session_name: str = cfg.get("session_name") or "opsforge"
    duration: int = int(cfg.get("duration_seconds") or _DEFAULT_LEASE_TTL_S)
    region: str | None = cfg.get("region")

    sts_kwargs: dict[str, Any] = {}
    if cfg.get("aws_access_key_id") and cfg.get("aws_secret_access_key"):
        sts_kwargs["aws_access_key_id"] = cfg["aws_access_key_id"]
        sts_kwargs["aws_secret_access_key"] = cfg["aws_secret_access_key"]
    if region:
        sts_kwargs["region_name"] = region

    sts = boto3.client("sts", **sts_kwargs)
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        DurationSeconds=duration,
    )
    c = resp["Credentials"]
    expires_at = c["Expiration"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    await _record_lease(
        connector=connector,
        run_id=run_id,
        provider="aws_sts",
        expires_at=expires_at,
        metadata={"role_arn": role_arn, "session_name": session_name},
    )

    return {
        "AWS_ACCESS_KEY_ID": c["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
        "AWS_SESSION_TOKEN": c["SessionToken"],
    }


# --------------------------------------------------------------------------- #
# HashiCorp Vault AppRole
# --------------------------------------------------------------------------- #


async def _resolve_vault(
    connector: dict[str, Any],
    run_id: UUID | None,
) -> dict[str, str]:
    import httpx

    cfg = _oidc_config(connector)
    vault_addr: str = cfg["vault_addr"].rstrip("/")
    role_id: str = cfg["role_id"]
    secret_id: str = cfg["secret_id"]
    secret_path: str = cfg["secret_path"].lstrip("/")
    duration: int = int(cfg.get("duration_seconds") or _DEFAULT_LEASE_TTL_S)

    async with httpx.AsyncClient(timeout=10) as client:
        login = await client.post(
            f"{vault_addr}/v1/auth/approle/login",
            json={"role_id": role_id, "secret_id": secret_id},
        )
        login.raise_for_status()
        auth = login.json()["auth"]
        token: str = auth["client_token"]
        lease_duration: int = auth.get("lease_duration") or duration

        secret_resp = await client.get(
            f"{vault_addr}/v1/{secret_path}",
            headers={"X-Vault-Token": token},
        )
        secret_resp.raise_for_status()

    body = secret_resp.json()
    # Support both KV v1 (data at top level) and KV v2 (data.data).
    data: dict[str, Any] = body.get("data", {})
    if "data" in data:
        data = data["data"]

    expires_at = datetime.now(UTC) + timedelta(seconds=lease_duration)
    await _record_lease(
        connector=connector,
        run_id=run_id,
        provider="vault_approle",
        expires_at=expires_at,
        metadata={"vault_addr": vault_addr, "secret_path": secret_path},
    )

    return {str(k): str(v) for k, v in data.items()}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _oidc_config(connector: dict[str, Any]) -> dict[str, Any]:
    """Decrypt and parse the oidc_config_enc blob."""
    enc = connector.get("oidc_config_enc")
    if not enc:
        raise ValueError(
            f"connector {connector.get('id')} has credential_kind="
            f"{connector.get('credential_kind')!r} but no oidc_config_enc"
        )
    raw = decrypt(enc)
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ValueError("oidc_config_enc is not a JSON object")
    return cfg


async def _record_lease(
    *,
    connector: dict[str, Any],
    run_id: UUID | None,
    provider: str,
    expires_at: datetime,
    metadata: dict[str, Any],
) -> None:
    connector_id = connector["id"]
    org_id = connector["org_id"]
    async with session_factory().begin() as s:
        await scope_to_org(s, org_id)
        await s.execute(
            text(
                "INSERT INTO credential_leases "
                "(org_id, connector_id, run_id, provider, expires_at, lease_metadata) "
                "VALUES (:org, :connector, :run, :provider, :expires, CAST(:meta AS jsonb))"
            ),
            {
                "org": str(org_id),
                "connector": str(connector_id),
                "run": str(run_id) if run_id else None,
                "provider": provider,
                "expires": expires_at,
                "meta": json.dumps(redact(metadata)),
            },
        )
    logger.info(
        "jit lease issued connector=%s provider=%s run=%s expires=%s",
        connector_id,
        provider,
        run_id,
        expires_at.isoformat(),
    )


def oidc_config_for_create(
    credential_kind: str,
    oidc_config: dict[str, str] | None,
) -> bytes | None:
    """Encrypt an oidc_config dict for storage. Returns None for static connectors."""
    if credential_kind == "static" or not oidc_config:
        return None
    return encrypt(json.dumps(oidc_config))
