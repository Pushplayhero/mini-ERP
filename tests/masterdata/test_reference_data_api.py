"""Light coverage for global reference-data endpoints (UoM / currencies).

These public reads do not require bearer authentication; company context is
not supplied by an HTTP header and is not used as tenant authority.
isolation semantics apply (see README "Design Decisions").
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_seeded_currencies(client: AsyncClient) -> None:
    response = await client.get("/api/v1/currencies")
    assert response.status_code == 200
    codes = {c["code"] for c in response.json()}
    assert {"TWD", "USD"}.issubset(codes)


@pytest.mark.asyncio
async def test_list_seeded_uom(client: AsyncClient) -> None:
    response = await client.get("/api/v1/uom")
    assert response.status_code == 200
    codes = {u["code"] for u in response.json()}
    assert {"EA", "KG"}.issubset(codes)


@pytest.mark.asyncio
async def test_create_uom_conversion(client: AsyncClient) -> None:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code="REF1")
    uoms = (await client.get("/api/v1/uom")).json()
    ea_id = next(u["id"] for u in uoms if u["code"] == "EA")
    kg_id = next(u["id"] for u in uoms if u["code"] == "KG")

    response = await client.post(
        "/api/v1/uom-conversions",
        json={"from_uom_id": kg_id, "to_uom_id": ea_id, "factor": "1.000000"},
        headers=auth.headers,
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_create_exchange_rate(client: AsyncClient) -> None:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code="REF2")
    response = await client.post(
        "/api/v1/exchange-rates",
        json={
            "from_currency_code": "USD",
            "to_currency_code": "TWD",
            "rate": "31.5000000000",
            "rate_date": "2026-08-13",
        },
        headers=auth.headers,
    )
    assert response.status_code == 201, response.text
    # NUMERIC(20, 10) per master-plan §10.1 — full scale is preserved, not stripped.
    assert response.json()["rate"] == "31.5000000000"
