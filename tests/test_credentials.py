"""Unit tests for the JIT credential resolver (credentials.py).

Uses monkeypatching — no real AWS / Vault endpoints required. The DB layer
is replaced with a fake that records calls so we can assert lease rows are
written (or not) without a running Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from opsforge.credentials import (
    _oidc_config,
    _resolve_static,
    oidc_config_for_create,
    resolve,
)
from opsforge.security import encrypt

ORG_ID = "00000000-0000-0000-0000-000000000001"
CONNECTOR_ID = "aaaaaaaa-0000-0000-0000-000000000001"
RUN_ID = UUID("bbbbbbbb-0000-0000-0000-000000000001")


def _make_connector(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": CONNECTOR_ID,
        "org_id": ORG_ID,
        "kind": "aws",
        "credential_kind": "static",
        "credentials_enc": None,
        "oidc_config_enc": None,
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------- #
# Static path
# --------------------------------------------------------------------------- #


def test_resolve_static_no_creds() -> None:
    c = _make_connector()
    assert _resolve_static(c) == {}


def test_resolve_static_with_creds() -> None:
    creds = {"AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "secret"}
    enc = encrypt(json.dumps(creds))
    c = _make_connector(credentials_enc=enc)
    result = _resolve_static(c)
    assert result == creds


@pytest.mark.asyncio
async def test_resolve_dispatches_static() -> None:
    creds = {"TOKEN": "abc123"}
    enc = encrypt(json.dumps(creds))
    c = _make_connector(credential_kind="static", credentials_enc=enc)
    result = await resolve(c)
    assert result == creds


# --------------------------------------------------------------------------- #
# oidc_config_enc helpers
# --------------------------------------------------------------------------- #


def test_oidc_config_roundtrip() -> None:
    cfg = {"role_arn": "arn:aws:iam::123:role/OpsForge", "duration_seconds": "900"}
    enc = oidc_config_for_create("oidc_aws", cfg)
    assert enc is not None
    c = _make_connector(credential_kind="oidc_aws", oidc_config_enc=enc)
    assert _oidc_config(c) == cfg


def test_oidc_config_static_returns_none() -> None:
    assert oidc_config_for_create("static", {"key": "val"}) is None


def test_oidc_config_missing_enc_raises() -> None:
    c = _make_connector(credential_kind="oidc_aws", oidc_config_enc=None)
    with pytest.raises(ValueError, match="no oidc_config_enc"):
        _oidc_config(c)


# --------------------------------------------------------------------------- #
# AWS STS path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_aws_returns_sts_credentials() -> None:
    cfg = {
        "role_arn": "arn:aws:iam::123456789012:role/OpsForge",
        "session_name": "test",
        "duration_seconds": "900",
        "aws_access_key_id": "AKIATEST",
        "aws_secret_access_key": "testsecret",
    }
    enc = encrypt(json.dumps(cfg))
    c = _make_connector(credential_kind="oidc_aws", oidc_config_enc=enc)

    fake_expiry = datetime.now(UTC) + timedelta(seconds=900)
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIASESSION",
            "SecretAccessKey": "sessionkey",
            "SessionToken": "sessiontoken==",
            "Expiration": fake_expiry,
        }
    }
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_sts

    import sys
    with (
        patch("opsforge.credentials._record_lease", new_callable=AsyncMock) as mock_lease,
        patch.dict(sys.modules, {"boto3": fake_boto3}),
    ):
        result = await resolve(c, run_id=RUN_ID)

    assert result["AWS_ACCESS_KEY_ID"] == "ASIASESSION"
    assert result["AWS_SECRET_ACCESS_KEY"] == "sessionkey"
    assert result["AWS_SESSION_TOKEN"] == "sessiontoken=="

    mock_lease.assert_awaited_once()
    call_kwargs = mock_lease.call_args.kwargs
    assert call_kwargs["provider"] == "aws_sts"
    assert call_kwargs["metadata"]["role_arn"] == cfg["role_arn"]


@pytest.mark.asyncio
async def test_resolve_aws_missing_boto3_raises() -> None:
    cfg = {"role_arn": "arn:aws:iam::123:role/Test"}
    enc = encrypt(json.dumps(cfg))
    c = _make_connector(credential_kind="oidc_aws", oidc_config_enc=enc)

    with (
        patch.dict("sys.modules", {"boto3": None}),
        pytest.raises(RuntimeError, match="boto3 is required"),
    ):
        await resolve(c)


# --------------------------------------------------------------------------- #
# Vault AppRole path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_vault_returns_secret_data() -> None:
    cfg = {
        "vault_addr": "https://vault.example.com",
        "role_id": "test-role-id",
        "secret_id": "test-secret-id",
        "secret_path": "secret/data/opsforge/myconn",
        "duration_seconds": "1800",
    }
    enc = encrypt(json.dumps(cfg))
    c = _make_connector(credential_kind="vault_approle", oidc_config_enc=enc)

    login_resp = MagicMock()
    login_resp.raise_for_status = MagicMock()
    login_resp.json.return_value = {
        "auth": {"client_token": "hvs.TOKEN", "lease_duration": 1800}
    }

    secret_resp = MagicMock()
    secret_resp.raise_for_status = MagicMock()
    secret_resp.json.return_value = {
        "data": {"data": {"DB_PASSWORD": "s3cret", "DB_USER": "opsforge"}}
    }

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=login_resp)
    fake_client.get = AsyncMock(return_value=secret_resp)

    with (
        patch("opsforge.credentials._record_lease", new_callable=AsyncMock) as mock_lease,
        patch("httpx.AsyncClient", return_value=fake_client),
    ):
        result = await resolve(c, run_id=RUN_ID)

    assert result == {"DB_PASSWORD": "s3cret", "DB_USER": "opsforge"}

    mock_lease.assert_awaited_once()
    call_kwargs = mock_lease.call_args.kwargs
    assert call_kwargs["provider"] == "vault_approle"


# --------------------------------------------------------------------------- #
# Unknown kind
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_unknown_kind_raises() -> None:
    c = _make_connector(credential_kind="magic_unicorn")
    with pytest.raises(ValueError, match="unknown credential_kind"):
        await resolve(c)
