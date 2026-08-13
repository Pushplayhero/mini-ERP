"""ADR-005 R2 — entry_no allocation is gapless even across a rolled-back post.

`service._validate_lines` rejects an unbalanced entry before the sequence
counter is ever touched, so a normal API call that fails validation never
consumes a number in the first place — gapless "for free". To actually
exercise the mechanism the brief asks for (rollback of a *DB-level*
rejection also rolls back the counter increment, per `_allocate_entry_no`'s
docstring), this test bypasses `service.create_journal_entry`'s pre-check
and drives the same low-level building blocks (`_allocate_entry_no` +
direct ORM inserts) to get an unbalanced entry all the way to `commit()`,
where the deferred balance constraint trigger rejects it. After the forced
rollback, a normal, valid API call must still get entry number 1 — proving
the failed attempt left no trace in the counter.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError

from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from app.modules.ledger.models import JournalEntry, JournalLine
from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


@pytest.mark.asyncio
async def test_entry_no_has_no_gap_after_db_level_rejection(
    client: AsyncClient, db_session
) -> None:
    company_id = await create_company(client, "GAPC1")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    period_json = await create_period(client, company_id, 2026, 1)
    entry_date = date(2026, 1, 15)

    with company_context(company_id):
        # Drive the exact same allocation helper `create_journal_entry` uses,
        # but skip `_validate_lines` so an unbalanced entry reaches commit().
        entry_no = await ledger_service._allocate_entry_no(db_session, company_id, entry_date.year)
        assert entry_no == "JE-2026-000001"

        entry = JournalEntry(
            company_id=company_id,
            entry_no=entry_no,
            entry_date=entry_date,
            period_id=uuid.UUID(period_json["id"]),
            reversal_of_id=None,
        )
        entry.lines = [
            JournalLine(
                company_id=company_id,
                account_id=cash,
                line_no=1,
                currency_code="TWD",
                txn_debit=Decimal("100"),
                debit=Decimal("100"),
                rate_date=entry_date,
            ),
            JournalLine(
                company_id=company_id,
                account_id=revenue,
                line_no=2,
                currency_code="TWD",
                txn_credit=Decimal("1"),
                credit=Decimal("1"),
                rate_date=entry_date,
            ),
        ]
        db_session.add(entry)
        with pytest.raises(DBAPIError):
            await db_session.commit()
        await db_session.rollback()

    # The counter increment from the failed attempt above must have rolled
    # back with it: the next *successful* entry (via the real API) still
    # gets number 1, not 2.
    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": entry_date.isoformat(), "lines": balanced_lines(cash, revenue, "50")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["entry_no"] == "JE-2026-000001"


@pytest.mark.asyncio
async def test_entry_no_has_no_gap_after_service_layer_rejection(client: AsyncClient) -> None:
    """The common case: `_validate_lines` fast-fails before touching the DB

    at all, so the counter is never incremented for the rejected attempt.
    """
    company_id = await create_company(client, "GAPC2")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    rejected = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "lines": [
                {
                    "account_id": str(cash),
                    "currency_code": "TWD",
                    "txn_debit": "100",
                    "txn_credit": "0",
                    "debit": "100",
                    "credit": "0",
                    "exchange_rate": "1",
                },
                {
                    "account_id": str(revenue),
                    "currency_code": "TWD",
                    "txn_debit": "0",
                    "txn_credit": "1",
                    "debit": "0",
                    "credit": "1",
                    "exchange_rate": "1",
                },
            ],
        },
        headers=headers,
    )
    assert rejected.status_code == 422

    accepted = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-15", "lines": balanced_lines(cash, revenue, "10")},
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["entry_no"] == "JE-2026-000001"
