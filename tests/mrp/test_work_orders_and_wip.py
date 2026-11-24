"""Work order lifecycle, WIP accounting, idempotency, short-close & platform governance tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import replay_outbox
from app.core import hooks
from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import ConflictError
from app.core.hooks import HookContext
from app.core.tenancy import company_context
from app.modules.masterdata.models import OutboxEvent
from app.modules.mrp.service import MRP_WORK_ORDER_VALIDATE_CONFIRM
from app.platform.workflow import service as wf_service
from app.platform.workflow.schemas import (
    WorkflowDefinitionCreate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)
from tests.conftest import TEST_ADMIN_ACTOR, company_headers
from tests.costing._helpers import get_cost_moves, get_summaries, get_trial_balance_by_code
from tests.inventory._helpers import on_hand
from tests.mrp._helpers import (
    bom_line,
    cancel_work_order,
    confirm_work_order,
    create_bom,
    create_product,
    create_work_order,
    issue_materials,
    receive_production,
    return_materials,
    seed_stock_via_po,
    setup_mrp_company,
)
from tests.platform.permissions._helpers import create_user_with_role


@pytest.mark.asyncio
async def test_work_order_wip_repeating_decimal_partial_receipts(client: AsyncClient) -> None:
    """Partial/final production receipts drain repeating-decimal 1310 WIP to zero residue."""
    ctx = await setup_mrp_company(client, "WIPCO1")
    raw_id = await create_product(client, ctx.company_id, "WIP-RAW", standard_cost="10")
    fg_id = await create_product(client, ctx.company_id, "WIP-FG", standard_cost="30")

    # PO1 1@50 (=50) + PO2 2@25 (=50) -> 3 units total cost = 100.000000
    await seed_stock_via_po(client, ctx.company_id, ctx.supplier_id, [(raw_id, "1", "50")])
    await seed_stock_via_po(client, ctx.company_id, ctx.supplier_id, [(raw_id, "2", "25")])

    # BOM: 1 FG requires 1 RAW
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="BOM-WIP",
            product_id=fg_id,
            lines=[bom_line(raw_id, "1")],
        )
    ).status_code == 201

    # WO 1: planned_qty=3, auto-issue all 3 RAW (100.000000 WIP) -> partial 1 + partial 1 + final 1
    wo1_resp = await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="3")
    assert wo1_resp.status_code == 201, wo1_resp.text
    wo1_id = wo1_resp.json()["id"]
    assert wo1_resp.json()["wo_no"].startswith("WO-")

    assert (await confirm_work_order(client, ctx.company_id, wo1_id)).status_code == 200
    iss_resp = await issue_materials(client, ctx.company_id, wo1_id, request_ref="iss-all-1")
    assert iss_resp.status_code == 200, iss_resp.text
    assert Decimal(iss_resp.json()["wip_balance"]) == Decimal("100")
    assert iss_resp.json()["status"] == "in_progress"

    # Idempotent replay of exact same request_ref returns unchanged state, wrong payload fails 409
    replay_iss = await issue_materials(client, ctx.company_id, wo1_id, request_ref="iss-all-1")
    assert replay_iss.status_code == 200
    assert Decimal(replay_iss.json()["wip_balance"]) == Decimal("100")

    conflict_iss = await issue_materials(
        client, ctx.company_id, wo1_id, request_ref="iss-all-1", lines=[(raw_id, "1")]
    )
    assert conflict_iss.status_code == 409

    # Receipt 1: 1 unit -> absorbs round_half_even(100 * 1 / 3) = 33.333333
    r1 = await receive_production(client, ctx.company_id, wo1_id, request_ref="rec-1", qty="1")
    assert r1.status_code == 200, r1.text
    assert Decimal(r1.json()["wip_balance"]) == Decimal("66.666667")

    # Receipt 2: 1 unit -> absorbs round_half_even(66.666667 * 1 / 2) = 33.333334
    r2 = await receive_production(client, ctx.company_id, wo1_id, request_ref="rec-2", qty="1")
    assert r2.status_code == 200
    assert Decimal(r2.json()["wip_balance"]) == Decimal("33.333333")

    # Receipt 3 (final): 1 unit -> absorbs exact remaining 33.333333 -> WIP == 0, completed
    r3 = await receive_production(client, ctx.company_id, wo1_id, request_ref="rec-3", qty="1")
    assert r3.status_code == 200
    assert r3.json()["status"] == "completed"
    assert Decimal(r3.json()["wip_balance"]) == Decimal("0")

    # Verify FG cost summary holds exact 100.000000 total_cost_value on 3 units (no penny lost)
    fg_sum = (await get_summaries(client, ctx.company_id, product_id=fg_id))[0]
    assert Decimal(fg_sum["tracked_qty"]) == Decimal("3")
    assert Decimal(fg_sum["total_cost_value"]) == Decimal("100")
    assert Decimal(fg_sum["moving_avg_cost"]) == Decimal("33.333333")

    # Verify 1310 WIP ledger balance == 0
    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb["1310"]["total_debit"]) == Decimal("100")
    assert Decimal(tb["1310"]["total_credit"]) == Decimal("100")


@pytest.mark.asyncio
async def test_work_order_return_and_short_close(client: AsyncClient) -> None:
    """Material return refunds WIP and mark_completed=True absorbs full remaining WIP."""
    ctx = await setup_mrp_company(client, "WIPCO2")
    raw_id = await create_product(client, ctx.company_id, "SC-RAW", standard_cost="25")
    fg_id = await create_product(client, ctx.company_id, "SC-FG", standard_cost="50")

    # Seed existing 3 FG @ 100/3 + 4 RAW @ 25
    await seed_stock_via_po(
        client,
        ctx.company_id,
        ctx.supplier_id,
        [(fg_id, "2", "50"), (fg_id, "1", "0"), (raw_id, "4", "25")],
    )
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="BOM-SC",
            product_id=fg_id,
            lines=[bom_line(raw_id, "1")],
        )
    ).status_code == 201

    wo2_id = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="4")
    ).json()["id"]
    assert (await confirm_work_order(client, ctx.company_id, wo2_id)).status_code == 200
    assert (
        await issue_materials(
            client, ctx.company_id, wo2_id, request_ref="w2-iss", lines=[(raw_id, "4")]
        )
    ).status_code == 200

    # Return 1 RAW before receiving -> WIP decreases 100 -> 75, RAW stock returns to 1
    ret_resp = await return_materials(
        client, ctx.company_id, wo2_id, request_ref="w2-ret", lines=[(raw_id, "1")]
    )
    assert ret_resp.status_code == 200, ret_resp.text
    assert Decimal(ret_resp.json()["wip_balance"]) == Decimal("75")
    assert await on_hand(client, ctx.company_id, raw_id) == Decimal("1")

    # Yield loss: produce 2 units out of 4 planned and pass mark_completed=True (short close)
    short_rec = await receive_production(
        client, ctx.company_id, wo2_id, request_ref="w2-rec", qty="2", mark_completed=True
    )
    assert short_rec.status_code == 200, short_rec.text
    assert short_rec.json()["status"] == "completed"
    assert Decimal(short_rec.json()["completed_qty"]) == Decimal("2")
    assert Decimal(short_rec.json()["wip_balance"]) == Decimal("0")

    # FG MWA: (100 + 75) / (3 + 2) = 175 / 5 = 35.000000
    fg_sum_after = (await get_summaries(client, ctx.company_id, product_id=fg_id))[0]
    assert Decimal(fg_sum_after["tracked_qty"]) == Decimal("5")
    assert Decimal(fg_sum_after["total_cost_value"]) == Decimal("175")
    assert Decimal(fg_sum_after["moving_avg_cost"]) == Decimal("35")


@pytest.mark.asyncio
async def test_mrp_outbox_replay_idempotency(client: AsyncClient, db_session: AsyncSession) -> None:
    """Replaying mrp.* outbox events is an exact no-op across inventory, costing, and ledger."""
    ctx = await setup_mrp_company(client, "MRPRPL")
    raw_id = await create_product(client, ctx.company_id, "RPL-RAW", standard_cost="15")
    fg_id = await create_product(client, ctx.company_id, "RPL-FG", standard_cost="30")
    await seed_stock_via_po(client, ctx.company_id, ctx.supplier_id, [(raw_id, "4", "15")])
    assert (
        await create_bom(
            client, ctx.company_id, code="B-RPL", product_id=fg_id, lines=[bom_line(raw_id, "2")]
        )
    ).status_code == 201

    wo_id = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="2")
    ).json()["id"]
    assert (await confirm_work_order(client, ctx.company_id, wo_id)).status_code == 200
    assert (
        await issue_materials(client, ctx.company_id, wo_id, request_ref="i1")
    ).status_code == 200
    assert (
        await receive_production(client, ctx.company_id, wo_id, request_ref="r1", qty="2")
    ).status_code == 200

    fg_moves_before = await get_cost_moves(client, ctx.company_id, product_id=fg_id)
    tb_before = await get_trial_balance_by_code(client, ctx.company_id)

    await db_session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type.in_(
                ["mrp.material_issued", "mrp.material_returned", "mrp.production_received"]
            )
        )
        .values(dispatched_at=None)
    )
    await db_session.commit()

    replay_summary = await replay_outbox.replay_outbox()
    assert replay_summary.failed == 0

    assert await get_cost_moves(client, ctx.company_id, product_id=fg_id) == fg_moves_before
    assert await get_trial_balance_by_code(client, ctx.company_id) == tb_before


@pytest.mark.asyncio
async def test_mrp_platform_customfields_hooks_workflow_and_batch_export(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Verify CustomFields on bom/work_order, plugin hook veto, SoD workflow & batch export."""
    ctx = await setup_mrp_company(client, "MRPPLAT")
    raw_id = await create_product(client, ctx.company_id, "PL-RAW", standard_cost="10")
    fg_id = await create_product(client, ctx.company_id, "PL-FG", standard_cost="20")

    # 1. Custom fields on bom & work_order
    for entity_type, field_key in (("bom", "eco_ref"), ("work_order", "shop_line")):
        r = await client.post(
            "/api/v1/custom-fields",
            json={
                "entity_type": entity_type,
                "field_key": field_key,
                "label": field_key.upper(),
                "field_type": "TEXT",
                "is_required": True,
            },
            headers=company_headers(ctx.company_id),
        )
        assert r.status_code == 201, r.text

    # Missing CF fails 422
    bad_bom = await create_bom(
        client, ctx.company_id, code="B-BAD", product_id=fg_id, lines=[bom_line(raw_id, "2")]
    )
    assert bad_bom.status_code == 422

    ok_bom = await create_bom(
        client,
        ctx.company_id,
        code="B-OK",
        product_id=fg_id,
        lines=[bom_line(raw_id, "2")],
        custom_data={"eco_ref": "ECO-101"},
    )
    assert ok_bom.status_code == 201, ok_bom.text

    wo_resp = await create_work_order(
        client,
        ctx.company_id,
        product_id=fg_id,
        planned_qty="2",
        custom_data={"shop_line": "SMT-01"},
    )
    assert wo_resp.status_code == 201, wo_resp.text
    wo_id = uuid.UUID(wo_resp.json()["id"])

    # 2. Plugin hook veto on mrp.work_order.validate_confirm
    async def _veto_hook(_session: AsyncSession, context: HookContext) -> None:
        if context.order_id == wo_id:
            raise ConflictError("Production line shutdown veto")

    hooks.register(MRP_WORK_ORDER_VALIDATE_CONFIRM, _veto_hook)
    try:
        vetoed = await confirm_work_order(client, ctx.company_id, wo_id)
        assert vetoed.status_code == 409
        assert "shutdown veto" in vetoed.text
    finally:
        hooks.unregister(MRP_WORK_ORDER_VALIDATE_CONFIRM, _veto_hook)

    # 3. Workflow SoD gate on work_order.confirm
    req_id = await create_user_with_role(
        db_session, email="woreq@example.com", password="pw", company_id=ctx.company_id
    )
    app_id = await create_user_with_role(
        db_session, email="woapp@example.com", password="pw", company_id=ctx.company_id
    )
    with company_context(ctx.company_id), actor_context(TEST_ADMIN_ACTOR):
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type="work_order",
                transition="confirm",
                name="WO Confirm SoD",
                allow_self_approval=False,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Production Mgr",
                        min_amount=Decimal("10"),
                        required_permission="mrp.work_order.confirm",
                    )
                ],
            ),
        )

    assert (await confirm_work_order(client, ctx.company_id, wo_id)).status_code == 409

    req_actor = ActorContext(
        user_id=req_id,
        permission_codes=frozenset({"platform.workflow.approve", "mrp.work_order.confirm"}),
    )
    app_actor = ActorContext(
        user_id=app_id,
        permission_codes=frozenset({"platform.workflow.approve", "mrp.work_order.confirm"}),
    )
    with company_context(ctx.company_id), actor_context(req_actor):
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(entity_type="work_order", entity_id=wo_id, transition="confirm"),
        )
    with company_context(ctx.company_id), actor_context(app_actor):
        approved = await wf_service.approve_workflow_instance(db_session, inst.id)
        assert approved.status == "APPROVED"

    assert (await confirm_work_order(client, ctx.company_id, wo_id)).status_code == 200
    assert (await cancel_work_order(client, ctx.company_id, wo_id)).status_code == 200

    # 4. Batch export adapters for bom & work_order
    for entity_type in ("bom", "work_order"):
        exp_resp: Any = await client.post(
            "/api/v1/integrations/exports",
            json={"entity_type": entity_type, "format": "JSON"},
            headers=company_headers(ctx.company_id),
        )
        assert exp_resp.status_code == 200, exp_resp.text
        assert exp_resp.json()["job"]["total_rows"] >= 1


@pytest.mark.asyncio
async def test_return_materials_after_partial_receipt_guards_consumed_and_drains_1ulp_wip(
    client: AsyncClient,
) -> None:
    """Partially completed WO blocks over-returning consumed qty and settles 1-ULP WIP drift."""
    ctx = await setup_mrp_company(client, "MRPRET")
    raw_id = await create_product(client, ctx.company_id, "RET-RAW", standard_cost="0")
    fg_id = await create_product(client, ctx.company_id, "RET-FG", standard_cost="10")

    # Seed 3 RAW with repeating total cost = 10.000001 (= 1@5 + 2@2.500000... or adjust)
    # Use 1 @ 5.000001 + 2 @ 2.500000 => total cost 10.000001 on 3 units
    await seed_stock_via_po(
        client,
        ctx.company_id,
        ctx.supplier_id,
        [(raw_id, "1", "5.000001"), (raw_id, "2", "2.5")],
    )
    assert (
        await create_bom(
            client,
            ctx.company_id,
            code="B-RET",
            product_id=fg_id,
            lines=[bom_line(raw_id, "1")],
        )
    ).status_code == 201

    wo_id = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="3")
    ).json()["id"]
    assert (await confirm_work_order(client, ctx.company_id, wo_id)).status_code == 200
    assert (
        await issue_materials(client, ctx.company_id, wo_id, request_ref="ret-iss")
    ).status_code == 200

    # Two partial receipts of 1 each: round(10.000001/3)=3.333334; remaining 6.666667/2=3.333334
    # Leaves exact wip_balance = 3.333333 while raw_net = 3.33333333... (would round to 3.333334)
    assert (
        await receive_production(client, ctx.company_id, wo_id, request_ref="ret-r1", qty="1")
    ).status_code == 200
    r2 = await receive_production(client, ctx.company_id, wo_id, request_ref="ret-r2", qty="1")
    assert r2.status_code == 200
    assert Decimal(r2.json()["wip_balance"]) == Decimal("3.333333")

    # Returning 2 (> max_returnable=1) must fail 409 because 2 units are already consumed in FG
    over_ret = await return_materials(
        client, ctx.company_id, wo_id, request_ref="ret-over", lines=[(raw_id, "2")]
    )
    assert over_ret.status_code == 409

    # Returning exact 1 unconsumed unit absorbs 1-ULP rounding delta and leaves wip_balance == 0
    ok_ret = await return_materials(
        client, ctx.company_id, wo_id, request_ref="ret-ok", lines=[(raw_id, "1")]
    )
    assert ok_ret.status_code == 200, ok_ret.text
    assert Decimal(ok_ret.json()["wip_balance"]) == Decimal("0")
    assert await on_hand(client, ctx.company_id, raw_id) == Decimal("1")
    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert Decimal(tb["1310"]["total_debit"]) == Decimal(tb["1310"]["total_credit"])
