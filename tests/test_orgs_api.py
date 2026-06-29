"""Org Ancestors API tests (GET/POST /api/v1/orgs/{id}/ancestors).

Tests that require a database talk to the Compose `db` service.
Bring it up first with:  docker compose up -d db migrate
"""

from __future__ import annotations

import uuid

import pytest
from conftest import api_client

from opsforge.config import get_settings

pytestmark = pytest.mark.usefixtures("db_required")


# --------------------------------------------------------------------------- #
# GET /api/v1/orgs/{org_id}/ancestors
# --------------------------------------------------------------------------- #


async def test_list_ancestors_empty(auth_headers):
    """An org with no ancestors returns an empty list."""
    org_id = get_settings().org_id
    async with api_client() as client:
        resp = await client.get(
            f"/api/v1/orgs/{org_id}/ancestors", headers=auth_headers
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == org_id
    assert isinstance(body["ancestors"], list)


async def test_list_ancestors_wrong_org_is_403(auth_headers):
    """Requesting ancestors for a different org returns 403."""
    other_org = str(uuid.uuid4())
    async with api_client() as client:
        resp = await client.get(
            f"/api/v1/orgs/{other_org}/ancestors", headers=auth_headers
        )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# POST /api/v1/orgs/{org_id}/ancestors
# --------------------------------------------------------------------------- #


async def test_add_ancestor_requires_admin(auth_headers):
    """Non-admin callers get 403."""
    org_id = get_settings().org_id
    ancestor_id = str(uuid.uuid4())
    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/orgs/{org_id}/ancestors",
            headers=auth_headers,  # regular token, role=viewer or member
            json={"ancestor_id": ancestor_id},
        )
    assert resp.status_code == 403


async def test_add_ancestor_self_reference_is_400(admin_auth_headers):
    """An org cannot declare itself as its own ancestor."""
    org_id = get_settings().org_id
    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/orgs/{org_id}/ancestors",
            headers=admin_auth_headers,
            json={"ancestor_id": org_id},
        )
    assert resp.status_code == 400


async def test_add_ancestor_wrong_org_is_403(admin_auth_headers):
    """Admin cannot add ancestors for an org other than their own."""
    other_org = str(uuid.uuid4())
    ancestor_id = str(uuid.uuid4())
    async with api_client() as client:
        resp = await client.post(
            f"/api/v1/orgs/{other_org}/ancestors",
            headers=admin_auth_headers,
            json={"ancestor_id": ancestor_id},
        )
    assert resp.status_code == 403


async def test_add_ancestor_and_list(admin_auth_headers):
    """Admin can add an ancestor and it appears in GET."""
    org_id = get_settings().org_id
    ancestor_id = str(uuid.uuid4())
    async with api_client() as client:
        post_resp = await client.post(
            f"/api/v1/orgs/{org_id}/ancestors",
            headers=admin_auth_headers,
            json={"ancestor_id": ancestor_id},
        )
        assert post_resp.status_code == 201, post_resp.text
        body = post_resp.json()
        assert body["org_id"] == org_id
        assert body["ancestor_id"] == ancestor_id

        get_resp = await client.get(
            f"/api/v1/orgs/{org_id}/ancestors", headers=admin_auth_headers
        )
    assert get_resp.status_code == 200
    ancestors = get_resp.json()["ancestors"]
    assert ancestor_id in ancestors


async def test_add_ancestor_idempotent(admin_auth_headers):
    """POSTing the same ancestor twice is idempotent (ON CONFLICT DO NOTHING → 201 both times)."""
    org_id = get_settings().org_id
    ancestor_id = str(uuid.uuid4())
    async with api_client() as client:
        for _ in range(2):
            resp = await client.post(
                f"/api/v1/orgs/{org_id}/ancestors",
                headers=admin_auth_headers,
                json={"ancestor_id": ancestor_id},
            )
            assert resp.status_code == 201, resp.text
