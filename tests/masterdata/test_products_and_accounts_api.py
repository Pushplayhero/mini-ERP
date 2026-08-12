"""masterdata /products and /accounts CRUD integration tests."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers


async def _create_company(client: AsyncClient, code: str = "ACME") -> uuid.UUID:
    response = await client.post(
        "/api/v1/companies",
        json={"code": code, "name": f"{code} Inc.", "functional_currency_code": "TWD"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _get_uom_id(client: AsyncClient, company_id: uuid.UUID) -> uuid.UUID:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    assert response.status_code == 200
    ea = next(u for u in response.json() if u["code"] == "EA")
    return uuid.UUID(ea["id"])


@pytest.mark.asyncio
async def test_product_crud(client: AsyncClient) -> None:
    company_id = await _create_company(client)
    headers = company_headers(company_id)
    uom_id = await _get_uom_id(client, company_id)

    create_response = await client.post(
        "/api/v1/products",
        json={
            "sku": "SKU-001",
            "name": "Widget",
            "uom_id": str(uom_id),
            "list_price": "199.990000",
        },
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text
    product = create_response.json()
    assert product["company_id"] == str(company_id)
    # NUMERIC(20, 6) per master-plan §10.1 — full scale is preserved, not stripped.
    assert product["list_price"] == "199.990000"

    update_response = await client.patch(
        f"/api/v1/products/{product['id']}",
        json={"list_price": "250.000000"},
        headers=headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["list_price"] == "250.000000"

    delete_response = await client.delete(f"/api/v1/products/{product['id']}", headers=headers)
    assert delete_response.status_code == 204


@pytest.mark.asyncio
async def test_account_crud(client: AsyncClient) -> None:
    company_id = await _create_company(client)
    headers = company_headers(company_id)

    create_response = await client.post(
        "/api/v1/accounts",
        json={"code": "1100-AR", "name": "Accounts Receivable", "type": "asset"},
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text
    account = create_response.json()
    assert account["type"] == "asset"

    list_response = await client.get("/api/v1/accounts", headers=headers)
    assert any(a["code"] == "1100-AR" for a in list_response.json())

    update_response = await client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"is_active": False},
        headers=headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["is_active"] is False
