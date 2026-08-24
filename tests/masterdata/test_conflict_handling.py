"""Regression tests for master-data conflict handling.

Duplicate keys and nonexistent company references must be translated by the
service into ``ConflictError``. HTTP adapters may map that domain error to
409; tenant authority comes from server-bound context, never from a client
company header.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import actor_context
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import AccountType
from app.modules.masterdata.schemas import AccountCreate, CustomerCreate, ProductCreate
from tests.conftest import TEST_ADMIN_ACTOR, company_headers


async def _create_company(client: AsyncClient, code: str) -> uuid.UUID:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code=code)
    return auth.company_id


async def _uom_id(client: AsyncClient, company_id: uuid.UUID) -> str:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    return str(next(u["id"] for u in response.json() if u["code"] == "EA"))


# ---------------------------------------------------------------------------
# Duplicate unique keys -> 409, not 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_company_code_is_409(client: AsyncClient) -> None:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code="ANCH")
    first = await client.post(
        "/api/v1/companies",
        json={"code": "DUPCO", "name": "First", "functional_currency_code": "TWD"},
        headers=auth.headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/companies",
        json={"code": "DUPCO", "name": "Second", "functional_currency_code": "TWD"},
        headers=auth.headers,
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
# Service-level nonexistent company_id (well-formed UUID, no matching row)
# raises ConflictError; company context is server-bound, not supplied by an
# HTTP company header.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_customer_with_nonexistent_company_id_is_409(
    db_session: AsyncSession,
) -> None:
    bogus_company_id = uuid.uuid4()
    with (
        actor_context(TEST_ADMIN_ACTOR),
        company_context(bogus_company_id),
        pytest.raises(ConflictError),
    ):
        await masterdata_service.create_customer(
            db_session,
            CustomerCreate(code="GHOST", name="Ghost", currency_code="TWD"),
        )


@pytest.mark.asyncio
async def test_create_product_with_nonexistent_company_id_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    real_company_id = await _create_company(client, "CONFP2")
    uom_id = uuid.UUID(await _uom_id(client, real_company_id))
    bogus_company_id = uuid.uuid4()

    with (
        actor_context(TEST_ADMIN_ACTOR),
        company_context(bogus_company_id),
        pytest.raises(ConflictError),
    ):
        await masterdata_service.create_product(
            db_session,
            ProductCreate(sku="GHOST-SKU", name="Ghost", uom_id=uom_id),
        )


@pytest.mark.asyncio
async def test_create_account_with_nonexistent_company_id_is_409(
    db_session: AsyncSession,
) -> None:
    bogus_company_id = uuid.uuid4()

    with (
        actor_context(TEST_ADMIN_ACTOR),
        company_context(bogus_company_id),
        pytest.raises(ConflictError),
    ):
        await masterdata_service.create_account(
            db_session,
            AccountCreate(code="9000", name="Ghost", type=AccountType.ASSET),
        )
