"""Shared HTTP-layer helpers for tests/ledger/* — mirrors the small

per-file `_create_company` helpers in tests/masterdata/*, factored out here
because ledger tests need several more moving parts (accounts, periods,
balanced line payloads) before they can post a single journal entry.
"""

from __future__ import annotations

import uuid
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
