"""tests/payables/test_payments.py — Vendor payments, idempotency & concurrency (ADR-015)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.authorization import actor_context
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.payables import service as payables_service
from app.modules.payables.schemas import VendorPaymentAllocationIn
from tests.conftest import TEST_ADMIN_ACTOR, company_headers
from tests.payables._helpers import (
    allocate_payment,
    allocation,
    create_payment,
    get_ap_aging,
    get_trial_balance_by_code,
    issue_bill,
    setup_received_order,
    void_payment,
)
from tests.purchase._helpers import confirm_order, create_draft_order, order_line, receive_order


@pytest.mark.asyncio
async def test_payment_posts_ap_and_cash_and_external_ref_retry(client: AsyncClient) -> None:
    ctx = await setup_received_order(client, "APAY1", standard_cost="50", qty="2")
    resp = await create_payment(client, ctx.company_id, ctx.supplier_id, "100", "APAY1-REF")
    assert resp.status_code == 201, resp.text
    payment = resp.json()
    assert payment["payment_no"].startswith("APAY-")
    assert payment["status"] == "issued"
    assert payment["allocated_amount"] == "0.000000"

    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert tb["2000"]["total_debit"] == "100.000000"
    assert tb["1000"]["total_credit"] == "100.000000"

    retry = await create_payment(client, ctx.company_id, ctx.supplier_id, "100", "APAY1-REF")
    assert retry.status_code == 409
    assert payment["id"] in retry.text
    assert payment["payment_no"] in retry.text


@pytest.mark.asyncio
async def test_void_payment_contra_and_reject_with_allocations(client: AsyncClient) -> None:
    ctx = await setup_received_order(client, "APAY2", standard_cost="40", qty="2")
    bill = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()

    p1 = (await create_payment(client, ctx.company_id, ctx.supplier_id, "80", "APAY2-REF1")).json()
    assert (await void_payment(client, ctx.company_id, p1["id"])).status_code == 200
    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert tb["1000"]["total_debit"] == tb["1000"]["total_credit"] == "80.000000"
    assert (await void_payment(client, ctx.company_id, p1["id"])).status_code == 409

    aging = await get_ap_aging(client, ctx.company_id)
    row = next(r for r in aging.json() if r["supplier_id"] == str(ctx.supplier_id))
    assert row["unapplied_payments"] == "0.000000"

    p2 = (
        await create_payment(
            client,
            ctx.company_id,
            ctx.supplier_id,
            "80",
            "APAY2-REF2",
            allocations=[allocation(bill["id"], "80")],
        )
    ).json()
    assert (await void_payment(client, ctx.company_id, p2["id"])).status_code == 409


@pytest.mark.asyncio
async def test_allocation_idempotency_validation_and_multi_bill(client: AsyncClient) -> None:
    ctx = await setup_received_order(client, "APAY3", standard_cost="50", qty="2")
    bill1 = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()

    po2 = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(ctx.product_id, "1")]
    )
    assert (await confirm_order(client, ctx.company_id, po2["id"])).status_code == 200
    assert (await receive_order(client, ctx.company_id, po2["id"])).status_code == 200
    bill2 = (await issue_bill(client, ctx.company_id, po2["id"])).json()

    pay = (await create_payment(client, ctx.company_id, ctx.supplier_id, "130", "APAY3-P")).json()
    alloc_list = [allocation(bill1["id"], "80"), allocation(bill2["id"], "50")]
    r1 = await allocate_payment(client, ctx.company_id, pay["id"], "CMD-1", alloc_list)
    assert r1.status_code == 200
    assert r1.json()["allocated_amount"] == "130.000000"

    # Reversed-order exact retry -> idempotent 200
    r2 = await allocate_payment(
        client, ctx.company_id, pay["id"], "CMD-1", list(reversed(alloc_list))
    )
    assert r2.status_code == 200
    assert r2.json()["allocated_amount"] == "130.000000"

    # Reused request_ref with different payload -> 409
    r_mismatch = await allocate_payment(
        client, ctx.company_id, pay["id"], "CMD-1", [allocation(bill1["id"], "100")]
    )
    assert r_mismatch.status_code == 409

    # Over-allocation & duplicate targets
    r_dup = await allocate_payment(
        client,
        ctx.company_id,
        pay["id"],
        "CMD-DUP",
        [allocation(bill1["id"], "10"), allocation(bill1["id"], "10")],
    )
    assert r_dup.status_code == 422

    # Check bill statuses: bill1 partial (80/100), bill2 paid (50/50)
    b1_get = (
        await client.get(
            f"/api/v1/payables/bills/{bill1['id']}", headers=company_headers(ctx.company_id)
        )
    ).json()
    b2_get = (
        await client.get(
            f"/api/v1/payables/bills/{bill2['id']}", headers=company_headers(ctx.company_id)
        )
    ).json()
    assert b1_get["status"] == "partial"
    assert b2_get["status"] == "paid"


@pytest.mark.asyncio
async def test_concurrent_allocations_serialize_without_over_settling(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_received_order(client, "APCON", standard_cost="100", qty="1")
    bill = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()
    p1 = (await create_payment(client, ctx.company_id, ctx.supplier_id, "70", "APCON-1")).json()
    p2 = (await create_payment(client, ctx.company_id, ctx.supplier_id, "70", "APCON-2")).json()
    bill_id = uuid.UUID(bill["id"])

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    outcomes: dict[str, str] = {}

    async def task_alloc(name: str, pay_id: str, ref: str, hold: bool) -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(ctx.company_id):
                try:
                    if not hold:
                        await first_locked.wait()
                    await payables_service.allocate_payment(
                        session,
                        uuid.UUID(pay_id),
                        ref,
                        [VendorPaymentAllocationIn(bill_id=bill_id, amount=Decimal("70"))],
                    )
                    if hold:
                        first_locked.set()
                        await release_first.wait()
                    await session.commit()
                    outcomes[name] = "ok"
                except ConflictError:
                    if hold:
                        first_locked.set()
                    await session.rollback()
                    outcomes[name] = "conflict"

    t1 = asyncio.create_task(task_alloc("t1", p1["id"], "C1", hold=True))
    await first_locked.wait()
    t2 = asyncio.create_task(task_alloc("t2", p2["id"], "C2", hold=False))
    await asyncio.sleep(0.3)
    assert not t2.done()
    release_first.set()
    await asyncio.gather(t1, t2)

    assert set(outcomes.values()) == {"ok", "conflict"}
    final_bill = (
        await client.get(
            f"/api/v1/payables/bills/{bill['id']}", headers=company_headers(ctx.company_id)
        )
    ).json()
    assert Decimal(final_bill["settled_amount"]) == Decimal("70")


@pytest.mark.asyncio
async def test_payment_bankers_rounding_custom_fields_and_workflow_void(
    client: AsyncClient,
) -> None:
    ctx = await setup_received_order(client, "APWF", standard_cost="60", qty="2")
    headers = company_headers(ctx.company_id)

    cf = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "vendor_payment",
            "field_key": "bank_fee",
            "label": "Bank Fee",
            "field_type": "DECIMAL",
        },
        headers=headers,
    )
    assert cf.status_code == 201

    p_resp = await create_payment(
        client,
        ctx.company_id,
        ctx.supplier_id,
        "100.1234565",
        "APWF-REF",
        custom_data={"bank_fee": "15.0000015"},
    )
    assert p_resp.status_code == 201, p_resp.text
    p = p_resp.json()
    assert p["amount"] == "100.123456"
    assert p["custom_data"]["bank_fee"] == "15.000002"

    # Self-approval allowed workflow on vendor_payment.void
    wf = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "vendor_payment",
            "transition": "void",
            "name": "Payment Void Gate",
            "allow_self_approval": True,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Treasury",
                    "min_amount": "50.000000",
                    "required_permission": "platform.workflow.approve",
                }
            ],
        },
        headers=headers,
    )
    assert wf.status_code == 201
    assert (await void_payment(client, ctx.company_id, p["id"])).status_code == 409

    inst = (
        await client.post(
            "/api/v1/workflows/instances",
            json={"entity_type": "vendor_payment", "entity_id": p["id"], "transition": "void"},
            headers=headers,
        )
    ).json()
    assert (
        await client.post(f"/api/v1/workflows/instances/{inst['id']}/approve", headers=headers)
    ).status_code == 200
    assert (await void_payment(client, ctx.company_id, p["id"])).status_code == 200
