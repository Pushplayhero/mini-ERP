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
async def test_negative_list_price_is_rejected_422(client: AsyncClient) -> None:
    """Diff-review regression: `list_price` had no lower-bound validation.
    Since `sales.service.confirm_order`'s repricing fix writes
    `product.list_price` straight into a non-override line's `unit_price`/
    `amount`, a negative price would otherwise reach
    `ck_sales_order_lines_unit_price_nonneg`/`_amount_nonneg` at confirm
    time instead of being rejected here, up front, at the source.
    """
    company_id = await _create_company(client, "NEGPRICE")
    headers = company_headers(company_id)
    uom_id = await _get_uom_id(client, company_id)

    create_response = await client.post(
        "/api/v1/products",
        json={"sku": "SKU-NEG1", "name": "Widget", "uom_id": str(uom_id), "list_price": "-1"},
        headers=headers,
    )
    assert create_response.status_code == 422, create_response.text

    valid_response = await client.post(
        "/api/v1/products",
        json={"sku": "SKU-NEG2", "name": "Widget", "uom_id": str(uom_id), "list_price": "10"},
        headers=headers,
    )
    assert valid_response.status_code == 201, valid_response.text

    update_response = await client.patch(
        f"/api/v1/products/{valid_response.json()['id']}",
        json={"list_price": "-5"},
        headers=headers,
    )
    assert update_response.status_code == 422, update_response.text


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
