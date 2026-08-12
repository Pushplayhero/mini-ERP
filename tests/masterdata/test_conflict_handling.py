"""Regression tests for /CODEX REVIEW DIFF (2026-08-13) Important findings.

Before the fix in `app.modules.masterdata.service._commit_or_conflict`, two
classes of request hit an unhandled `sqlalchemy.exc.IntegrityError` and
surfaced as a generic 500 instead of the already-built `ConflictError` / 409
path:

1. Duplicate unique keys (company code, customer/product/account code-or-sku
   scoped to a company).
2. A well-formed but nonexistent `X-Company-Id` (no matching `companies`
   row), which violates the `company_id` foreign key on create.

Every test here asserts `409`, not just "no crash" — a regression that
brings back the raw 500 (or silently swallows the conflict and returns 200)
must fail this suite.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers


async def _create_company(client: AsyncClient, code: str) -> uuid.UUID:
    response = await client.post(
        "/api/v1/companies",
        json={"code": code, "name": f"{code} Inc.", "functional_currency_code": "TWD"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _uom_id(client: AsyncClient, company_id: uuid.UUID) -> str:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    return next(u["id"] for u in response.json() if u["code"] == "EA")


# ---------------------------------------------------------------------------
# Duplicate unique keys -> 409, not 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_company_code_is_409(client: AsyncClient) -> None:
    first = await client.post(
        "/api/v1/companies",
        json={"code": "DUPCO", "name": "First", "functional_currency_code": "TWD"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/companies",
        json={"code": "DUPCO", "name": "Second", "functional_currency_code": "TWD"},
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_duplicate_customer_code_within_same_company_is_409(client: AsyncClient) -> None:
    company_id = await _create_company(client, "CONFC1")
    headers = company_headers(company_id)

    first = await client.post(
        "/api/v1/customers",
        json={"code": "CUST-1", "name": "First", "currency_code": "TWD"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/customers",
        json={"code": "CUST-1", "name": "Second", "currency_code": "TWD"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_same_customer_code_in_different_companies_is_allowed(client: AsyncClient) -> None:
    """Uniqueness is scoped per-company, not global — this must stay a 201."""
    company_a = await _create_company(client, "CONFCA")
    company_b = await _create_company(client, "CONFCB")

    first = await client.post(
        "/api/v1/customers",
        json={"code": "SHARED", "name": "A's customer", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/customers",
        json={"code": "SHARED", "name": "B's customer", "currency_code": "TWD"},
        headers=company_headers(company_b),
    )
    assert second.status_code == 201, second.text


@pytest.mark.asyncio
async def test_duplicate_product_sku_within_same_company_is_409(client: AsyncClient) -> None:
    company_id = await _create_company(client, "CONFP1")
    headers = company_headers(company_id)
    uom_id = await _uom_id(client, company_id)

    first = await client.post(
        "/api/v1/products",
        json={"sku": "SKU-1", "name": "First", "uom_id": uom_id},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/products",
        json={"sku": "SKU-1", "name": "Second", "uom_id": uom_id},
        headers=headers,
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_duplicate_account_code_within_same_company_is_409(client: AsyncClient) -> None:
    company_id = await _create_company(client, "CONFA1")
    headers = company_headers(company_id)

    first = await client.post(
        "/api/v1/accounts",
        json={"code": "1000", "name": "Cash", "type": "asset"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/accounts",
        json={"code": "1000", "name": "Cash (dup)", "type": "asset"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# Nonexistent company_id (well-formed UUID, no matching row) -> 409, not 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_customer_with_nonexistent_company_id_is_409(client: AsyncClient) -> None:
    bogus_company_id = uuid.uuid4()

    response = await client.post(
        "/api/v1/customers",
        json={"code": "GHOST", "name": "Ghost", "currency_code": "TWD"},
        headers=company_headers(bogus_company_id),
    )
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_create_product_with_nonexistent_company_id_is_409(client: AsyncClient) -> None:
    real_company_id = await _create_company(client, "CONFP2")
    uom_id = await _uom_id(client, real_company_id)
    bogus_company_id = uuid.uuid4()

    response = await client.post(
        "/api/v1/products",
        json={"sku": "GHOST-SKU", "name": "Ghost", "uom_id": uom_id},
        headers=company_headers(bogus_company_id),
    )
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_create_account_with_nonexistent_company_id_is_409(client: AsyncClient) -> None:
    bogus_company_id = uuid.uuid4()

    response = await client.post(
        "/api/v1/accounts",
        json={"code": "9000", "name": "Ghost", "type": "asset"},
        headers=company_headers(bogus_company_id),
    )
    assert response.status_code == 409, response.text
