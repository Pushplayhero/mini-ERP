"""Topological multi-level MRP net-requirements planning tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.mrp._helpers import (
    bom_line,
    calculate_net_requirements,
    confirm_work_order,
    create_bom,
    create_product,
    create_work_order,
    issue_materials,
    seed_stock_via_po,
    setup_mrp_company,
)
from tests.purchase._helpers import confirm_order as confirm_po
from tests.purchase._helpers import create_draft_order as create_po_draft
from tests.purchase._helpers import order_line as po_line
from tests.sales._helpers import confirm_order as confirm_so
from tests.sales._helpers import create_customer
from tests.sales._helpers import create_draft_order as create_so_draft
from tests.sales._helpers import order_line as so_line


@pytest.mark.asyncio
async def test_topological_net_requirements_across_diamond_bom_and_open_orders(
    client: AsyncClient,
) -> None:
    """Diamond BOM nets stock, POs, WOs & SOs without double-counting shared components."""
    ctx = await setup_mrp_company(client, "MRPPLAN")
    raw_shared = await create_product(client, ctx.company_id, "01-RAW-SHD", standard_cost="5")
    raw_bolt = await create_product(client, ctx.company_id, "02-RAW-BLT", standard_cost="2")
    sa_1 = await create_product(client, ctx.company_id, "03-SA1")
    sa_2 = await create_product(client, ctx.company_id, "04-SA2")
    fg = await create_product(client, ctx.company_id, "05-FG", list_price="500")

    # SA1 needs 2 SHARED_RAW; SA2 needs 3 SHARED_RAW + 1 BOLT; FG needs 1 SA1 + 1 SA2 + 2 BOLT
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="B-SA1",
            product_id=sa_1,
            lines=[bom_line(raw_shared, "2")],
        )
    ).status_code == 201
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="B-SA2",
            product_id=sa_2,
            lines=[bom_line(raw_shared, "3"), bom_line(raw_bolt, "1")],
        )
    ).status_code == 201
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="B-FG",
            product_id=fg,
            lines=[bom_line(sa_1, "1"), bom_line(sa_2, "1"), bom_line(raw_bolt, "2")],
        )
    ).status_code == 201

    # Stock on hand: FG=2, SA1=1, SHARED_RAW=15, BOLT=5
    await seed_stock_via_po(
        client,
        ctx.company_id,
        ctx.supplier_id,
        [(fg, "2", "100"), (sa_1, "1", "20"), (raw_shared, "15", "5"), (raw_bolt, "5", "2")],
    )

    # Open CONFIRMED PO: incoming 10 units of RAW_SHARED
    po = await create_po_draft(
        client, ctx.company_id, ctx.supplier_id, [po_line(raw_shared, "10", unit_cost="5")]
    )
    assert (await confirm_po(client, ctx.company_id, po["id"])).status_code == 200

    # Open CONFIRMED WO for SA1 of qty=2, with 1 SHARED_RAW issued (remaining alloc = 3)
    wo_sa1 = (
        await create_work_order(client, ctx.company_id, product_id=sa_1, planned_qty="2")
    ).json()
    assert (await confirm_work_order(client, ctx.company_id, wo_sa1["id"])).status_code == 200
    assert (
        await issue_materials(
            client, ctx.company_id, wo_sa1["id"], request_ref="iss-1", lines=[(raw_shared, "1")]
        )
    ).status_code == 200

    # Confirmed SalesOrder demanding 6 FG, PLUS explicit extra demand of 2 FG => total FG gross = 8
    cust_id = await create_customer(client, ctx.company_id, "PLANCUST")
    so = await create_so_draft(client, ctx.company_id, cust_id, [so_line(fg, "6")])
    assert (await confirm_so(client, ctx.company_id, so["id"])).status_code == 200

    plan_resp = await calculate_net_requirements(
        client,
        ctx.company_id,
        demands=[(fg, "2")],
        include_open_sales_orders=True,
        include_open_purchase_orders=True,
        include_open_work_orders=True,
    )
    assert plan_resp.status_code == 200, plan_resp.text
    by_sku = {r["sku"]: r for r in plan_resp.json()["requirements"]}

    # Verify FG: gross 8, on_hand 2 -> net 6 -> manufacture
    assert Decimal(by_sku["05-FG"]["gross_requirement"]) == Decimal("8")
    assert Decimal(by_sku["05-FG"]["net_requirement"]) == Decimal("6")
    assert by_sku["05-FG"]["action"] == "manufacture"
    assert by_sku["05-FG"]["level"] == 0

    # Verify SA1: gross 6, on_hand 1, open WO SA1 scheduled 2 -> net = 3 -> manufacture
    assert Decimal(by_sku["03-SA1"]["gross_requirement"]) == Decimal("6")
    assert Decimal(by_sku["03-SA1"]["scheduled_receipts_qty"]) == Decimal("2")
    assert Decimal(by_sku["03-SA1"]["net_requirement"]) == Decimal("3")
    assert by_sku["03-SA1"]["action"] == "manufacture"

    # Verify SA2: gross 6, on_hand 0, scheduled 0 -> net = 6 -> manufacture
    assert Decimal(by_sku["04-SA2"]["net_requirement"]) == Decimal("6")
    assert by_sku["04-SA2"]["action"] == "manufacture"

    # Verify DIAMOND SHARED_RAW combined gross:
    # From net SA1 (3*2=6) + net SA2 (6*3=18) + open WO_SA1 unissued commitment (4-1=3) = 27
    # Supply: on_hand 14 (15 - 1 issued to WO_SA1) + confirmed PO 10 = 24 -> Net = 27 - 24 = 3
    assert Decimal(by_sku["01-RAW-SHD"]["gross_requirement"]) == Decimal("27")
    assert Decimal(by_sku["01-RAW-SHD"]["on_hand_qty"]) == Decimal("14")
    assert Decimal(by_sku["01-RAW-SHD"]["scheduled_receipts_qty"]) == Decimal("10")
    assert Decimal(by_sku["01-RAW-SHD"]["net_requirement"]) == Decimal("3")
    assert by_sku["01-RAW-SHD"]["action"] == "purchase"
    assert by_sku["01-RAW-SHD"]["level"] == 2

    # Verify BOLT: from FG (6*2=12) + SA2 (6*1=6) = 18; on_hand 5 -> net = 13 -> purchase
    assert Decimal(by_sku["02-RAW-BLT"]["gross_requirement"]) == Decimal("18")
    assert Decimal(by_sku["02-RAW-BLT"]["net_requirement"]) == Decimal("13")
    assert by_sku["02-RAW-BLT"]["action"] == "purchase"
    assert by_sku["02-RAW-BLT"]["level"] == 2
