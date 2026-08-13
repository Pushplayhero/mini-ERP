"""Multi-company isolation — master-plan §10.2, mirroring

tests/masterdata/test_cross_company_isolation.py for the ledger module's
own tenant-scoped tables: periods and journal entries.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.exceptions import TenancyContextError
from app.core.tenancy import company_context
from app.modules.ledger.models import AccountingPeriod, JournalEntry
from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


@pytest.mark.asyncio
async def test_period_cross_company_not_visible_in_list(client: AsyncClient) -> None:
    company_a = await create_company(client, "ISOLA1")
    company_b = await create_company(client, "ISOLB1")

    await create_period(client, company_a, 2026, 1)

    list_b = await client.get("/api/v1/periods", headers=company_headers(company_b))
    assert list_b.json() == []


@pytest.mark.asyncio
async def test_period_close_across_company_is_404(client: AsyncClient) -> None:
    company_a = await create_company(client, "ISOLA2")
    company_b = await create_company(client, "ISOLB2")

    period = await create_period(client, company_a, 2026, 1)

    cross_close = await client.post(
        f"/api/v1/periods/{period['id']}/close", headers=company_headers(company_b)
    )
    assert cross_close.status_code == 404

    # Still open for company A.
    still_open = await client.get("/api/v1/periods", headers=company_headers(company_a))
    assert still_open.json()[0]["status"] == "open"


@pytest.mark.asyncio
async def test_journal_entry_cross_company_get_is_404(client: AsyncClient) -> None:
    company_a = await create_company(client, "ISOLA3")
    company_b = await create_company(client, "ISOLB3")
    cash = await create_account(client, company_a, "1000", "Cash")
    revenue = await create_account(client, company_a, "4000", "Revenue", "revenue")
    await create_period(client, company_a, 2026, 1)

    create_response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, "100")},
        headers=company_headers(company_a),
    )
    entry_id = create_response.json()["id"]

    cross_get = await client.get(
        f"/api/v1/journal-entries/{entry_id}", headers=company_headers(company_b)
    )
    assert cross_get.status_code == 404


@pytest.mark.asyncio
async def test_journal_entry_list_never_leaks_across_companies(client: AsyncClient) -> None:
    company_a = await create_company(client, "ISOLA4")
    company_b = await create_company(client, "ISOLB4")
    cash = await create_account(client, company_a, "1000", "Cash")
    revenue = await create_account(client, company_a, "4000", "Revenue", "revenue")
    await create_period(client, company_a, 2026, 1)

    await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, "100")},
        headers=company_headers(company_a),
    )

    list_b = await client.get("/api/v1/journal-entries", headers=company_headers(company_b))
    assert list_b.json() == []


@pytest.mark.asyncio
async def test_journal_entry_reverse_across_company_is_404(client: AsyncClient) -> None:
    company_a = await create_company(client, "ISOLA5")
    company_b = await create_company(client, "ISOLB5")
    cash = await create_account(client, company_a, "1000", "Cash")
    revenue = await create_account(client, company_a, "4000", "Revenue", "revenue")
    await create_period(client, company_a, 2026, 1)

    create_response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, "100")},
        headers=company_headers(company_a),
    )
    entry_id = create_response.json()["id"]

    cross_reverse = await client.post(
        f"/api/v1/journal-entries/{entry_id}/reverse", headers=company_headers(company_b)
    )
    assert cross_reverse.status_code == 404


@pytest.mark.asyncio
async def test_ledger_endpoints_reject_missing_company_context(client: AsyncClient) -> None:
    for method, path in [
        ("GET", "/api/v1/periods"),
        ("GET", "/api/v1/journal-entries"),
        ("GET", "/api/v1/reports/trial-balance"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 403, f"{method} {path} did not fail-closed"


@pytest.mark.asyncio
async def test_orm_query_without_company_context_raises_for_ledger_models(db_session) -> None:
    with pytest.raises(TenancyContextError):
        await db_session.execute(select(AccountingPeriod))
    with pytest.raises(TenancyContextError):
        await db_session.execute(select(JournalEntry))


@pytest.mark.asyncio
async def test_orm_query_with_company_context_is_filtered_for_periods(
    client: AsyncClient, db_session
) -> None:
    company_a = await create_company(client, "ISOLDBA")
    company_b = await create_company(client, "ISOLDBB")

    await create_period(client, company_a, 2026, 1)
    await create_period(client, company_b, 2026, 1)

    with company_context(company_a):
        result = await db_session.execute(select(AccountingPeriod))
        rows = result.scalars().all()
    assert {r.company_id for r in rows} == {company_a}
