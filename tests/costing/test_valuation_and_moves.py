"""Moving weighted average valuation, shipment relief, standard-cost fallback & replay tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import replay_outbox
from app.modules.masterdata.models import OutboxEvent
from tests.conftest import company_headers
from tests.costing._helpers import (
    create_received_po,
    get_cost_moves,
    get_summaries,
    get_valuation_variance,
    setup_costing_company,
    sync_standard_cost,
)
from tests.sales._helpers import (
    confirm_order as confirm_sales_order,
)
from tests.sales._helpers import (
    create_customer,
    ship_order,
)
from tests.sales._helpers import (
    create_draft_order as create_sales_draft,
)
from tests.sales._helpers import (
    order_line as sales_order_line,
)


@pytest.mark.asyncio
async def test_moving_average_across_multiple_receipts_shipment_and_standard_cost_sync(
    client: AsyncClient,
) -> None:
    """PO receipts update moving_avg_cost; shipments relieve MWA; sync updates SKU."""
    ctx = await setup_costing_company(
        client, "MWACOMP", standard_cost="40", list_price="100", initial_po_qty="4"
    )

    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert len(summaries) == 1
    assert Decimal(summaries[0]["tracked_qty"]) == Decimal("4")
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("160")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("40")

    # PO2: 6 units at override unit_cost = 60 -> (4*40 + 6*60) / 10 = 520 / 10 = 52.000000
    await create_received_po(
        client,
        ctx.company_id,
        ctx.supplier_id,
        [(ctx.product_id, "6", "60")],
    )
    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries[0]["tracked_qty"]) == Decimal("10")
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("520")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("52")

    # Ship 3 units via sales order -> relieves 3 * 52 = 156, leaving 7 units / 364 / avg 52
    cust_id = await create_customer(client, ctx.company_id, "MWACUST")
    so = await create_sales_draft(
        client, ctx.company_id, cust_id, [sales_order_line(ctx.product_id, "3")]
    )
    assert (await confirm_sales_order(client, ctx.company_id, so["id"])).status_code == 200
    assert (await ship_order(client, ctx.company_id, so["id"])).status_code == 200

    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries[0]["tracked_qty"]) == Decimal("7")
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("364")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("52")

    moves = await get_cost_moves(client, ctx.company_id, product_id=ctx.product_id)
    assert [m["move_type"] for m in moves] == ["receipt", "receipt", "shipment"]
    assert Decimal(moves[-1]["qty_delta"]) == Decimal("-3")
    assert Decimal(moves[-1]["value_delta"]) == Decimal("-156")
    assert Decimal(moves[-1]["unit_cost"]) == Decimal("52")

    # Valuation variance report shows 7 * (52 - 40) = 84 total variance before sync
    report = await get_valuation_variance(client, ctx.company_id, product_id=ctx.product_id)
    assert len(report) == 1
    assert Decimal(report[0]["standard_cost"]) == Decimal("40")
    assert Decimal(report[0]["moving_avg_cost"]) == Decimal("52")
    assert Decimal(report[0]["valuation_variance"]) == Decimal("84")
    assert Decimal(report[0]["unit_cost_variance"]) == Decimal("12")

    # Sync moving_avg_cost (52) into product.standard_cost
    sync_resp = await sync_standard_cost(client, ctx.company_id, ctx.product_id)
    assert sync_resp.status_code == 200, sync_resp.text
    prod_resp = await client.get(
        f"/api/v1/products/{ctx.product_id}", headers=company_headers(ctx.company_id)
    )
    assert Decimal(prod_resp.json()["standard_cost"]) == Decimal("52")

    report_after = await get_valuation_variance(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(report_after[0]["valuation_variance"]) == Decimal("0")
    assert Decimal(report_after[0]["unit_cost_variance"]) == Decimal("0")


@pytest.mark.asyncio
async def test_shipment_standard_cost_fallback_on_manual_adjustment_stock(
    client: AsyncClient,
) -> None:
    """Manual inventory adjustment without PO receipt falls back smoothly on SO shipment."""
    from tests.purchase._helpers import create_company, create_product

    company_id = await create_company(client, "FALLBACKCO")
    from tests.ledger._helpers import create_account, create_current_posting_periods

    await create_current_posting_periods(client, company_id)
    await create_account(client, company_id, "1300", "Inventory", "asset")
    await create_account(client, company_id, "5000", "COGS", "expense")

    product_id = await create_product(
        client, company_id, "FB-SKU", list_price="100", standard_cost="25"
    )
    adj_resp = await client.post(
        "/api/v1/inventory/adjustments",
        json={"product_id": str(product_id), "qty_delta": "5", "reason": "Opening stock"},
        headers=company_headers(company_id),
    )
    assert adj_resp.status_code == 201, adj_resp.text

    cust_id = await create_customer(client, company_id, "FBCUST")
    so = await create_sales_draft(client, company_id, cust_id, [sales_order_line(product_id, "2")])
    assert (await confirm_sales_order(client, company_id, so["id"])).status_code == 200
    assert (await ship_order(client, company_id, so["id"])).status_code == 200

    summaries = await get_summaries(client, company_id, product_id=product_id)
    assert len(summaries) == 1
    assert Decimal(summaries[0]["tracked_qty"]) == Decimal("0")
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("0")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("0")

    moves = await get_cost_moves(client, company_id, product_id=product_id)
    assert len(moves) == 1
    assert moves[0]["move_type"] == "shipment"
    assert Decimal(moves[0]["qty_delta"]) == Decimal("0")
    assert Decimal(moves[0]["value_delta"]) == Decimal("0")
    assert Decimal(moves[0]["unit_cost"]) == Decimal("25")

    # Syncing standard_cost before any tracked PO history fails with 422
    sync_resp = await sync_standard_cost(client, company_id, product_id)
    assert sync_resp.status_code == 422


@pytest.mark.asyncio
async def test_cost_moves_replay_idempotency(client: AsyncClient, db_session: AsyncSession) -> None:
    """Replaying purchase.goods_received and sales.goods_shipped is an exact no-op for costing."""
    ctx = await setup_costing_company(
        client, "REPLAYCO", standard_cost="30", list_price="100", initial_po_qty="5"
    )
    cust_id = await create_customer(client, ctx.company_id, "RPLCUST")
    so = await create_sales_draft(
        client, ctx.company_id, cust_id, [sales_order_line(ctx.product_id, "2")]
    )
    assert (await confirm_sales_order(client, ctx.company_id, so["id"])).status_code == 200
    assert (await ship_order(client, ctx.company_id, so["id"])).status_code == 200

    moves_before = await get_cost_moves(client, ctx.company_id, product_id=ctx.product_id)
    summaries_before = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert len(moves_before) == 2

    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.event_type.in_(["purchase.goods_received", "sales.goods_shipped"]))
        .values(dispatched_at=None)
    )
    await db_session.commit()

    replay_summary = await replay_outbox.replay_outbox()
    assert replay_summary.failed == 0

    moves_after = await get_cost_moves(client, ctx.company_id, product_id=ctx.product_id)
    summaries_after = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert len(moves_after) == 2
    assert summaries_after == summaries_before


@pytest.mark.asyncio
async def test_bankers_rounding_on_repeating_unit_cost(client: AsyncClient) -> None:
    """Moving weighted average uses ROUND_HALF_EVEN to 6 decimal places."""
    ctx = await setup_costing_company(
        client, "RNDCO", standard_cost="100", list_price="200", initial_po_qty="3"
    )
    # Receive 2 units at 10 (=20), then 1 unit at 11 -> total 31 / 3 = 10.333333 (ROUND_HALF_EVEN)
    p2_resp = await client.post(
        "/api/v1/products",
        json={
            "sku": "RND-2",
            "name": "Rounding Item",
            "uom_id": ctx.order["lines"][0]["uom_id"],
            "list_price": "50",
            "standard_cost": "10",
        },
        headers=company_headers(ctx.company_id),
    )
    assert p2_resp.status_code == 201
    p2_id = uuid.UUID(p2_resp.json()["id"])

    await create_received_po(client, ctx.company_id, ctx.supplier_id, [(p2_id, "2", "10")])
    await create_received_po(client, ctx.company_id, ctx.supplier_id, [(p2_id, "1", "11")])
    summaries = await get_summaries(client, ctx.company_id, product_id=p2_id)
    assert Decimal(summaries[0]["tracked_qty"]) == Decimal("3")
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("31")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("10.333333")
