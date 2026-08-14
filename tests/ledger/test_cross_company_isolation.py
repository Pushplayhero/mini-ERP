"""Multi-company isolation — master-plan §10.2, mirroring

tests/masterdata/test_cross_company_isolation.py for the ledger module's
own tenant-scoped tables: periods and journal entries.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.exceptions import TenancyContextError
from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from app.modules.ledger.models import AccountingPeriod, JournalEntry, JournalLine
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


@pytest.mark.asyncio
async def test_trial_balance_excludes_cross_company_account_bypass_insert(
    client: AsyncClient, db_session
) -> None:
    """Diff-review regression: `get_trial_balance`'s accounts join used to
    filter only `JournalEntry.company_id`, never `_ACCOUNTS.c.company_id` —
    the docstring claimed the latter existed when it didn't.
    `journal_lines.account_id` has only a plain FK to `accounts.id` (not a
    composite, company-scoped FK), so a bypass writer that skips the
    application-layer `_ensure_accounts_belong_to_company` check (as this
    test deliberately does, via raw ORM inserts, mirroring
    `test_immutability.py`'s balance-trigger bypass test) could reference
    another company's account. Prove the trial balance report no longer
    surfaces that other company's account metadata.
    """
    company_a = await create_company(client, "ISOLTB1")
    company_b = await create_company(client, "ISOLTB2")
    cash_a = await create_account(client, company_a, "1000", "Cash")
    revenue_b = await create_account(client, company_b, "4999", "Cross-Company Bogus", "revenue")
    period_a = await create_period(client, company_a, 2026, 1)

    with company_context(company_a):
        entry = JournalEntry(
            company_id=company_a,
            entry_no="JE-2026-999998",
            entry_date=date(2026, 1, 20),
            period_id=uuid.UUID(period_a["id"]),
            reversal_of_id=None,
        )
        entry.lines = [
            JournalLine(
                company_id=company_a,
                account_id=cash_a,
                line_no=1,
                currency_code="TWD",
                txn_debit=Decimal("50"),
                debit=Decimal("50"),
                rate_date=date(2026, 1, 20),
            ),
            JournalLine(
                # Bypass: this line's account belongs to company_b, not
                # company_a. Nothing at the DB layer stops it (plain FK to
                # accounts.id); only the application-layer check — skipped
                # here on purpose — normally would.
                company_id=company_a,
                account_id=revenue_b,
                line_no=2,
                currency_code="TWD",
                txn_credit=Decimal("50"),
                credit=Decimal("50"),
                rate_date=date(2026, 1, 20),
            ),
        ]
        db_session.add(entry)
        await db_session.commit()

        trial_balance = await ledger_service.get_trial_balance(db_session)

    account_ids = {line.account_id for line in trial_balance}
    assert revenue_b not in account_ids, "trial balance leaked another company's account"
