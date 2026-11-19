"""masterdata /suppliers CRUD integration tests (ADR-014, real Postgres, no mocks)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import company_headers
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)


async def _create_company(client: AsyncClient, code: str = "SUPCO") -> uuid.UUID:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code=code)
    return auth.company_id


@pytest.mark.asyncio
async def test_create_get_list_update_delete_supplier(client: AsyncClient) -> None:
    company_id = await _create_company(client)
    headers = company_headers(company_id)

    create_resp = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-001",
            "name": "Taipei Parts Vendor Co.",
            "currency_code": "TWD",
            "payment_terms_days": 45,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert body["code"] == "SUP-001"
    assert body["company_id"] == str(company_id)
    assert body["currency_code"] == "TWD"
    assert body["payment_terms_days"] == 45
    assert body["is_active"] is True
    assert body["custom_data"] == {}

    supplier_id = body["id"]
    get_resp = await client.get(f"/api/v1/suppliers/{supplier_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["code"] == "SUP-001"

    list_resp = await client.get("/api/v1/suppliers", headers=headers)
    assert list_resp.status_code == 200
    assert {s["code"] for s in list_resp.json()} == {"SUP-001"}

    patch_resp = await client.patch(
        f"/api/v1/suppliers/{supplier_id}",
        json={"name": "Taipei Parts Vendor Ltd.", "payment_terms_days": 60, "is_active": False},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    updated = patch_resp.json()
    assert updated["name"] == "Taipei Parts Vendor Ltd."
    assert updated["payment_terms_days"] == 60
    assert updated["is_active"] is False

    del_resp = await client.delete(f"/api/v1/suppliers/{supplier_id}", headers=headers)
    assert del_resp.status_code == 204

    get_deleted = await client.get(f"/api/v1/suppliers/{supplier_id}", headers=headers)
    assert get_deleted.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_supplier_code_returns_409(client: AsyncClient) -> None:
    company_id = await _create_company(client, "SUPDUP")
    headers = company_headers(company_id)

    payload = {"code": "SUP-DUP", "name": "Vendor 1", "currency_code": "TWD"}
    first = await client.post("/api/v1/suppliers", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post("/api/v1/suppliers", json=payload, headers=headers)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_supplier_rejects_non_twd_currency_and_out_of_bounds_terms(
    client: AsyncClient,
) -> None:
    company_id = await _create_company(client, "SUPVAL")
    headers = company_headers(company_id)

    bad_ccy = await client.post(
        "/api/v1/suppliers",
        json={"code": "SUP-USD", "name": "US Supplier", "currency_code": "USD"},
        headers=headers,
    )
    assert bad_ccy.status_code == 422

    bad_terms_low = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-T1",
            "name": "Bad Terms",
            "currency_code": "TWD",
            "payment_terms_days": -1,
        },
        headers=headers,
    )
    assert bad_terms_low.status_code == 422

    bad_terms_high = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-T2",
            "name": "Bad Terms",
            "currency_code": "TWD",
            "payment_terms_days": 366,
        },
        headers=headers,
    )
    assert bad_terms_high.status_code == 422

    # Valid boundary terms: 0 and 365
    ok_zero = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-COD",
            "name": "Cash on Delivery",
            "currency_code": "TWD",
            "payment_terms_days": 0,
        },
        headers=headers,
    )
    assert ok_zero.status_code == 201
    assert ok_zero.json()["payment_terms_days"] == 0

    bad_patch_ccy = await client.patch(
        f"/api/v1/suppliers/{ok_zero.json()['id']}",
        json={"currency_code": "USD"},
        headers=headers,
    )
    assert bad_patch_ccy.status_code == 422


@pytest.mark.asyncio
async def test_supplier_custom_fields_validation_and_bankers_rounding(
    client: AsyncClient,
) -> None:
    company_id = await _create_company(client, "SUPCF")
    headers = company_headers(company_id)

    cf_resp = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "supplier",
            "field_key": "rebate_pct",
            "label": "Rebate Rate",
            "field_type": "DECIMAL",
            "is_required": True,
            "validation_rules": {"min_value": "0", "max_value": "100"},
        },
        headers=headers,
    )
    assert cf_resp.status_code == 201, cf_resp.text

    missing_cf = await client.post(
        "/api/v1/suppliers",
        json={"code": "SUP-MISS", "name": "Missing CF", "currency_code": "TWD"},
        headers=headers,
    )
    assert missing_cf.status_code == 422

    unknown_cf = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-UNK",
            "name": "Unknown CF",
            "currency_code": "TWD",
            "custom_data": {"rebate_pct": "1.5", "extra_field": "bad"},
        },
        headers=headers,
    )
    assert unknown_cf.status_code == 422

    valid_cf = await client.post(
        "/api/v1/suppliers",
        json={
            "code": "SUP-OK",
            "name": "Valid CF Vendor",
            "currency_code": "TWD",
            "custom_data": {"rebate_pct": "2.5000025"},  # Half-even -> 2.500002
        },
        headers=headers,
    )
    assert valid_cf.status_code == 201, valid_cf.text
    assert valid_cf.json()["custom_data"]["rebate_pct"] == "2.500002"


@pytest.mark.asyncio
async def test_supplier_rbac_denies_without_permission(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await _create_company(client, "SUPRBAC")
    await create_role_without_permissions(db_session, "no_supplier_role")
    await create_user_with_role(
        db_session,
        email="no_supplier_perm@example.com",
        password="pw",
        company_id=company_id,
        role_code="no_supplier_role",
    )
    login = await client.post(
        "/auth/login",
        json={"email": "no_supplier_perm@example.com", "password": "pw"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    for method, path, body in (
        ("GET", "/api/v1/suppliers", None),
        (
            "POST",
            "/api/v1/suppliers",
            {"code": "X", "name": "X", "currency_code": "TWD"},
        ),
    ):
        resp = await client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403, f"expected 403 for {method} {path}, got {resp.status_code}"
