"""Cost adjustment (Landed Cost, PPV, Revaluation), ledger, workflow & batch export tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context
from app.core.tenancy import company_context
from app.platform.workflow import service as wf_service
from app.platform.workflow.schemas import (
    WorkflowDefinitionCreate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)
from tests.conftest import TEST_ADMIN_ACTOR, company_headers
from tests.costing._helpers import (
    create_adjustment,
    get_cost_moves,
    get_summaries,
    get_trial_balance_by_code,
    get_valuation_variance,
    setup_costing_company,
    void_adjustment,
)
from tests.platform.permissions._helpers import create_user_with_role


@pytest.mark.asyncio
async def test_landed_cost_capitalization_ledger_and_void_lifecycle(
    client: AsyncClient,
) -> None:
    """LANDED_COST capitalizes Inventory & 2110, updates avg cost, and reverses cleanly on void."""
    ctx = await setup_costing_company(
        client, "LANDCO", standard_cost="40", list_price="100", initial_po_qty="4"
    )
    # Initial receipt posted Dr 1300 160 / Cr 2110 160
    lc_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="landed_cost",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="20",
        reason="Customs & ocean freight allocation",
    )
    assert lc_resp.status_code == 201, lc_resp.text
    lc = lc_resp.json()
    assert lc["adjustment_no"].startswith("COST-")
    assert lc["status"] == "posted"

    # Summary: qty 4, value 160 + 20 = 180, avg 45.000000
    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("180")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("45")

    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb["1300"]["total_debit"]) == Decimal("180")
    assert Decimal(tb["2110"]["total_credit"]) == Decimal("180")

    report = await get_valuation_variance(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(report[0]["valuation_variance"]) == Decimal("20")
    assert Decimal(report[0]["unit_cost_variance"]) == Decimal("5")

    # Void landed cost -> inventory returns to 160 / avg 40; ledger reverses Dr 2110 20 / Cr 1300 20
    void_resp = await void_adjustment(client, ctx.company_id, lc["id"])
    assert void_resp.status_code == 200, void_resp.text
    assert void_resp.json()["status"] == "voided"
    assert void_resp.json()["voided_at"] is not None

    summaries_after = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries_after[0]["total_cost_value"]) == Decimal("160")
    assert Decimal(summaries_after[0]["moving_avg_cost"]) == Decimal("40")

    tb_after = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb_after["1300"]["total_debit"]) - Decimal(
        tb_after["1300"]["total_credit"]
    ) == Decimal("160")
    assert Decimal(tb_after["2110"]["total_credit"]) - Decimal(
        tb_after["2110"]["total_debit"]
    ) == Decimal("160")


@pytest.mark.asyncio
async def test_ppv_unfavorable_and_favorable_clearing_and_void(client: AsyncClient) -> None:
    """PPV posts to 5110/2110 without mutating ProductCostSummary value or avg cost."""
    ctx = await setup_costing_company(
        client, "PPVCO", standard_cost="40", list_price="100", initial_po_qty="4"
    )

    # Unfavorable PPV (+15): Dr 5110 15 / Cr 2110 15
    unfav_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="ppv",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="15",
        reason="Supplier price hike variance",
    )
    assert unfav_resp.status_code == 201, unfav_resp.text

    # Favorable PPV (-5): Dr 2110 5 / Cr 5110 5
    fav_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="ppv",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="-5",
        reason="Volume rebate variance",
    )
    assert fav_resp.status_code == 201, fav_resp.text

    # Product cost summary stays untouched at 160 / 40
    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("160")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("40")

    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb["5110"]["total_debit"]) - Decimal(tb["5110"]["total_credit"]) == Decimal("10")
    assert Decimal(tb["2110"]["total_credit"]) - Decimal(tb["2110"]["total_debit"]) == Decimal(
        "170"
    )

    # Void favorable PPV -> net 5110 becomes +15, net 2110 becomes 175
    void_resp = await void_adjustment(client, ctx.company_id, fav_resp.json()["id"])
    assert void_resp.status_code == 200, void_resp.text
    tb_after = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb_after["5110"]["total_debit"]) - Decimal(
        tb_after["5110"]["total_credit"]
    ) == Decimal("15")
    assert Decimal(tb_after["2110"]["total_credit"]) - Decimal(
        tb_after["2110"]["total_debit"]
    ) == Decimal("175")


@pytest.mark.asyncio
async def test_revaluation_upward_downward_and_void(client: AsyncClient) -> None:
    """REVALUATION adjusts 1300 Inventory vs 5110 Purchase Price Variance cleanly."""
    ctx = await setup_costing_company(
        client, "REVCO", standard_cost="40", list_price="100", initial_po_qty="4"
    )

    up_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="revaluation",
        product_id=ctx.product_id,
        amount="40",
        reason="Upward market cost revaluation",
    )
    assert up_resp.status_code == 201, up_resp.text
    summaries = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries[0]["total_cost_value"]) == Decimal("200")
    assert Decimal(summaries[0]["moving_avg_cost"]) == Decimal("50")

    down_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="revaluation",
        product_id=ctx.product_id,
        amount="-20",
        reason="NRV write-down",
    )
    assert down_resp.status_code == 201, down_resp.text
    summaries_down = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries_down[0]["total_cost_value"]) == Decimal("180")
    assert Decimal(summaries_down[0]["moving_avg_cost"]) == Decimal("45")

    void_down = await void_adjustment(client, ctx.company_id, down_resp.json()["id"])
    assert void_down.status_code == 200, void_down.text
    summaries_final = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(summaries_final[0]["total_cost_value"]) == Decimal("200")
    assert Decimal(summaries_final[0]["moving_avg_cost"]) == Decimal("50")

    moves = await get_cost_moves(client, ctx.company_id, product_id=ctx.product_id)
    assert [m["move_type"] for m in moves] == [
        "receipt",
        "revaluation",
        "revaluation",
        "revaluation",
    ]


@pytest.mark.asyncio
async def test_platform_customfields_workflow_void_and_batch_export(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cost adjustments honor CustomFields validation, Workflow SoD void gate, and Batch export."""
    ctx = await setup_costing_company(
        client, "PLATCOST", standard_cost="40", list_price="100", initial_po_qty="4"
    )

    # 1. Custom field required on cost_adjustment
    cf_resp = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "cost_adjustment",
            "field_key": "carrier_ref",
            "label": "Carrier Reference",
            "field_type": "TEXT",
            "is_required": True,
        },
        headers=company_headers(ctx.company_id),
    )
    assert cf_resp.status_code == 201, cf_resp.text

    # Missing required custom field fails 422
    bad_adj = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="landed_cost",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="25",
        reason="Missing CF",
    )
    assert bad_adj.status_code == 422

    ok_adj_resp = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="landed_cost",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="25",
        reason="Air freight bill",
        custom_data={"carrier_ref": "AWB-8899"},
    )
    assert ok_adj_resp.status_code == 201, ok_adj_resp.text
    adj_id = uuid.UUID(ok_adj_resp.json()["id"])

    # 2. Workflow gate on cost_adjustment.void with SoD enabled
    requester_id = await create_user_with_role(
        db_session,
        email="costreq@example.com",
        password="pw",
        company_id=ctx.company_id,
    )
    approver_id = await create_user_with_role(
        db_session,
        email="costapp@example.com",
        password="pw",
        company_id=ctx.company_id,
    )

    with company_context(ctx.company_id), actor_context(TEST_ADMIN_ACTOR):
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type="cost_adjustment",
                transition="void",
                name="Cost Void Approval",
                allow_self_approval=False,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Controller Void Approval",
                        min_amount=Decimal("10"),
                        required_permission="costing.adjustment.void",
                    )
                ],
            ),
        )

    # Void without approval -> 409
    blocked_void = await void_adjustment(client, ctx.company_id, str(adj_id))
    assert blocked_void.status_code == 409

    # Submit by requester, verify approver must differ (SoD)
    req_actor = ActorContext(
        user_id=requester_id,
        permission_codes=frozenset({"platform.workflow.approve", "costing.adjustment.void"}),
    )
    app_actor = ActorContext(
        user_id=approver_id,
        permission_codes=frozenset({"platform.workflow.approve", "costing.adjustment.void"}),
    )
    with company_context(ctx.company_id), actor_context(req_actor):
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type="cost_adjustment",
                entity_id=adj_id,
                transition="void",
            ),
        )
    with company_context(ctx.company_id), actor_context(app_actor):
        approved = await wf_service.approve_workflow_instance(db_session, inst.id)
        assert approved.status == "APPROVED"

    void_ok = await void_adjustment(client, ctx.company_id, str(adj_id))
    assert void_ok.status_code == 200, void_ok.text
    assert void_ok.json()["status"] == "voided"

    # 3. Batch exports for cost_adjustment and product_cost_summary
    for entity_type in ("cost_adjustment", "product_cost_summary"):
        exp_resp = await client.post(
            "/api/v1/integrations/exports",
            json={"entity_type": entity_type, "format": "JSON"},
            headers=company_headers(ctx.company_id),
        )
        assert exp_resp.status_code == 200, exp_resp.text
        assert exp_resp.json()["job"]["total_rows"] >= 1
