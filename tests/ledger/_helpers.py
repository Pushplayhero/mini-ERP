"""Shared HTTP-layer helpers for tests/ledger/* — mirrors the small

per-file `_create_company` helpers in tests/masterdata/*, factored out here
because ledger tests need several more moving parts (accounts, periods,
balanced line payloads) before they can post a single journal entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from httpx import AsyncClient

from tests.conftest import company_headers


async def create_company(client: AsyncClient, code: str) -> uuid.UUID:
    from tests._auth_helpers import setup_company_with_admin

    auth = await setup_company_with_admin(client, code=code)
    return auth.company_id


async def create_account(
    client: AsyncClient, company_id: uuid.UUID, code: str, name: str, acct_type: str = "asset"
) -> uuid.UUID:
    response = await client.post(
        "/api/v1/accounts",
        json={"code": code, "name": name, "type": acct_type},
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def create_period(
    client: AsyncClient, company_id: uuid.UUID, year: int, month: int
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/periods",
        json={"year": year, "month": month},
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def create_current_posting_periods(client: AsyncClient, company_id: uuid.UUID) -> None:
    """Open the nearby months used by service-generated posting dates.

    Invoice and reversal defaults use the host's local date, while shipment
    events use a UTC timestamp. Include adjacent months so a test that crosses
    a month boundary during setup still has an open period for its postings.
    """
    reference_dates = (date.today(), datetime.now(UTC).date())
    periods: set[tuple[int, int]] = set()
    for reference_date in reference_dates:
        first_of_month = reference_date.replace(day=1)
        previous_month = first_of_month - timedelta(days=1)
        next_month = (first_of_month + timedelta(days=32)).replace(day=1)
        periods.update(
            {
                (previous_month.year, previous_month.month),
                (reference_date.year, reference_date.month),
                (next_month.year, next_month.month),
            }
        )

    for year, month in sorted(periods):
        await create_period(client, company_id, year, month)


def twd_line(
    account_id: uuid.UUID, *, debit: Decimal | str = "0", credit: Decimal | str = "0"
) -> dict[str, Any]:
    """Build a Phase-1 (TWD-only) journal line payload: txn amounts always

    mirror the functional amounts, exchange_rate is always 1.
    """
    debit = Decimal(debit)
    credit = Decimal(credit)
    return {
        "account_id": str(account_id),
        "currency_code": "TWD",
        "txn_debit": str(debit),
        "txn_credit": str(credit),
        "debit": str(debit),
        "credit": str(credit),
        "exchange_rate": "1",
    }


def balanced_lines(
    debit_account_id: uuid.UUID, credit_account_id: uuid.UUID, amount: str
) -> list[dict[str, Any]]:
    return [
        twd_line(debit_account_id, debit=amount),
        twd_line(credit_account_id, credit=amount),
    ]
