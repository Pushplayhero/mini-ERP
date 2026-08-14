"""Diff-review regression: `CompanyCreate.functional_currency_code` must be
restricted to Phase 1's only supported functional currency (ADR-005 R1).

Before this fix, masterdata allowed a company to be created with any
registered currency (`functional_currency_code` was only length-validated),
while `ledger.service` separately hardcoded `FUNCTIONAL_CURRENCY = "TWD"` —
the two modules disagreed about what Phase 1 actually supports, and a
non-TWD company's ledger would either reject every legitimate journal line
or (worse) silently treat a non-functional-currency amount as functional.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_company_with_non_twd_functional_currency_is_422(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/companies",
        json={"code": "USDCO", "name": "USD Co.", "functional_currency_code": "USD"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_company_with_twd_functional_currency_still_succeeds(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/companies",
        json={"code": "TWDCO", "name": "TWD Co.", "functional_currency_code": "TWD"},
    )
    assert response.status_code == 201, response.text
