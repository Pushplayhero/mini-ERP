"""Multi-company isolation tests — master-plan §10.2, the mandatory suite.

For every tenant-scoped masterdata endpoint (customers, products, accounts):
company A creates a resource; company B must get 404 on GET/PATCH/DELETE by
id, and must never see it in a list. A request with no company context at
all must be rejected (403), never silently return an empty/unfiltered set.

Also includes a DB-layer test (no HTTP) proving the ORM filter itself is
fail-closed, independent of the router/exception-handler wiring.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TenancyContextError
from app.core.tenancy import company_context
from app.modules.masterdata.models import Customer
from tests.conftest import company_headers


async def _create_company(client: AsyncClient, code: str) -> uuid.UUID:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code=code)
    return auth.company_id


async def _uom_id(client: AsyncClient, company_id: uuid.UUID) -> str:
    response = await client.get("/api/v1/uom", headers=company_headers(company_id))
    return str(next(u["id"] for u in response.json() if u["code"] == "EA"))


# ---------------------------------------------------------------------------
# customers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_cross_company_get_is_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOA1")
    company_b = await _create_company(client, "ISOB1")

    create_response = await client.post(
        "/api/v1/customers",
        json={"code": "SECRET", "name": "Company A's Customer", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    customer_id = create_response.json()["id"]

    # Company A can read its own customer.
    own_read = await client.get(
        f"/api/v1/customers/{customer_id}", headers=company_headers(company_a)
    )
    assert own_read.status_code == 200

    # Company B must not be able to read it.
    cross_read = await client.get(
        f"/api/v1/customers/{customer_id}", headers=company_headers(company_b)
    )
    assert cross_read.status_code == 404


@pytest.mark.asyncio
async def test_customer_cross_company_patch_is_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOA2")
    company_b = await _create_company(client, "ISOB2")

    create_response = await client.post(
        "/api/v1/customers",
        json={"code": "SECRET2", "name": "Company A's Customer", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    customer_id = create_response.json()["id"]

    cross_patch = await client.patch(
        f"/api/v1/customers/{customer_id}",
        json={"name": "Hijacked"},
        headers=company_headers(company_b),
    )
    assert cross_patch.status_code == 404

    # Prove the row was NOT modified.
    still_own = await client.get(
        f"/api/v1/customers/{customer_id}", headers=company_headers(company_a)
    )
    assert still_own.json()["name"] == "Company A's Customer"


@pytest.mark.asyncio
async def test_customer_cross_company_delete_is_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOA3")
    company_b = await _create_company(client, "ISOB3")

    create_response = await client.post(
        "/api/v1/customers",
        json={"code": "SECRET3", "name": "Company A's Customer", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    customer_id = create_response.json()["id"]

    cross_delete = await client.delete(
        f"/api/v1/customers/{customer_id}", headers=company_headers(company_b)
    )
    assert cross_delete.status_code == 404

    # Still exists for company A.
    still_own = await client.get(
        f"/api/v1/customers/{customer_id}", headers=company_headers(company_a)
    )
    assert still_own.status_code == 200


@pytest.mark.asyncio
async def test_customer_list_never_leaks_across_companies(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOA4")
    company_b = await _create_company(client, "ISOB4")

    await client.post(
        "/api/v1/customers",
        json={"code": "ONLY-A", "name": "A", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )

    list_b = await client.get("/api/v1/customers", headers=company_headers(company_b))
    assert list_b.json() == []


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_cross_company_get_is_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOPA1")
    company_b = await _create_company(client, "ISOPB1")
    uom_id = await _uom_id(client, company_a)

    create_response = await client.post(
        "/api/v1/products",
        json={"sku": "SEC-SKU", "name": "Secret Widget", "uom_id": uom_id},
        headers=company_headers(company_a),
    )
    product_id = create_response.json()["id"]

    cross_read = await client.get(
        f"/api/v1/products/{product_id}", headers=company_headers(company_b)
    )
    assert cross_read.status_code == 404


@pytest.mark.asyncio
async def test_product_cross_company_patch_and_delete_are_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOPA2")
    company_b = await _create_company(client, "ISOPB2")
    uom_id = await _uom_id(client, company_a)

    create_response = await client.post(
        "/api/v1/products",
        json={"sku": "SEC-SKU-2", "name": "Secret Widget 2", "uom_id": uom_id},
        headers=company_headers(company_a),
    )
    product_id = create_response.json()["id"]

    cross_patch = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"name": "Hijacked"},
        headers=company_headers(company_b),
    )
    assert cross_patch.status_code == 404

    cross_delete = await client.delete(
        f"/api/v1/products/{product_id}", headers=company_headers(company_b)
    )
    assert cross_delete.status_code == 404


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_cross_company_get_patch_delete_are_404(client: AsyncClient) -> None:
    company_a = await _create_company(client, "ISOACA")
    company_b = await _create_company(client, "ISOACB")

    create_response = await client.post(
        "/api/v1/accounts",
        json={"code": "9999-SECRET", "name": "Secret Account", "type": "asset"},
        headers=company_headers(company_a),
    )
    account_id = create_response.json()["id"]

    cross_read = await client.get(
        f"/api/v1/accounts/{account_id}", headers=company_headers(company_b)
    )
    assert cross_read.status_code == 404

    cross_patch = await client.patch(
        f"/api/v1/accounts/{account_id}",
        json={"name": "Hijacked"},
        headers=company_headers(company_b),
    )
    assert cross_patch.status_code == 404

    cross_delete = await client.delete(
        f"/api/v1/accounts/{account_id}", headers=company_headers(company_b)
    )
    assert cross_delete.status_code == 404


# ---------------------------------------------------------------------------
# no-context fail-closed behaviour (HTTP layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tenant_endpoints_reject_missing_company_context(client: AsyncClient) -> None:
    for method, path in [
        ("GET", "/api/v1/customers"),
        ("GET", "/api/v1/products"),
        ("GET", "/api/v1/accounts"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 401, f"{method} {path} did not fail-closed"


# ---------------------------------------------------------------------------
# DB-layer fail-closed proof (bypasses HTTP/router entirely)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_query_without_company_context_raises_not_silently_empty(
    db_session: AsyncSession,
) -> None:
    """Directly exercises app.core.db's do_orm_execute hook.

    Master-plan §10.2 requires fail-*closed*: an unbound context must raise,
    not quietly return zero rows (which would be indistinguishable from "no
    data" and easy to miss in a buggy caller).
    """
    with pytest.raises(TenancyContextError):
        await db_session.execute(select(Customer))


@pytest.mark.asyncio
async def test_orm_query_with_company_context_is_filtered(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_a = await _create_company(client, "ISODBA")
    company_b = await _create_company(client, "ISODBB")

    await client.post(
        "/api/v1/customers",
        json={"code": "DBX", "name": "A", "currency_code": "TWD"},
        headers=company_headers(company_a),
    )
    await client.post(
        "/api/v1/customers",
        json={"code": "DBY", "name": "B", "currency_code": "TWD"},
        headers=company_headers(company_b),
    )

    with company_context(company_a):
        result = await db_session.execute(select(Customer))
        rows = result.scalars().all()
    assert {r.code for r in rows} == {"DBX"}


@pytest.mark.asyncio
async def test_cross_company_entitlement_isolation_http(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ADR-009 Decision 8 & 12: A user entitled only to Company A cannot
    read Company B in list_companies, get_company(B) -> 404, nor switch-company to B (403).
    """
    from tests.platform.permissions._helpers import create_user_with_role

    company_a = await _create_company(client, "ENTITA")
    company_b = await _create_company(client, "ENTITB")

    await create_user_with_role(
        db_session,
        email="entitled_user@example.com",
        password="secretpassword123",
        company_id=company_a,
        role_code="admin",
    )

    # Login to Company A
    login_resp = await client.post(
        "/auth/login",
        json={
            "email": "entitled_user@example.com",
            "password": "secretpassword123",
            "company_id": str(company_a),
        },
    )
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. list_companies only returns Company A (entitled), never Company B
    companies_resp = await client.get("/api/v1/companies", headers=headers)
    assert companies_resp.status_code == 200
    returned_ids = {c["id"] for c in companies_resp.json()}
    assert str(company_a) in returned_ids
    assert str(company_b) not in returned_ids

    # 2. get_company on Company B returns 404
    get_b_resp = await client.get(f"/api/v1/companies/{company_b}", headers=headers)
    assert get_b_resp.status_code == 404

    # 3. switch-company to Company B fails with 403 Forbidden
    switch_resp = await client.post(
        "/auth/switch-company",
        json={"company_id": str(company_b)},
        headers=headers,
    )
    assert switch_resp.status_code == 403
