"""Shared HTTP-layer helpers for tests/inventory/* — mirrors tests/sales/_helpers.py."""

from __future__ import annotations

import uuid
from decimal import Decimal

from httpx import AsyncClient, Response

from tests.conftest import company_headers


async def create_company(client: AsyncClient, code: str) -> uuid.UUID:
    response = await client.post(
        "/api/v1/companies",
        json={"code": code, "name": f"{code} Inc.", "functional_currency_code": "TWD"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def uom_id(client: AsyncClient, company_id: uuid.UUID) -> uuid.UUID:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    return uuid.UUID(next(u["id"] for u in response.json() if u["code"] == "EA"))


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


async def create_adjustment(
    client: AsyncClient,
    company_id: uuid.UUID,
    product_id: uuid.UUID,
    qty_delta: str | Decimal,
    reason: str = "manual intake",
) -> Response:
    return await client.post(
        "/api/v1/inventory/adjustments",
        json={"product_id": str(product_id), "qty_delta": str(qty_delta), "reason": reason},
        headers=company_headers(company_id),
    )


async def get_summary(
    client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID | None = None
) -> Response:
    params = {"product_id": str(product_id)} if product_id is not None else {}
    return await client.get(
        "/api/v1/inventory/summary", params=params, headers=company_headers(company_id)
    )


async def list_moves(
    client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID | None = None
) -> Response:
    params = {"product_id": str(product_id)} if product_id is not None else {}
    return await client.get(
        "/api/v1/inventory/moves", params=params, headers=company_headers(company_id)
    )


async def on_hand(client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    resp = await get_summary(client, company_id, product_id)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    if not rows:
        return Decimal("0")
    return Decimal(rows[0]["on_hand"])
