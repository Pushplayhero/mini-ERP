"""POST /journal-entries/{id}/reverse — ADR-005 R3.

Reversal is dated "today" (see `service.reverse_journal_entry` docstring for
the rationale), so every test here also opens the *current* period, not a
fixed one, otherwise the reversal call itself would 409 on period
resolution rather than exercising the behaviour under test.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient

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
