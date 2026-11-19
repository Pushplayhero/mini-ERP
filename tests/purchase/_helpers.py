"""Shared HTTP-layer helpers for tests/purchase/*."""

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


async def create_supplier(
    client: AsyncClient,
    company_id: uuid.UUID,
    code: str,
    *,
    name: str | None = None,
    payment_terms_days: int = 30,
) -> uuid.UUID:
    response = await client.post(
        "/api/v1/suppliers",
        json={
            "code": code,
            "name": name or f"{code} supplier",
            "currency_code": "TWD",
            "payment_terms_days": payment_terms_days,
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
    standard_cost: str = "30",
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
    product_id: uuid.UUID, qty: str | Decimal, *, unit_cost: str | Decimal | None = None
) -> dict[str, Any]:
    line: dict[str, Any] = {"product_id": str(product_id), "qty": str(qty)}
    if unit_cost is not None:
        line["unit_cost"] = str(unit_cost)
    return line


async def create_draft_order(
    client: AsyncClient,
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    lines: list[dict[str, Any]],
    *,
    custom_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"supplier_id": str(supplier_id), "lines": lines}
    if custom_data is not None:
        payload["custom_data"] = custom_data
    response = await client.post(
        "/api/v1/purchase-orders", json=payload, headers=company_headers(company_id)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def confirm_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/purchase-orders/{order_id}/confirm", headers=company_headers(company_id)
    )


async def cancel_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/purchase-orders/{order_id}/cancel", headers=company_headers(company_id)
    )


async def receive_order(client: AsyncClient, company_id: uuid.UUID, order_id: str) -> Response:
    return await client.post(
        f"/api/v1/purchase-orders/{order_id}/receive", headers=company_headers(company_id)
    )
