"""End-to-end closed-loop P2P -> Costing -> Multi-level MRP -> O2C accounting verification."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.costing._helpers import create_adjustment, get_summaries, get_trial_balance_by_code
from tests.inventory._helpers import on_hand
from tests.mrp._helpers import (
    bom_line,
    calculate_net_requirements,
    confirm_work_order,
    create_bom,
    create_product,
    create_work_order,
    issue_materials,
    receive_production,
    seed_stock_via_po,
    setup_mrp_company,
)
from tests.payables._helpers import (
    allocate_payment as allocate_ap_payment,
)
from tests.payables._helpers import (
    allocation as ap_allocation,
)
from tests.payables._helpers import (
    create_payment as create_ap_payment,
)
from tests.payables._helpers import (
    issue_bill,
)
from tests.receivables._helpers import (
    allocate_payment as allocate_ar_payment,
)
from tests.receivables._helpers import (
    allocation as ar_allocation,
)
from tests.receivables._helpers import (
    create_payment as create_ar_payment,
)
from tests.receivables._helpers import (
    issue_invoice,
)
from tests.sales._helpers import confirm_order as confirm_so
from tests.sales._helpers import create_customer, ship_order
from tests.sales._helpers import create_draft_order as create_so_draft
from tests.sales._helpers import order_line as so_line

_ZERO = Decimal("0.000000")


@pytest.mark.asyncio
async def test_closed_loop_p2p_costing_multilevel_mrp_and_o2c_reconciliation(
    client: AsyncClient,
) -> None:
    """Verify full ERP loop: PO + Landed Cost -> 2-level MRP WOs -> Sales -> AP/AR settlement."""
    ctx = await setup_mrp_company(client, "E2EMFG")
    raw_id = await create_product(client, ctx.company_id, "MFG-RAW", standard_cost="20")
    sub_id = await create_product(client, ctx.company_id, "MFG-SUB", standard_cost="50")
    fg_id = await create_product(
        client, ctx.company_id, "MFG-FG", list_price="300", standard_cost="120"
    )

    # 1. BOMs: SUB uses 2 RAW; FG uses 1 SUB
    assert (
        await create_bom(
            client, ctx.company_id, code="B-SUB", product_id=sub_id, lines=[bom_line(raw_id, "2")]
        )
    ).status_code == 201
    assert (
        await create_bom(
            client, ctx.company_id, code="B-FG", product_id=fg_id, lines=[bom_line(sub_id, "1")]
        )
    ).status_code == 201

    # 2. Run MRP net-requirements before purchasing for demand of 2 FG -> expects 2 FG, 2 SUB, 4 RAW
    plan = await calculate_net_requirements(client, ctx.company_id, demands=[(fg_id, "2")])
    assert plan.status_code == 200
    actions = {
        r["sku"]: (Decimal(r["net_requirement"]), r["action"]) for r in plan.json()["requirements"]
    }
    assert actions["MFG-FG"] == (Decimal("2"), "manufacture")
    assert actions["MFG-SUB"] == (Decimal("2"), "manufacture")
    assert actions["MFG-RAW"] == (Decimal("4"), "purchase")

    # 3. Purchase 4 RAW @ 20 (=80) + Landed Cost +20 (=100 total, MWA=25) + Vendor Bill + Pay (100)
    po = await seed_stock_via_po(client, ctx.company_id, ctx.supplier_id, [(raw_id, "4", "20")])
    assert (
        await create_adjustment(
            client,
            ctx.company_id,
            adjustment_type="landed_cost",
            product_id=raw_id,
            order_id=po["id"],
            amount="20",
            reason="Freight on raw steel",
        )
    ).status_code == 201
    bill = (
        await issue_bill(client, ctx.company_id, po["id"], supplier_invoice_no="MFG-BILL-1")
    ).json()
    ap_pay = (
        await create_ap_payment(
            client, ctx.company_id, ctx.supplier_id, Decimal("80"), "MFG-APPAY-1"
        )
    ).json()
    assert (
        await allocate_ap_payment(
            client,
            ctx.company_id,
            ap_pay["id"],
            "MFG-APALLOC",
            [ap_allocation(bill["id"], Decimal("80"))],
        )
    ).status_code == 200

    # 4. Manufacture 2 SUB from 4 RAW (MWA 25 -> WIP 100 -> 2 SUB @ 50 = 100)
    wo_sub = (
        await create_work_order(client, ctx.company_id, product_id=sub_id, planned_qty="2")
    ).json()
    assert (await confirm_work_order(client, ctx.company_id, wo_sub["id"])).status_code == 200
    assert (
        await issue_materials(client, ctx.company_id, wo_sub["id"], request_ref="sub-iss")
    ).status_code == 200
    assert (
        await receive_production(
            client, ctx.company_id, wo_sub["id"], request_ref="sub-rec", qty="2"
        )
    ).status_code == 200

    # 5. Manufacture 2 FG from 2 SUB (WIP 100 -> 2 FG @ 50 = 100)
    wo_fg = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="2")
    ).json()
    assert (await confirm_work_order(client, ctx.company_id, wo_fg["id"])).status_code == 200
    assert (
        await issue_materials(client, ctx.company_id, wo_fg["id"], request_ref="fg-iss")
    ).status_code == 200
    fg_done = await receive_production(
        client, ctx.company_id, wo_fg["id"], request_ref="fg-rec", qty="2"
    )
    assert fg_done.status_code == 200
    assert fg_done.json()["status"] == "completed"
    assert await on_hand(client, ctx.company_id, fg_id) == Decimal("2")

    # 6. Sync FG standard_cost to actual MWA (50) and sell 1 FG @ 300 -> COGS 50, remaining Inv 50
    sync_resp = await client.post(
        f"/api/v1/costing/summaries/{fg_id}/sync-standard-cost",
        headers=company_headers(ctx.company_id),
    )
    assert sync_resp.status_code == 200

    cust_id = await create_customer(client, ctx.company_id, "MFGCUST")
    so = await create_so_draft(client, ctx.company_id, cust_id, [so_line(fg_id, "1")])
    assert (await confirm_so(client, ctx.company_id, so["id"])).status_code == 200
    assert (await ship_order(client, ctx.company_id, so["id"])).status_code == 200

    inv = (await issue_invoice(client, ctx.company_id, so["id"])).json()
    ar_pay = (
        await create_ar_payment(client, ctx.company_id, cust_id, Decimal("300"), "MFG-ARPAY-1")
    ).json()
    assert (
        await allocate_ar_payment(
            client,
            ctx.company_id,
            ar_pay["id"],
            "MFG-ARALLOC",
            [ar_allocation(inv["id"], Decimal("300"))],
        )
    ).status_code == 200

    # 7. Final financial & subledger reconciliation invariants
    tb = await get_trial_balance_by_code(client, ctx.company_id)
    total_dr = sum((Decimal(v["total_debit"]) for v in tb.values()), _ZERO)
    total_cr = sum((Decimal(v["total_credit"]) for v in tb.values()), _ZERO)
    assert total_dr == total_cr

    inv_gl = Decimal(tb["1300"]["total_debit"]) - Decimal(tb["1300"]["total_credit"])
    wip_gl = Decimal(tb["1310"]["total_debit"]) - Decimal(tb["1310"]["total_credit"])
    ap_gl = Decimal(tb["2000"]["total_credit"]) - Decimal(tb["2000"]["total_debit"])
    ar_gl = Decimal(tb["1100"]["total_debit"]) - Decimal(tb["1100"]["total_credit"])
    cogs_gl = Decimal(tb["5000"]["total_debit"]) - Decimal(tb["5000"]["total_credit"])
    rev_gl = Decimal(tb["4000"]["total_credit"]) - Decimal(tb["4000"]["total_debit"])

    summaries = await get_summaries(client, ctx.company_id)
    subledger_inv_total = sum((Decimal(s["total_cost_value"]) for s in summaries), _ZERO)

    assert inv_gl == subledger_inv_total == Decimal("50")
    assert wip_gl == _ZERO
    assert ap_gl == _ZERO
    assert ar_gl == _ZERO
    assert cogs_gl == Decimal("50")
    assert rev_gl == Decimal("300")
