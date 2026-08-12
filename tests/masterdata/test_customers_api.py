"""masterdata /customers CRUD integration tests (real Postgres, no mocks)."""

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


@pytest.mark.asyncio
async def test_create_and_get_customer(client: AsyncClient) -> None:
    company_id = await _create_company(client)
    headers = company_headers(company_id)

    create_response = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-001",
            "name": "Test Customer Co.",
            "credit_limit": "50000.123456",
            "currency_code": "TWD",
        },
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text
    body = create_response.json()
    assert body["code"] == "CUST-001"
    assert body["company_id"] == str(company_id)
    assert body["credit_limit"] == "50000.123456"

    customer_id = body["id"]
    get_response = await client.get(f"/api/v1/customers/{customer_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["code"] == "CUST-001"


@pytest.mark.asyncio
async def test_list_customers_scoped_to_company(client: AsyncClient) -> None:
    company_a = await _create_company(client, "COA")
    company_b = await _create_company(client, "COB")

    await client.post(
        "/api/v1/customers",
        json={"code": "A1", "name": "A Customer", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    await client.post(
        "/api/v1/customers",
        json={"code": "B1", "name": "B Customer", "currency_code": "TWD"},
        headers=company_headers(company_b),
    )

    list_a = await client.get("/api/v1/customers", headers=company_headers(company_a))
    assert list_a.status_code == 200
    codes_a = {c["code"] for c in list_a.json()}
    assert codes_a == {"A1"}

    list_b = await client.get("/api/v1/customers", headers=company_headers(company_b))
    codes_b = {c["code"] for c in list_b.json()}
    assert codes_b == {"B1"}


@pytest.mark.asyncio
async def test_update_and_delete_customer(client: AsyncClient) -> None:
    company_id = await _create_company(client)
    headers = company_headers(company_id)

    create_response = await client.post(
        "/api/v1/customers",
        json={"code": "CUST-UPD", "name": "Before", "currency_code": "TWD"},
        headers=headers,
    )
    customer_id = create_response.json()["id"]

    update_response = await client.patch(
        f"/api/v1/customers/{customer_id}",
        json={"name": "After", "credit_limit": "1000.5"},
        headers=headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "After"
    # NUMERIC(20, 6) per master-plan §10.1 — full scale is preserved, not stripped.
    assert update_response.json()["credit_limit"] == "1000.500000"

    delete_response = await client.delete(f"/api/v1/customers/{customer_id}", headers=headers)
    assert delete_response.status_code == 204

    get_after_delete = await client.get(f"/api/v1/customers/{customer_id}", headers=headers)
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_missing_company_context_is_rejected(client: AsyncClient) -> None:
    """No X-Company-Id header -> fail-closed 403, never an empty/unfiltered result."""
    response = await client.get("/api/v1/customers")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_client_supplied_company_id_in_body_is_ignored(client: AsyncClient) -> None:
    """`CustomerCreate` has no `company_id` field — a client can't spoof one via the body."""
    company_id = await _create_company(client)
    other_company_id = await _create_company(client, "OTHER")

    response = await client.post(
        "/api/v1/customers",
        json={
            "code": "SNEAKY",
            "name": "Sneaky Co",
            "currency_code": "TWD",
            "company_id": str(other_company_id),  # not a real field on CustomerCreate
        },
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    # The customer was stamped with the header's company, not the spoofed body value.
    assert response.json()["company_id"] == str(company_id)
