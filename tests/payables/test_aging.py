"""tests/payables/test_aging.py — AP aging report + 2000/2110 tie-out properties (ADR-015)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.authorization import actor_context
from app.core.tenancy import company_context
from app.modules.payables import service as payables_service
from tests.conftest import TEST_ADMIN_ACTOR
from tests.payables._helpers import (
    allocate_payment,
    allocation,
    create_payment,
    get_ap_aging,
    get_trial_balance_by_code,
    issue_bill,
    setup_received_order,
)
from tests.purchase._helpers import confirm_order, create_draft_order, order_line, receive_order


@pytest.fixture(autouse=True)
def _bind_admin() -> Iterator[None]:
    with actor_context(TEST_ADMIN_ACTOR):
        yield


@pytest.mark.parametrize(
    "edge_days,before_bucket,after_bucket",
    [
        (0, "current", "days_1_30"),
        (30, "days_1_30", "days_31_60"),
        (60, "days_31_60", "days_61_90"),
        (90, "days_61_90", "days_90_plus"),
    ],
)
@pytest.mark.asyncio
async def test_ap_aging_bucket_edge_boundaries(
    client: AsyncClient, edge_days: int, before_bucket: str, after_bucket: str
) -> None:
    ctx = await setup_received_order(client, f"APEDG{edge_days}", standard_cost="100", qty="1")
    due_today = datetime.now(timezone.utc).date()  # noqa: UP017
    issued = await issue_bill(
        client, ctx.company_id, ctx.order["id"], due_date=due_today.isoformat()
    )
    assert issued.status_code == 201, issued.text

    at_edge = (due_today + timedelta(days=edge_days)).isoformat()
    row_edge = next(
        r
        for r in (await get_ap_aging(client, ctx.company_id, bucket_date=at_edge)).json()
        if r["supplier_id"] == str(ctx.supplier_id)
    )
    assert row_edge[before_bucket] == "100.000000"
    assert row_edge[after_bucket] == "0.000000"

    past_edge = (due_today + timedelta(days=edge_days + 1)).isoformat()
    row_past = next(
        r
        for r in (await get_ap_aging(client, ctx.company_id, bucket_date=past_edge)).json()
        if r["supplier_id"] == str(ctx.supplier_id)
    )
    assert row_past[after_bucket] == "100.000000"
    assert row_past[before_bucket] == "0.000000"


@pytest.mark.asyncio
async def test_payment_only_supplier_appears_with_negative_net_and_control_tieout(
    client: AsyncClient,
) -> None:
    ctx = await setup_received_order(client, "APAGE2", standard_cost="1000", qty="1")

    # Stage 1 (PO received, unbilled): 2110 net credit = 1000
    tb_stage1 = await get_trial_balance_by_code(client, ctx.company_id)
    grir_bal_1 = Decimal(tb_stage1["2110"]["total_credit"]) - Decimal(
        tb_stage1["2110"]["total_debit"]
    )
    assert grir_bal_1 == Decimal("1000.000000")

    # Pay 300 BEFORE billing -> appears as unapplied_payments, net_total = -300
    p1 = (await create_payment(client, ctx.company_id, ctx.supplier_id, "300", "APAGE2-P1")).json()
    aging_pre = (await get_ap_aging(client, ctx.company_id)).json()
    row_pre = next(r for r in aging_pre if r["supplier_id"] == str(ctx.supplier_id))
    assert row_pre["unapplied_payments"] == "300.000000"
    assert Decimal(row_pre["net_total"]) == Decimal("-300.000000")

    # Receive second PO (500) that remains UNBILLED; bill first PO (1000) and allocate 300
    po2 = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(ctx.product_id, "1", unit_cost="500")]
    )
    assert (await confirm_order(client, ctx.company_id, po2["id"])).status_code == 200
    assert (await receive_order(client, ctx.company_id, po2["id"])).status_code == 200

    b1 = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()
    assert (
        await allocate_payment(
            client, ctx.company_id, p1["id"], "A1", [allocation(b1["id"], "300")]
        )
    ).status_code == 200
    assert (
        await create_payment(client, ctx.company_id, ctx.supplier_id, "75", "APAGE2-P2")
    ).status_code == 201

    row = next(
        r
        for r in (await get_ap_aging(client, ctx.company_id)).json()
        if r["supplier_id"] == str(ctx.supplier_id)
    )
    tb = await get_trial_balance_by_code(client, ctx.company_id)
    ap_credit_net = Decimal(tb["2000"]["total_credit"]) - Decimal(tb["2000"]["total_debit"])
    grir_credit_net = Decimal(tb["2110"]["total_credit"]) - Decimal(tb["2110"]["total_debit"])

    assert Decimal(row["net_total"]) == ap_credit_net == Decimal("625.000000")
    assert grir_credit_net == Decimal("500.000000")


@pytest.mark.asyncio
async def test_get_ap_aging_reads_in_one_union_statement(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_received_order(client, "APAGE3", standard_cost="100", qty="1")
    assert (await issue_bill(client, ctx.company_id, ctx.order["id"])).status_code == 201

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    statement_count = 0

    def _count(*args: object, **kwargs: object) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _count)
    try:
        async with session_factory() as session:
            with company_context(ctx.company_id):
                await session.connection()
                statement_count = 0
                await payables_service.get_ap_aging(session)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _count)

    assert statement_count == 2
