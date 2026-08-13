"""/journal-entries API integration tests."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.ledger._helpers import (
    balanced_lines,
    create_account,
    create_company,
    create_period,
    twd_line,
)


@pytest.mark.asyncio
async def test_create_balanced_journal_entry_success(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC1")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash", "asset")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    response = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "lines": balanced_lines(cash, revenue, "1000.000000"),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["entry_no"] == "JE-2026-000001"
    assert entry["company_id"] == str(company_id)
    assert entry["reversal_of_id"] is None
    assert len(entry["lines"]) == 2
    debit_line = next(line for line in entry["lines"] if line["debit"] != "0.000000")
    credit_line = next(line for line in entry["lines"] if line["credit"] != "0.000000")
    assert debit_line["debit"] == "1000.000000"
    assert credit_line["credit"] == "1000.000000"


@pytest.mark.asyncio
async def test_entry_no_increments_per_company_per_year(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC2")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    first = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-05", "lines": balanced_lines(cash, revenue, "100")},
        headers=headers,
    )
    second = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-06", "lines": balanced_lines(cash, revenue, "200")},
        headers=headers,
    )
    assert first.json()["entry_no"] == "JE-2026-000001"
    assert second.json()["entry_no"] == "JE-2026-000002"


@pytest.mark.asyncio
async def test_get_and_list_journal_entries(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC3")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    create_response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-15", "lines": balanced_lines(cash, revenue, "50")},
        headers=headers,
    )
    entry_id = create_response.json()["id"]

    get_response = await client.get(f"/api/v1/journal-entries/{entry_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == entry_id
    assert len(get_response.json()["lines"]) == 2

    list_response = await client.get("/api/v1/journal-entries", headers=headers)
    assert list_response.status_code == 200
    assert any(e["id"] == entry_id for e in list_response.json())


@pytest.mark.asyncio
async def test_get_nonexistent_journal_entry_is_404(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC4")
    bogus_id = uuid.uuid4()
    response = await client.get(
        f"/api/v1/journal-entries/{bogus_id}", headers=company_headers(company_id)
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Rejections — service-layer validation (422)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbalanced_entry_is_rejected_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC5")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    response = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "lines": [twd_line(cash, debit="100"), twd_line(revenue, credit="99")],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_unsupported_currency_is_rejected_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC6")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    await create_period(client, company_id, 2026, 1)

    line = twd_line(cash, debit="100")
    line["currency_code"] = "USD"
    other_line = twd_line(revenue, credit="100")

    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-15", "lines": [line, other_line]},
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_unknown_account_id_is_rejected_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC7")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    await create_period(client, company_id, 2026, 1)

    response = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "lines": balanced_lines(cash, uuid.uuid4(), "100"),
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_cross_company_account_id_is_rejected_422(client: AsyncClient) -> None:
    company_a = await create_company(client, "JEC8A")
    company_b = await create_company(client, "JEC8B")
    cash_a = await create_account(client, company_a, "1000", "Cash")
    revenue_b = await create_account(client, company_b, "4000", "Revenue", "revenue")
    await create_period(client, company_a, 2026, 1)

    response = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "lines": balanced_lines(cash_a, revenue_b, "100"),
        },
        headers=company_headers(company_a),
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_missing_period_is_rejected_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "JEC9")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")
    revenue = await create_account(client, company_id, "4000", "Revenue", "revenue")
    # Deliberately do not create a period for 2026-06.

    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-06-15", "lines": balanced_lines(cash, revenue, "100")},
        headers=headers,
    )
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_single_line_entry_is_rejected_by_schema(client: AsyncClient) -> None:
    """`lines` requires min_length=2 — a single line can never balance."""
    company_id = await create_company(client, "JEC10")
    headers = company_headers(company_id)
    cash = await create_account(client, company_id, "1000", "Cash")

    response = await client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-15", "lines": [twd_line(cash, debit="100")]},
        headers=headers,
    )
    assert response.status_code == 422, response.text
