"""/periods API integration tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.ledger._helpers import create_company, create_period


@pytest.mark.asyncio
async def test_create_period_defaults_to_open(client: AsyncClient) -> None:
    company_id = await create_company(client, "PERC1")
    period = await create_period(client, company_id, 2026, 1)
    assert period["status"] == "open"
    assert period["year"] == 2026
    assert period["month"] == 1
    assert period["company_id"] == str(company_id)


@pytest.mark.asyncio
async def test_duplicate_period_same_company_year_month_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "PERC2")
    await create_period(client, company_id, 2026, 2)

    dup = await client.post(
        "/api/v1/periods", json={"year": 2026, "month": 2}, headers=company_headers(company_id)
    )
    assert dup.status_code == 409, dup.text


@pytest.mark.asyncio
async def test_same_year_month_in_different_companies_is_allowed(client: AsyncClient) -> None:
    company_a = await create_company(client, "PERCA")
    company_b = await create_company(client, "PERCB")

    await create_period(client, company_a, 2026, 3)
    second = await client.post(
        "/api/v1/periods", json={"year": 2026, "month": 3}, headers=company_headers(company_b)
    )
    assert second.status_code == 201, second.text


@pytest.mark.asyncio
async def test_close_period(client: AsyncClient) -> None:
    company_id = await create_company(client, "PERC3")
    period = await create_period(client, company_id, 2026, 4)

    close_response = await client.post(
        f"/api/v1/periods/{period['id']}/close", headers=company_headers(company_id)
    )
    assert close_response.status_code == 200, close_response.text
    assert close_response.json()["status"] == "closed"


@pytest.mark.asyncio
async def test_closing_an_already_closed_period_is_idempotent(client: AsyncClient) -> None:
    company_id = await create_company(client, "PERC4")
    period = await create_period(client, company_id, 2026, 5)
    headers = company_headers(company_id)

    first_close = await client.post(f"/api/v1/periods/{period['id']}/close", headers=headers)
    assert first_close.status_code == 200

    second_close = await client.post(f"/api/v1/periods/{period['id']}/close", headers=headers)
    assert second_close.status_code == 200
    assert second_close.json()["status"] == "closed"


@pytest.mark.asyncio
async def test_close_nonexistent_period_is_404(client: AsyncClient) -> None:
    company_id = await create_company(client, "PERC5")
    bogus_id = "00000000-0000-0000-0000-000000000000"

    response = await client.post(
        f"/api/v1/periods/{bogus_id}/close", headers=company_headers(company_id)
    )
    assert response.status_code == 404
