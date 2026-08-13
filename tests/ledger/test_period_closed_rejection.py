"""Posting to a closed accounting period must be rejected (ADR-005 R4)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.ledger._helpers import balanced_lines, create_account, create_company, create_period


@pytest.mark.asyncio
async def test_posting_to_a_closed_period_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "CLOSEC1")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    period = await create_period(client, company_id, 2026, 7)

    close_response = await client.post(f"/api/v1/periods/{period['id']}/close", headers=headers)
    assert close_response.status_code == 200

    post_response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-07-15", "lines": balanced_lines(cash, revenue, "500")},
        headers=headers,
    )
    assert post_response.status_code == 409, post_response.text


@pytest.mark.asyncio
async def test_period_open_for_one_month_does_not_cover_another(client: AsyncClient) -> None:
    """Opening 2026-08 must not make 2026-09 postable — periods are exact matches."""
    company_id = await create_company(client, "CLOSEC2")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 8)

    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-09-01", "lines": balanced_lines(cash, revenue, "100")},
        headers=headers,
    )
    assert response.status_code == 409, response.text
