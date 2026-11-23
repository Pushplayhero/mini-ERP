"""Shared HTTP-layer helpers for tests/costing/* (ADR-016)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response

from tests.conftest import company_headers
from tests.ledger._helpers import create_account, create_current_posting_periods
from tests.purchase._helpers import (
    confirm_order,
    create_draft_order,
    create_product,
    create_supplier,
    order_line,
    receive_order,
)
from tests.purchase._helpers import (
    create_company as purchase_create_company,
)

create_company = purchase_create_company


@dataclass
class CostingSetupContext:
    company_id: uuid.UUID
    supplier_id: uuid.UUID
    product_id: uuid.UUID
    order: dict[str, Any]


async def setup_costing_company(
    client: AsyncClient,
    code: str,
    *,
    standard_cost: str = "40",
    list_price: str = "100",
    initial_po_qty: str = "4",
) -> CostingSetupContext:
    """Create company + open period + chart (incl. 5110) + supplier + SKU + RECEIVED PO."""
    company_id = await purchase_create_company(client, code)
    await create_current_posting_periods(client, company_id)
    for account_code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("1300", "Inventory", "asset"),
        ("2000", "Accounts Payable", "liability"),
        ("2110", "Accrued Purchases", "liability"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
        ("5110", "Purchase Price Variance", "expense"),
    ):
        await create_account(client, company_id, account_code, name, acct_type)

    supplier_id = await create_supplier(client, company_id, f"{code}SUP")
    product_id = await create_product(
        client, company_id, f"{code}-SKU", list_price=list_price, standard_cost=standard_cost
    )
    order = await create_received_po(
        client,
        company_id,
        supplier_id,
        [(product_id, initial_po_qty, standard_cost)],
    )
    return CostingSetupContext(
        company_id=company_id,
        supplier_id=supplier_id,
        product_id=product_id,
        order=order,
    )


async def create_received_po(
    client: AsyncClient,
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    lines: list[tuple[uuid.UUID, str, str | None]],
) -> dict[str, Any]:
    po_lines = [order_line(pid, qty, unit_cost=cost) for pid, qty, cost in lines]
    draft = await create_draft_order(client, company_id, supplier_id, po_lines)
    confirm_resp = await confirm_order(client, company_id, draft["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    receive_resp = await receive_order(client, company_id, draft["id"])
    assert receive_resp.status_code == 200, receive_resp.text
    result: dict[str, Any] = receive_resp.json()
    return result


async def get_summaries(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    product_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    params = {"product_id": str(product_id)} if product_id else {}
    resp = await client.get(
        "/api/v1/costing/summaries", params=params, headers=company_headers(company_id)
    )
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, Any]] = resp.json()
    return rows


async def sync_standard_cost(
    client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID
) -> Response:
    return await client.post(
        f"/api/v1/costing/summaries/{product_id}/sync-standard-cost",
        headers=company_headers(company_id),
    )


async def get_cost_moves(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    product_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    params = {"product_id": str(product_id)} if product_id else {}
    resp = await client.get(
        "/api/v1/costing/moves", params=params, headers=company_headers(company_id)
    )
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, Any]] = resp.json()
    return rows


async def create_adjustment(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    adjustment_type: str,
    product_id: uuid.UUID,
    amount: str | Decimal,
    reason: str,
    order_id: str | uuid.UUID | None = None,
    adjustment_date: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, Any] = {
        "adjustment_type": adjustment_type,
        "product_id": str(product_id),
        "amount": str(amount),
        "reason": reason,
    }
    if order_id is not None:
        payload["order_id"] = str(order_id)
    if adjustment_date is not None:
        payload["adjustment_date"] = adjustment_date
    if custom_data is not None:
        payload["custom_data"] = custom_data
    return await client.post(
        "/api/v1/costing/adjustments", json=payload, headers=company_headers(company_id)
    )


async def void_adjustment(
    client: AsyncClient, company_id: uuid.UUID, adjustment_id: str
) -> Response:
    return await client.post(
        f"/api/v1/costing/adjustments/{adjustment_id}/void",
        headers=company_headers(company_id),
    )


async def get_valuation_variance(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    product_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    params = {"product_id": str(product_id)} if product_id else {}
    resp = await client.get(
        "/api/v1/costing/reports/valuation-variance",
        params=params,
        headers=company_headers(company_id),
    )
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, Any]] = resp.json()
    return rows


async def get_trial_balance_by_code(
    client: AsyncClient, company_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    resp = await client.get("/api/v1/reports/trial-balance", headers=company_headers(company_id))
    assert resp.status_code == 200, resp.text
    return {line["account_code"]: line for line in resp.json()}
