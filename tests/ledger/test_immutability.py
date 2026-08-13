"""ADR-005 Decision 3 — journal_entries/journal_lines are immutable at the

DB layer, not just "no endpoint for it". These tests bypass the service and
router entirely and issue raw SQL UPDATE/DELETE against a live session, to
prove the `BEFORE UPDATE OR DELETE` triggers block mutation even for a
writer that isn't this application's service layer at all (e.g. a future
plugin, a bulk-import script, or a careless manual fix in psql).
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


async def _post_entry(client: AsyncClient, company_id, cash, revenue) -> dict:
    await create_period(client, company_id, 2026, 1)
    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, "77.00")},
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_direct_sql_update_on_journal_entries_is_blocked(client, db_session) -> None:
    company_id = await create_company(client, "IMMC1")
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    entry = await _post_entry(client, company_id, cash, revenue)

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("UPDATE journal_entries SET entry_no = 'HACKED' WHERE id = :id"),
            {"id": entry["id"]},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_direct_sql_delete_on_journal_entries_is_blocked(client, db_session) -> None:
    company_id = await create_company(client, "IMMC2")
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    entry = await _post_entry(client, company_id, cash, revenue)

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("DELETE FROM journal_entries WHERE id = :id"), {"id": entry["id"]}
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_direct_sql_update_on_journal_lines_is_blocked(client, db_session) -> None:
    company_id = await create_company(client, "IMMC3")
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    entry = await _post_entry(client, company_id, cash, revenue)
    line_id = entry["lines"][0]["id"]

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("UPDATE journal_lines SET debit = 999999 WHERE id = :id"), {"id": line_id}
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_direct_sql_delete_on_journal_lines_is_blocked(client, db_session) -> None:
    company_id = await create_company(client, "IMMC4")
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    entry = await _post_entry(client, company_id, cash, revenue)
    line_id = entry["lines"][0]["id"]

    with pytest.raises(DBAPIError):
        await db_session.execute(text("DELETE FROM journal_lines WHERE id = :id"), {"id": line_id})
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unbalanced_insert_is_rejected_by_db_trigger_defense_in_depth(
    client: AsyncClient, db_session
) -> None:
    """Bypasses `service._validate_lines` entirely (raw ORM inserts) to prove

    the deferred balance constraint trigger (ADR-005 Decision 2) is a real,
    independent backstop and not just a description of what the service
    layer already does.
    """
    from decimal import Decimal

    from app.core.tenancy import company_context
    from app.modules.ledger.models import JournalEntry, JournalLine
    from app.modules.ledger.service import get_period

    company_id = await create_company(client, "IMMC5")
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    period_json = await create_period(client, company_id, 2026, 1)

    with company_context(company_id):
        period = await get_period(db_session, period_json["id"])
        entry = JournalEntry(
            company_id=company_id,
            entry_no="JE-2026-999999",
            entry_date=date(2026, 1, 20),
            period_id=period.id,
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
                rate_date=date(2026, 1, 20),
            ),
            JournalLine(
                company_id=company_id,
                account_id=revenue,
                line_no=2,
                currency_code="TWD",
                txn_credit=Decimal("40"),
                credit=Decimal("40"),
                rate_date=date(2026, 1, 20),
            ),
        ]
        db_session.add(entry)
        with pytest.raises(DBAPIError):
            await db_session.commit()
        await db_session.rollback()
