"""Shared HTTP helpers for tests/mrp/* and tests/e2e/test_mfg_end_to_end.py."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response

from tests.conftest import company_headers
from tests.costing._helpers import create_received_po
from tests.ledger._helpers import create_account, create_current_posting_periods
from tests.purchase._helpers import create_company, create_product, create_supplier

_CHART_WITH_WIP = (
    ("1000", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("1300", "Inventory", "asset"),
    ("1310", "Work in Process", "asset"),
    ("2000", "Accounts Payable", "liability"),
    ("2110", "Accrued Purchases", "liability"),
    ("4000", "Revenue", "revenue"),
    ("5000", "COGS", "expense"),
    ("5110", "Purchase Price Variance", "expense"),
)


@dataclass
class MrpSetupContext:
    company_id: uuid.UUID
    supplier_id: uuid.UUID


async def setup_mrp_company(client: AsyncClient, code: str) -> MrpSetupContext:
    """Create company + open UTC periods + full chart incl. 1310 WIP + supplier."""
    company_id = await create_company(client, code)
    await create_current_posting_periods(client, company_id)
    for acct_code, name, acct_type in _CHART_WITH_WIP:
        await create_account(client, company_id, acct_code, name, acct_type)
    supplier_id = await create_supplier(client, company_id, f"{code}SUP")
    return MrpSetupContext(company_id=company_id, supplier_id=supplier_id)


async def seed_stock_via_po(
    client: AsyncClient,
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    items: list[tuple[uuid.UUID, str, str]],
) -> dict[str, Any]:
    return await create_received_po(
        client,
        company_id,
        supplier_id,
        list(items),
    )


async def create_bom(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    code: str,
    product_id: uuid.UUID,
    lines: list[dict[str, Any]],
    output_qty: str | Decimal = "1",
    is_active: bool = True,
    uom_id: uuid.UUID | None = None,
    notes: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, Any] = {
        "code": code,
        "product_id": str(product_id),
        "output_qty": str(output_qty),
        "is_active": is_active,
        "lines": lines,
    }
    if uom_id is not None:
        payload["uom_id"] = str(uom_id)
    if notes is not None:
        payload["notes"] = notes
    if custom_data is not None:
        payload["custom_data"] = custom_data
    return await client.post("/api/v1/mrp/boms", json=payload, headers=company_headers(company_id))


def bom_line(
    component_product_id: uuid.UUID,
    qty_per: str | Decimal,
    *,
    scrap_rate: str | Decimal = "0",
    uom_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "component_product_id": str(component_product_id),
        "qty_per": str(qty_per),
        "scrap_rate": str(scrap_rate),
    }
    if uom_id is not None:
        item["uom_id"] = str(uom_id)
    return item


async def update_bom(
    client: AsyncClient,
    company_id: uuid.UUID,
    bom_id: str | uuid.UUID,
    payload: dict[str, Any],
) -> Response:
    return await client.patch(
        f"/api/v1/mrp/boms/{bom_id}", json=payload, headers=company_headers(company_id)
    )


async def explode_bom(
    client: AsyncClient,
    company_id: uuid.UUID,
    bom_id: str | uuid.UUID,
    *,
    qty: str | None = None,
) -> Response:
    params = {"qty": qty} if qty is not None else {}
    return await client.get(
        f"/api/v1/mrp/boms/{bom_id}/explode",
        params=params,
        headers=company_headers(company_id),
    )


async def create_work_order(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    planned_qty: str | Decimal,
    bom_id: str | uuid.UUID | None = None,
    product_id: str | uuid.UUID | None = None,
    notes: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, Any] = {"planned_qty": str(planned_qty)}
    if bom_id is not None:
        payload["bom_id"] = str(bom_id)
    if product_id is not None:
        payload["product_id"] = str(product_id)
    if notes is not None:
        payload["notes"] = notes
    if custom_data is not None:
        payload["custom_data"] = custom_data
    return await client.post(
        "/api/v1/mrp/work-orders", json=payload, headers=company_headers(company_id)
    )


async def confirm_work_order(
    client: AsyncClient, company_id: uuid.UUID, wo_id: str | uuid.UUID
) -> Response:
    return await client.post(
        f"/api/v1/mrp/work-orders/{wo_id}/confirm", headers=company_headers(company_id)
    )


async def cancel_work_order(
    client: AsyncClient, company_id: uuid.UUID, wo_id: str | uuid.UUID
) -> Response:
    return await client.post(
        f"/api/v1/mrp/work-orders/{wo_id}/cancel", headers=company_headers(company_id)
    )


async def issue_materials(
    client: AsyncClient,
    company_id: uuid.UUID,
    wo_id: str | uuid.UUID,
    *,
    request_ref: str,
    lines: list[tuple[uuid.UUID, str | Decimal]] | None = None,
) -> Response:
    payload: dict[str, Any] = {"request_ref": request_ref}
    if lines is not None:
        payload["lines"] = [{"component_product_id": str(pid), "qty": str(q)} for pid, q in lines]
    return await client.post(
        f"/api/v1/mrp/work-orders/{wo_id}/issue-materials",
        json=payload,
        headers=company_headers(company_id),
    )


async def return_materials(
    client: AsyncClient,
    company_id: uuid.UUID,
    wo_id: str | uuid.UUID,
    *,
    request_ref: str,
    lines: list[tuple[uuid.UUID, str | Decimal]],
) -> Response:
    payload = {
        "request_ref": request_ref,
        "lines": [{"component_product_id": str(pid), "qty": str(q)} for pid, q in lines],
    }
    return await client.post(
        f"/api/v1/mrp/work-orders/{wo_id}/return-materials",
        json=payload,
        headers=company_headers(company_id),
    )


async def receive_production(
    client: AsyncClient,
    company_id: uuid.UUID,
    wo_id: str | uuid.UUID,
    *,
    request_ref: str,
    qty: str | Decimal,
    mark_completed: bool = False,
) -> Response:
    return await client.post(
        f"/api/v1/mrp/work-orders/{wo_id}/receive-production",
        json={"request_ref": request_ref, "qty": str(qty), "mark_completed": mark_completed},
        headers=company_headers(company_id),
    )


async def calculate_net_requirements(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    demands: list[tuple[uuid.UUID, str | Decimal]],
    include_open_sales_orders: bool = False,
    include_open_purchase_orders: bool = True,
    include_open_work_orders: bool = True,
) -> Response:
    return await client.post(
        "/api/v1/mrp/planning/net-requirements",
        json={
            "demands": [{"product_id": str(pid), "qty": str(q)} for pid, q in demands],
            "include_open_sales_orders": include_open_sales_orders,
            "include_open_purchase_orders": include_open_purchase_orders,
            "include_open_work_orders": include_open_work_orders,
        },
        headers=company_headers(company_id),
    )


__all__ = [
    "MrpSetupContext",
    "bom_line",
    "calculate_net_requirements",
    "cancel_work_order",
    "confirm_work_order",
    "create_bom",
    "create_product",
    "create_work_order",
    "explode_bom",
    "issue_materials",
    "receive_production",
    "return_materials",
    "seed_stock_via_po",
    "setup_mrp_company",
    "update_bom",
]
