"""GET /reports/trial-balance — ADR-005 Decision 4 (computed on the fly)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


@pytest.mark.asyncio
async def test_trial_balance_sums_by_account(client: AsyncClient) -> None:
    company_id = await create_company(client, "TBC1")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    for amount in ("100.00", "250.50", "49.50"):
        response = await client.post(
            "/api/v1/journal-entries",
            json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, amount)},
            headers=headers,
        )
        assert response.status_code == 201, response.text

    tb_response = await client.get("/api/v1/reports/trial-balance", headers=headers)
    assert tb_response.status_code == 200
    lines = {line["account_code"]: line for line in tb_response.json()}

    assert lines["1000"]["total_debit"] == "400.000000"
    assert lines["1000"]["total_credit"] == "0.000000"
    assert lines["4000"]["total_debit"] == "0.000000"
    assert lines["4000"]["total_credit"] == "400.000000"

    total_debit = sum(Decimal(line["total_debit"]) for line in lines.values())
    total_credit = sum(Decimal(line["total_credit"]) for line in lines.values())
    assert total_debit == total_credit == Decimal("400.00")


@pytest.mark.asyncio
async def test_trial_balance_filtered_by_period(client: AsyncClient) -> None:
    company_id = await create_company(client, "TBC2")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    jan = await create_period(client, company_id, 2026, 1)
    await create_period(client, company_id, 2026, 2)

    await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash, revenue, "100")},
        headers=headers,
    )
    await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-02-10", "lines": balanced_lines(cash, revenue, "300")},
        headers=headers,
    )

    jan_only = await client.get(
        "/api/v1/reports/trial-balance", params={"period_id": jan["id"]}, headers=headers
    )
    assert jan_only.status_code == 200
    jan_lines = {line["account_code"]: line for line in jan_only.json()}
    assert jan_lines["1000"]["total_debit"] == "100.000000"

    all_history = await client.get("/api/v1/reports/trial-balance", headers=headers)
    all_lines = {line["account_code"]: line for line in all_history.json()}
    assert all_lines["1000"]["total_debit"] == "400.000000"


@pytest.mark.asyncio
async def test_trial_balance_never_leaks_across_companies(client: AsyncClient) -> None:
    company_a = await create_company(client, "TBC3A")
    company_b = await create_company(client, "TBC3B")
    cash_a = await create_account(client, company_a, "1000", "Cash")
    revenue_a = await create_account(client, company_a, "4000", "Revenue", "revenue")
    await create_period(client, company_a, 2026, 1)

    await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-10", "lines": balanced_lines(cash_a, revenue_a, "999")},
        headers=company_headers(company_a),
    )

    tb_b = await client.get("/api/v1/reports/trial-balance", headers=company_headers(company_b))
    assert tb_b.status_code == 200
    assert tb_b.json() == []
