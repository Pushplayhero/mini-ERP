"""Shared HTTP-layer helpers for tests/sales/* — mirrors tests/ledger/_helpers.py."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response

from tests.conftest import company_headers


async def create_company(client: AsyncClient, code: str) -> uuid.UUID:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code=code)
    return auth.company_id


async def uom_id(client: AsyncClient, company_id: uuid.UUID) -> uuid.UUID:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    return uuid.UUID(next(u["id"] for u in response.json() if u["code"] == "EA"))


async def create_customer(
    client: AsyncClient,
    company_id: uuid.UUID,
    code: str,
    *,
    credit_limit: str = "0",
) -> uuid.UUID:
    response = await client.post(
        "/api/v1/customers",
        json={
            "code": code,
            "name": f"{code} customer",
            "currency_code": "TWD",
            "credit_limit": credit_limit,
        },
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def create_product(
    client: AsyncClient,
    company_id: uuid.UUID,
    sku: str,
    *,
    list_price: str = "100",
    standard_cost: str = "0",
) -> uuid.UUID:
    uom = await uom_id(client, company_id)
    response = await client.post(
        "/api/v1/products",
        json={
            "sku": sku,
            "name": f"{sku} widget",
            "uom_id": str(uom),
            "list_price": list_price,
            "standard_cost": standard_cost,
        },
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def order_line(
    product_id: uuid.UUID, qty: str | Decimal, *, unit_price: str | Decimal | None = None
) -> dict[str, Any]:
    line: dict[str, Any] = {"product_id": str(product_id), "qty": str(qty)}
    if unit_price is not None:
        line["unit_price"] = str(unit_price)
    return line


async def create_draft_order(
    client: AsyncClient,
    company_id: uuid.UUID,
    customer_id: uuid.UUID,
    lines: list[dict[str, Any]],
    *,
    custom_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"customer_id": str(customer_id), "lines": lines}
    if custom_data is not None:
        payload["custom_data"] = custom_data
    response = await client.post(
        "/api/v1/sales-orders", json=payload, headers=company_headers(company_id)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def confirm_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/sales-orders/{order_id}/confirm", headers=company_headers(company_id)
    )


async def cancel_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/sales-orders/{order_id}/cancel", headers=company_headers(company_id)
    )


async def ship_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/sales-orders/{order_id}/ship", headers=company_headers(company_id)
    )
