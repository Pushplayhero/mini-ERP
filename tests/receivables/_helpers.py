"""Shared HTTP-layer helpers for tests/receivables/* — mirrors

tests/sales/_helpers.py / tests/inventory/_helpers.py, plus a
`setup_shipped_order` convenience that builds the full O2C chain up to a
shipped order (the precondition every invoice test needs).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response

from tests.conftest import company_headers
from tests.ledger._helpers import create_account, create_period
from tests.sales._helpers import (
    confirm_order,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
    ship_order,
)
from tests.sales._helpers import create_company as sales_create_company

create_company = sales_create_company


@dataclass
class ShippedOrderContext:
    company_id: uuid.UUID
    customer_id: uuid.UUID
    product_id: uuid.UUID
    order: dict[str, Any]
    order_total: Decimal


async def setup_shipped_order(
    client: AsyncClient,
    code: str,
    *,
    list_price: str = "100",
    standard_cost: str = "30",
    stock: str = "100",
    qty: str = "2",
    payment_terms_days: int | None = None,
) -> ShippedOrderContext:
    """Company + open period + standard chart (1000/1100/4000/5000/1300) +

    customer + product (seeded with `stock` via a manual adjustment) + one
    order confirmed and shipped for `qty` units. Returns every id/order the
    caller is likely to need.
    """
    from tests.inventory._helpers import create_adjustment

    company_id = await sales_create_company(client, code)
    await create_period(client, company_id, 2026, 8)
    for account_code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
        ("1300", "Inventory", "asset"),
    ):
        await create_account(client, company_id, account_code, name, acct_type)

    customer_id = await create_customer(client, company_id, f"{code}CUST")
    if payment_terms_days is not None:
        resp = await client.patch(
            f"/api/v1/customers/{customer_id}",
            json={"payment_terms_days": payment_terms_days},
            headers=company_headers(company_id),
        )
        assert resp.status_code == 200, resp.text

    product_id = await create_product(
        client, company_id, f"{code}-SKU", list_price=list_price, standard_cost=standard_cost
    )
    adjust_resp = await create_adjustment(client, company_id, product_id, stock, "initial stock")
    assert adjust_resp.status_code == 201, adjust_resp.text

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, qty, unit_price=list_price)]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    ship_resp = await ship_order(client, company_id, order["id"])
    assert ship_resp.status_code == 200, ship_resp.text
    shipped_order = ship_resp.json()

    return ShippedOrderContext(
        company_id=company_id,
        customer_id=customer_id,
        product_id=product_id,
        order=shipped_order,
        order_total=Decimal(shipped_order["total"]),
    )


async def issue_invoice(
    client: AsyncClient,
    company_id: uuid.UUID,
    order_id: str,
    *,
    invoice_date: str | None = None,
    due_date: str | None = None,
) -> Response:
    payload: dict[str, object] = {"order_id": order_id}
    if invoice_date is not None:
        payload["invoice_date"] = invoice_date
    if due_date is not None:
        payload["due_date"] = due_date
    return await client.post(
        "/api/v1/receivables/invoices", json=payload, headers=company_headers(company_id)
    )


async def void_invoice(client: AsyncClient, company_id: uuid.UUID, invoice_id: str) -> Response:
    return await client.post(
        f"/api/v1/receivables/invoices/{invoice_id}/void", headers=company_headers(company_id)
    )


async def create_payment(
    client: AsyncClient,
    company_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount: str | Decimal,
    external_ref: str,
    *,
    allocations: list[dict[str, Any]] | None = None,
) -> Response:
    payload: dict[str, object] = {
        "customer_id": str(customer_id),
        "external_ref": external_ref,
        "amount": str(amount),
    }
    if allocations is not None:
        payload["allocations"] = allocations
    return await client.post(
        "/api/v1/receivables/payments", json=payload, headers=company_headers(company_id)
    )


async def void_payment(client: AsyncClient, company_id: uuid.UUID, payment_id: str) -> Response:
    return await client.post(
        f"/api/v1/receivables/payments/{payment_id}/void", headers=company_headers(company_id)
    )


async def allocate_payment(
    client: AsyncClient,
    company_id: uuid.UUID,
    payment_id: str,
    request_ref: str,
    allocations: list[dict[str, Any]],
) -> Response:
    return await client.post(
        f"/api/v1/receivables/payments/{payment_id}/allocations",
        json={"request_ref": request_ref, "allocations": allocations},
        headers=company_headers(company_id),
    )


def allocation(invoice_id: str, amount: str | Decimal) -> dict[str, Any]:
    return {"invoice_id": invoice_id, "amount": str(amount)}


async def get_ar_aging(
    client: AsyncClient, company_id: uuid.UUID, *, bucket_date: str | None = None
) -> Response:
    params = {"bucket_date": bucket_date} if bucket_date else {}
    return await client.get(
        "/api/v1/receivables/reports/ar-aging",
        params=params,
        headers=company_headers(company_id),
    )


async def get_trial_balance_by_code(
    client: AsyncClient, company_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    resp = await client.get("/api/v1/reports/trial-balance", headers=company_headers(company_id))
    assert resp.status_code == 200, resp.text
    return {line["account_code"]: line for line in resp.json()}
