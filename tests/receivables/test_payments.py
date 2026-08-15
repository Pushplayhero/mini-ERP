"""tests/receivables/test_payments.py — payments + allocation/沖帳 (ADR-008

Decision 2/3, R2, R7, R14).

Payment retry idempotency (`external_ref`), void, inline + late allocation,
allocation-command idempotency (exact retry replays, reused ref with a
different body 409s), over-allocation both directions, duplicate targets,
cross-customer/voided-target/voided-payment rejection, and the
concurrent-allocation capacity race.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.receivables import service as receivables_service
from app.modules.receivables.schemas import PaymentAllocationIn
from tests.receivables._helpers import (
    allocate_payment,
    allocation,
    create_payment,
    get_trial_balance_by_code,
    issue_invoice,
    setup_shipped_order,
    void_payment,
)


@pytest.mark.asyncio
async def test_payment_posts_cash_and_ar(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY1", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id

    resp = await create_payment(client, company_id, customer_id, "100", "PAY1-REF")
    assert resp.status_code == 201, resp.text
    payment = resp.json()
    assert payment["status"] == "received"
    assert payment["allocated_amount"] == "0.000000"

    tb = await get_trial_balance_by_code(client, company_id)
    assert tb["1000"]["total_debit"] == "100.000000"
    assert tb["1100"]["total_credit"] == "100.000000"


@pytest.mark.asyncio
async def test_payment_retry_same_external_ref_is_409_and_posts_once(
    client: AsyncClient,
) -> None:
    ctx = await setup_shipped_order(client, "PAY2", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id

    first = await create_payment(client, company_id, customer_id, "50", "PAY2-REF")
    assert first.status_code == 201, first.text
    existing_payment = first.json()
    retry = await create_payment(client, company_id, customer_id, "50", "PAY2-REF")
    assert retry.status_code == 409, retry.text
    # ADR-008 R2: "the response body of the 409 identifies the existing
    # payment" — not just a generic conflict message (Codex diff review
    # 2026-08-15, finding 3).
    assert existing_payment["id"] in retry.text
    assert existing_payment["payment_no"] in retry.text

    tb = await get_trial_balance_by_code(client, company_id)
    assert tb["1000"]["total_debit"] == "50.000000"


@pytest.mark.asyncio
async def test_void_payment_posts_contra_and_drops_unapplied_credit(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY3", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id

    payment = (await create_payment(client, company_id, customer_id, "50", "PAY3-REF")).json()
    voided = await void_payment(client, company_id, payment["id"])
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "voided"

    tb = await get_trial_balance_by_code(client, company_id)
    assert tb["1000"]["total_debit"] == tb["1000"]["total_credit"] == "50.000000"

    aging = await client.get(
        "/api/v1/receivables/reports/ar-aging", headers={"X-Company-Id": str(company_id)}
    )
    rows = aging.json()
    matching = [r for r in rows if r["customer_id"] == str(customer_id)]
    assert matching == [] or matching[0]["unapplied_credits"] == "0.000000"


@pytest.mark.asyncio
async def test_void_payment_with_allocations_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY4", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (
        await create_payment(
            client,
            company_id,
            customer_id,
            "50",
            "PAY4-REF",
            allocations=[allocation(invoice["id"], "50")],
        )
    ).json()

    voided = await void_payment(client, company_id, payment["id"])
    assert voided.status_code == 409, voided.text


@pytest.mark.asyncio
async def test_inline_allocation_settles_invoice(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY5", list_price="80", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment_resp = await create_payment(
        client,
        company_id,
        customer_id,
        "80",
        "PAY5-REF",
        allocations=[allocation(invoice["id"], "80")],
    )
    assert payment_resp.status_code == 201, payment_resp.text
    payment = payment_resp.json()
    assert payment["allocated_amount"] == "80.000000"

    invoice_resp = await client.get(
        f"/api/v1/receivables/invoices/{invoice['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert invoice_resp.json()["status"] == "paid"
    assert invoice_resp.json()["settled_amount"] == "80.000000"


@pytest.mark.asyncio
async def test_late_allocation_settles_invoice(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY6", list_price="60", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "60", "PAY6-REF")).json()

    alloc_resp = await allocate_payment(
        client, company_id, payment["id"], "PAY6-ALLOC-1", [allocation(invoice["id"], "60")]
    )
    assert alloc_resp.status_code == 200, alloc_resp.text
    assert alloc_resp.json()["allocated_amount"] == "60.000000"

    invoice_resp = await client.get(
        f"/api/v1/receivables/invoices/{invoice['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert invoice_resp.json()["status"] == "paid"


@pytest.mark.asyncio
async def test_partial_allocation_leaves_invoice_partial(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY7", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "40", "PAY7-REF")).json()
    alloc_resp = await allocate_payment(
        client, company_id, payment["id"], "PAY7-ALLOC-1", [allocation(invoice["id"], "40")]
    )
    assert alloc_resp.status_code == 200, alloc_resp.text

    invoice_resp = await client.get(
        f"/api/v1/receivables/invoices/{invoice['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert invoice_resp.json()["status"] == "partial"
    assert invoice_resp.json()["settled_amount"] == "40.000000"


# ---------------------------------------------------------------------------
# Allocation-command idempotency (ADR-008 R14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_retry_allocation_command_replays_idempotently(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY8", list_price="70", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "70", "PAY8-REF")).json()

    first = await allocate_payment(
        client, company_id, payment["id"], "PAY8-ALLOC-1", [allocation(invoice["id"], "70")]
    )
    assert first.status_code == 200, first.text
    retry = await allocate_payment(
        client, company_id, payment["id"], "PAY8-ALLOC-1", [allocation(invoice["id"], "70")]
    )
    assert retry.status_code == 200, retry.text
    # Idempotent replay — allocated_amount must NOT double.
    assert retry.json()["allocated_amount"] == "70.000000"


@pytest.mark.asyncio
async def test_reused_ref_different_body_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY9", list_price="100", qty="2")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "200", "PAY9-REF")).json()

    first = await allocate_payment(
        client, company_id, payment["id"], "PAY9-ALLOC-1", [allocation(invoice["id"], "50")]
    )
    assert first.status_code == 200, first.text
    different_body = await allocate_payment(
        client, company_id, payment["id"], "PAY9-ALLOC-1", [allocation(invoice["id"], "100")]
    )
    assert different_body.status_code == 409, different_body.text


@pytest.mark.asyncio
async def test_duplicate_invoice_target_in_one_request_is_422(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY10", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "100", "PAY10-REF")).json()

    resp = await allocate_payment(
        client,
        company_id,
        payment["id"],
        "PAY10-ALLOC-1",
        [allocation(invoice["id"], "50"), allocation(invoice["id"], "50")],
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_over_allocation_exceeding_payment_amount_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY11", list_price="200", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "50", "PAY11-REF")).json()

    resp = await allocate_payment(
        client, company_id, payment["id"], "PAY11-ALLOC-1", [allocation(invoice["id"], "100")]
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_over_allocation_exceeding_invoice_total_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY12", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "200", "PAY12-REF")).json()

    resp = await allocate_payment(
        client, company_id, payment["id"], "PAY12-ALLOC-1", [allocation(invoice["id"], "100")]
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_allocation_to_voided_invoice_is_422(client: AsyncClient) -> None:
    from tests.receivables._helpers import void_invoice

    ctx = await setup_shipped_order(client, "PAY13", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    voided = await void_invoice(client, company_id, invoice["id"])
    assert voided.status_code == 200, voided.text

    payment = (await create_payment(client, company_id, customer_id, "50", "PAY13-REF")).json()
    resp = await allocate_payment(
        client, company_id, payment["id"], "PAY13-ALLOC-1", [allocation(invoice["id"], "50")]
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_allocation_to_voided_payment_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "PAY14", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "50", "PAY14-REF")).json()
    voided = await void_payment(client, company_id, payment["id"])
    assert voided.status_code == 200, voided.text

    resp = await allocate_payment(
        client, company_id, payment["id"], "PAY14-ALLOC-1", [allocation(invoice["id"], "50")]
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_allocation_across_customers_is_422(client: AsyncClient) -> None:
    from tests.sales._helpers import (
        create_customer,
    )

    ctx = await setup_shipped_order(client, "PAY15", list_price="50", qty="1")
    company_id = ctx.company_id
    order = ctx.order
    invoice = (await issue_invoice(client, company_id, order["id"])).json()

    other_customer_id = await create_customer(client, company_id, "PAY15OTHER")
    other_payment = (
        await create_payment(client, company_id, other_customer_id, "50", "PAY15-REF")
    ).json()

    resp = await allocate_payment(
        client,
        company_id,
        other_payment["id"],
        "PAY15-ALLOC-1",
        [allocation(invoice["id"], "50")],
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Concurrency: two allocation commands racing the same invoice's capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_allocations_same_invoice_capacity_respected(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_shipped_order(client, "PAY16", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment_a = (await create_payment(client, company_id, customer_id, "60", "PAY16-A")).json()
    payment_b = (await create_payment(client, company_id, customer_id, "60", "PAY16-B")).json()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    a_locked = asyncio.Event()
    release_a = asyncio.Event()
    results: dict[str, object] = {}

    async def alloc_task_a() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await receivables_service.allocate_payment(
                        session,
                        uuid.UUID(payment_a["id"]),
                        "PAY16-ALLOC-A",
                        [
                            PaymentAllocationIn(
                                invoice_id=uuid.UUID(invoice["id"]), amount=Decimal("60")
                            )
                        ],
                    )
                    a_locked.set()
                    await release_a.wait()
                    await session.commit()
                    results["a"] = "ok"
                except ConflictError as exc:
                    a_locked.set()
                    await session.rollback()
                    results["a"] = ("conflict", str(exc))

    async def alloc_task_b() -> None:
        await a_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await receivables_service.allocate_payment(
                        session,
                        uuid.UUID(payment_b["id"]),
                        "PAY16-ALLOC-B",
                        [
                            PaymentAllocationIn(
                                invoice_id=uuid.UUID(invoice["id"]), amount=Decimal("60")
                            )
                        ],
                    )
                    await session.commit()
                    results["b"] = "ok"
                except ConflictError as exc:
                    await session.rollback()
                    results["b"] = ("conflict", str(exc))

    task_a = asyncio.create_task(alloc_task_a())
    await a_locked.wait()
    task_b = asyncio.create_task(alloc_task_b())

    await asyncio.sleep(0.3)
    assert not task_b.done(), "task B did not block on task A's held invoice-row lock"

    release_a.set()
    await asyncio.gather(task_a, task_b)

    outcomes = [results["a"], results["b"]]
    winners = [r for r in outcomes if r == "ok"]
    losers = [r for r in outcomes if r != "ok"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1

    invoice_resp = await client.get(
        f"/api/v1/receivables/invoices/{invoice['id']}", headers={"X-Company-Id": str(company_id)}
    )
    # Capacity respected: settled_amount never exceeds total (100), even
    # though both payments together (120) could have overshot it.
    assert Decimal(invoice_resp.json()["settled_amount"]) <= Decimal("100.000000")
