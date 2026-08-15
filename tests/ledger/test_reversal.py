"""POST /journal-entries/{id}/reverse — ADR-005 R3.

Reversal is dated "today" (see `service.reverse_journal_entry` docstring for
the rationale), so every test here also opens the *current* period, not a
fixed one, otherwise the reversal call itself would 409 on period
resolution rather than exercising the behaviour under test.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import company_context
from app.modules.ledger.schemas import JournalEntryCreate, JournalLineCreate
from app.modules.ledger.service import post_journal_entry
from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


async def _post_original_entry(client: AsyncClient, company_id, cash, revenue) -> dict:
    today = date.today()
    await create_period(client, company_id, today.year, today.month)
    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": today.isoformat(), "lines": balanced_lines(cash, revenue, "300.00")},
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_reverse_swaps_debit_and_credit(client: AsyncClient) -> None:
    company_id = await create_company(client, "REVC1")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")

    original = await _post_original_entry(client, company_id, cash, revenue)

    reverse_response = await client.post(
        f"/api/v1/journal-entries/{original['id']}/reverse", headers=headers
    )
    assert reverse_response.status_code == 201, reverse_response.text
    reversal = reverse_response.json()

    assert reversal["reversal_of_id"] == original["id"]
    assert reversal["entry_no"] != original["entry_no"]
    assert reversal["entry_date"] == date.today().isoformat()

    original_debit_line = next(line for line in original["lines"] if line["debit"] != "0.000000")
    original_credit_line = next(line for line in original["lines"] if line["credit"] != "0.000000")
    reversal_by_account = {line["account_id"]: line for line in reversal["lines"]}

    # The account that was debited in the original entry is credited in the
    # reversal, for the same amount, and vice versa.
    assert (
        reversal_by_account[original_debit_line["account_id"]]["credit"]
        == original_debit_line["debit"]
    )
    assert reversal_by_account[original_debit_line["account_id"]]["debit"] == "0.000000"
    assert (
        reversal_by_account[original_credit_line["account_id"]]["debit"]
        == original_credit_line["credit"]
    )
    assert reversal_by_account[original_credit_line["account_id"]]["credit"] == "0.000000"


@pytest.mark.asyncio
async def test_reversing_an_already_reversed_entry_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "REVC2")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")

    original = await _post_original_entry(client, company_id, cash, revenue)

    first_reverse = await client.post(
        f"/api/v1/journal-entries/{original['id']}/reverse", headers=headers
    )
    assert first_reverse.status_code == 201

    second_reverse = await client.post(
        f"/api/v1/journal-entries/{original['id']}/reverse", headers=headers
    )
    assert second_reverse.status_code == 409, second_reverse.text


@pytest.mark.asyncio
async def test_reversing_a_reversal_entry_itself_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "REVC3")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")

    original = await _post_original_entry(client, company_id, cash, revenue)
    reversal = (
        await client.post(f"/api/v1/journal-entries/{original['id']}/reverse", headers=headers)
    ).json()

    reverse_of_reversal = await client.post(
        f"/api/v1/journal-entries/{reversal['id']}/reverse", headers=headers
    )
    assert reverse_of_reversal.status_code == 409, reverse_of_reversal.text


@pytest.mark.asyncio
async def test_reversing_an_event_sourced_entry_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Diff-review regression (Week 2-4 consensus review, Critical finding):
    `_post_reversal` used to copy `source_type`/`source_id` from the
    original entry onto the reversal, colliding with
    `uq_journal_entries_source` (scoped to `(company_id, source_type,
    source_id)`) — every reversal of an event-sourced entry (i.e. every
    entry the posting engine ever creates, including all of Week 5's
    shipment COGS postings) failed. The fix sets the reversal's
    `source_type`/`source_id` to `None`; traceability is preserved via
    `reversal_of_id` alone.

    ADR-008 R11 made `source_type`/`source_id` server-only, so this event-
    sourced original can no longer be built through the public
    `POST /journal-entries` endpoint (see `test_journal_entries_api.py
    ::test_http_supplied_source_fields_are_ignored_not_a_duplicate_conflict`
    for that behavior) — it is built the way every real posting event
    actually does, via `ledger.service.post_journal_entry` directly, with
    `1000`/`4000` (neither a control account, so R16 does not block
    reversing it — that restriction is exercised separately by
    `tests/receivables/test_invoices.py
    ::test_manual_reversal_of_control_account_entry_is_rejected`).
    """
    company_id = await create_company(client, "REVC4")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    today = date.today()
    await create_period(client, company_id, today.year, today.month)

    with company_context(company_id):
        original_entry = await post_journal_entry(
            db_session,
            JournalEntryCreate(
                entry_date=today,
                source_type="sales.goods_shipped",
                source_id=uuid.uuid4(),
                lines=[
                    JournalLineCreate(
                        account_id=cash,
                        currency_code="TWD",
                        txn_debit="300.00",
                        debit="300.00",
                    ),
                    JournalLineCreate(
                        account_id=revenue,
                        currency_code="TWD",
                        txn_credit="300.00",
                        credit="300.00",
                    ),
                ],
            ),
        )
        await db_session.commit()
    original = {"id": str(original_entry.id), "source_type": original_entry.source_type}
    assert original["source_type"] == "sales.goods_shipped"

    reverse_response = await client.post(
        f"/api/v1/journal-entries/{original['id']}/reverse", headers=headers
    )
    assert reverse_response.status_code == 201, reverse_response.text
    reversal = reverse_response.json()

    assert reversal["reversal_of_id"] == original["id"]
    # The reversal carries no source of its own — it wasn't triggered by
    # replaying an event, a human/service action reversed it directly.
    # Traceability to the original event is still available by following
    # reversal_of_id -> original.source_type/source_id.
    assert reversal["source_type"] is None
    assert reversal["source_id"] is None
