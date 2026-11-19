"""Shared HTTP-layer helpers for tests/payables/* (ADR-015)."""

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
class ReceivedPOContext:
    company_id: uuid.UUID
    supplier_id: uuid.UUID
    product_id: uuid.UUID
    order: dict[str, Any]
    order_total: Decimal


async def setup_received_order(
    client: AsyncClient,
    code: str,
    *,
    standard_cost: str = "40",
    qty: str = "3",
    payment_terms_days: int | None = None,
) -> ReceivedPOContext:
    """Company + open period + chart (1000/1300/2000/2110) + supplier + product + RECEIVED PO."""
    company_id = await purchase_create_company(client, code)
    await create_current_posting_periods(client, company_id)
    for account_code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1300", "Inventory", "asset"),
        ("2000", "Accounts Payable", "liability"),
        ("2110", "Accrued Purchases", "liability"),
    ):
        await create_account(client, company_id, account_code, name, acct_type)

    supplier_id = await create_supplier(client, company_id, f"{code}SUP")
    if payment_terms_days is not None:
        resp = await client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"payment_terms_days": payment_terms_days},
            headers=company_headers(company_id),
        )
        assert resp.status_code == 200, resp.text

    product_id = await create_product(
        client, company_id, f"{code}-SKU", list_price="100", standard_cost=standard_cost
    )
    order = await create_draft_order(
        client, company_id, supplier_id, [order_line(product_id, qty, unit_cost=standard_cost)]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    receive_resp = await receive_order(client, company_id, order["id"])
    assert receive_resp.status_code == 200, receive_resp.text
    received_order = receive_resp.json()

    return ReceivedPOContext(
        company_id=company_id,
        supplier_id=supplier_id,
        product_id=product_id,
        order=received_order,
        order_total=Decimal(received_order["total"]),
    )


async def issue_bill(
    client: AsyncClient,
    company_id: uuid.UUID,
    order_id: str,
    *,
    supplier_invoice_no: str | None = None,
    bill_date: str | None = None,
    due_date: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, object] = {"order_id": order_id}
    if supplier_invoice_no is not None:
        payload["supplier_invoice_no"] = supplier_invoice_no
    if bill_date is not None:
        payload["bill_date"] = bill_date
    if due_date is not None:
        payload["due_date"] = due_date
    if custom_data is not None:
        payload["custom_data"] = custom_data
    return await client.post(
        "/api/v1/payables/bills", json=payload, headers=company_headers(company_id)
    )


async def void_bill(client: AsyncClient, company_id: uuid.UUID, bill_id: str) -> Response:
    return await client.post(
        f"/api/v1/payables/bills/{bill_id}/void", headers=company_headers(company_id)
    )


async def create_payment(
    client: AsyncClient,
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    amount: str | Decimal,
    external_ref: str,
    *,
    allocations: list[dict[str, Any]] | None = None,
    custom_data: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, object] = {
        "supplier_id": str(supplier_id),
        "external_ref": external_ref,
        "amount": str(amount),
    }
    if allocations is not None:
        payload["allocations"] = allocations
    if custom_data is not None:
        payload["custom_data"] = custom_data
    return await client.post(
        "/api/v1/payables/payments", json=payload, headers=company_headers(company_id)
    )


async def void_payment(client: AsyncClient, company_id: uuid.UUID, payment_id: str) -> Response:
    return await client.post(
        f"/api/v1/payables/payments/{payment_id}/void", headers=company_headers(company_id)
    )


async def allocate_payment(
    client: AsyncClient,
    company_id: uuid.UUID,
    payment_id: str,
    request_ref: str,
    allocations: list[dict[str, Any]],
) -> Response:
    return await client.post(
        f"/api/v1/payables/payments/{payment_id}/allocations",
        json={"request_ref": request_ref, "allocations": allocations},
        headers=company_headers(company_id),
    )


def allocation(bill_id: str, amount: str | Decimal) -> dict[str, Any]:
    return {"bill_id": bill_id, "amount": str(amount)}


async def get_ap_aging(
    client: AsyncClient, company_id: uuid.UUID, *, bucket_date: str | None = None
) -> Response:
    params = {"bucket_date": bucket_date} if bucket_date else {}
    return await client.get(
        "/api/v1/payables/reports/ap-aging",
        params=params,
        headers=company_headers(company_id),
    )


async def get_trial_balance_by_code(
    client: AsyncClient, company_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    resp = await client.get("/api/v1/reports/trial-balance", headers=company_headers(company_id))
    assert resp.status_code == 200, resp.text
    return {line["account_code"]: line for line in resp.json()}
