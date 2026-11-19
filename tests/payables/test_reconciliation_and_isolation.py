"""tests/payables/test_reconciliation_and_isolation.py — rebuild, replay, isolation (ADR-015)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.cli import replay_outbox
from app.cli.rebuild_ap_balances import rebuild_ap_balances
from app.core.authorization import actor_context
from app.core.tenancy import company_context
from app.modules.ledger import posting
from app.modules.ledger import service as ledger_service
from app.modules.masterdata.models import OutboxEvent
from app.modules.payables.models import VendorBillStatus
from tests.conftest import TEST_ADMIN_ACTOR, company_headers
from tests.payables._helpers import (
    allocation,
    create_payment,
    issue_bill,
    setup_received_order,
)


@pytest.fixture(autouse=True)
def _bind_admin() -> Iterator[None]:
    with actor_context(TEST_ADMIN_ACTOR):
        yield


@pytest.mark.asyncio
async def test_rebuild_ap_balances_repairs_corrupted_projections(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_received_order(client, "APREC1", standard_cost="200", qty="1")
    bill = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()
    payment = (
        await create_payment(
            client,
            ctx.company_id,
            ctx.supplier_id,
            "120",
            "APREC1-P1",
            allocations=[allocation(bill["id"], "120")],
        )
    ).json()

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE vendor_bills SET settled_amount = 0, status = 'OPEN' WHERE id = :bid"),
            {"bid": bill["id"]},
        )
        await conn.execute(
            text("UPDATE vendor_payments SET allocated_amount = 0 WHERE id = :pid"),
            {"pid": payment["id"]},
        )

    summary = await rebuild_ap_balances()
    assert summary.companies >= 1
    assert summary.bills_updated >= 1
    assert summary.payments_updated >= 1

    b_after = (
        await client.get(
            f"/api/v1/payables/bills/{bill['id']}", headers=company_headers(ctx.company_id)
        )
    ).json()
    p_after = (
        await client.get(
            f"/api/v1/payables/payments/{payment['id']}", headers=company_headers(ctx.company_id)
        )
    ).json()
    assert Decimal(b_after["settled_amount"]) == Decimal("120.000000")
    assert b_after["status"] == VendorBillStatus.PARTIAL.value
    assert Decimal(p_after["allocated_amount"]) == Decimal("120.000000")


@pytest.mark.asyncio
async def test_replay_of_duplicate_payables_bill_issued_posts_once(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_received_order(client, "APREC2", standard_cost="90", qty="1")
    bill = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    payload = {
        "company_id": str(ctx.company_id),
        "source_id": bill["id"],
        "event_date": bill["bill_date"],
        "bill_no": bill["bill_no"],
        "order_id": ctx.order["id"],
        "supplier_id": str(ctx.supplier_id),
        "total": bill["total"],
    }
    async with session_factory() as session:
        session.add(
            OutboxEvent(event_type=posting.PAYABLES_BILL_ISSUED_EVENT_TYPE, payload=payload)
        )
        await session.commit()

    summary = await replay_outbox.replay_outbox()
    assert summary.failed == 0

    async with session_factory() as session:
        with company_context(ctx.company_id):
            entries = await ledger_service.list_journal_entries(session)
    matching = [e for e in entries if e.source_id == uuid.UUID(bill["id"])]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_payables_cross_company_isolation(client: AsyncClient) -> None:
    ctx_a = await setup_received_order(client, "APISOA", standard_cost="100", qty="1")
    ctx_b = await setup_received_order(client, "APISOB", standard_cost="100", qty="1")

    bill_a = (await issue_bill(client, ctx_a.company_id, ctx_a.order["id"])).json()
    pay_a = (
        await create_payment(client, ctx_a.company_id, ctx_a.supplier_id, "50", "APISO-REF")
    ).json()

    headers_b = company_headers(ctx_b.company_id)
    assert (
        await client.get(f"/api/v1/payables/bills/{bill_a['id']}", headers=headers_b)
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/payables/bills/{bill_a['id']}/void", headers=headers_b)
    ).status_code == 404
    assert (await client.get("/api/v1/payables/bills", headers=headers_b)).json() == []

    assert (
        await client.get(f"/api/v1/payables/payments/{pay_a['id']}", headers=headers_b)
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/payables/payments/{pay_a['id']}/void", headers=headers_b)
    ).status_code == 404
    assert (await client.get("/api/v1/payables/payments", headers=headers_b)).json() == []

    cross_alloc = await client.post(
        f"/api/v1/payables/payments/{pay_a['id']}/allocations",
        json={"request_ref": "CROSS", "allocations": [allocation(bill_a["id"], "50")]},
        headers=headers_b,
    )
    assert cross_alloc.status_code in (404, 422)

    aging_b = await client.get("/api/v1/payables/reports/ap-aging", headers=headers_b)
    assert aging_b.status_code == 200
    assert str(ctx_a.supplier_id) not in {row["supplier_id"] for row in aging_b.json()}
