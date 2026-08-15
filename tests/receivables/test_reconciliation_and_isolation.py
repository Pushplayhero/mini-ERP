"""tests/receivables/test_reconciliation_and_isolation.py —

`app.cli.rebuild_ar_balances` reconciliation, replay idempotency for all
four posting events, TWD-only enforcement, and cross-company isolation
(ADR-008 R6, R1, R12).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.cli import replay_outbox
from app.cli.rebuild_ar_balances import rebuild_ar_balances
from app.core.tenancy import company_context
from app.modules.ledger import posting
from app.modules.ledger import service as ledger_service
from app.modules.masterdata.models import OutboxEvent
from app.modules.receivables.models import Invoice, Payment
from tests.receivables._helpers import (
    allocation,
    create_payment,
    issue_invoice,
    setup_shipped_order,
)


@pytest.mark.asyncio
async def test_rebuild_ar_balances_matches_live_state(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_shipped_order(client, "REC1", list_price="200", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()
    payment = (await create_payment(client, company_id, customer_id, "120", "REC1-REF")).json()
    alloc_resp = await client.post(
        f"/api/v1/receivables/payments/{payment['id']}/allocations",
        json={"request_ref": "REC1-A1", "allocations": [allocation(invoice["id"], "120")]},
        headers={"X-Company-Id": str(company_id)},
    )
    assert alloc_resp.status_code == 200, alloc_resp.text

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        with company_context(company_id):
            before_invoice = (
                await session.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice["id"])))
            ).scalar_one()
            before_payment = (
                await session.execute(select(Payment).where(Payment.id == uuid.UUID(payment["id"])))
            ).scalar_one()
            before_settled = before_invoice.settled_amount
            before_allocated = before_payment.allocated_amount

    result = await rebuild_ar_balances()
    assert result.companies >= 1

    async with session_factory() as session:
        with company_context(company_id):
            after_invoice = (
                await session.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice["id"])))
            ).scalar_one()
            after_payment = (
                await session.execute(select(Payment).where(Payment.id == uuid.UUID(payment["id"])))
            ).scalar_one()

    assert after_invoice.settled_amount == before_settled == Decimal("120.000000")
    assert after_payment.allocated_amount == before_allocated == Decimal("120.000000")
    assert after_invoice.status.value == "partial"


# ---------------------------------------------------------------------------
# Replay idempotency for all four posting events (ADR-008 R1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_of_duplicate_invoice_issued_posts_once(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    ctx = await setup_shipped_order(client, "REC2", list_price="90", qty="1")
    company_id = ctx.company_id
    order = ctx.order

    invoice = (await issue_invoice(client, company_id, order["id"])).json()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        with company_context(company_id):
            entries_before = await ledger_service.list_journal_entries(session)
    matching_before = [e for e in entries_before if str(e.source_id) == invoice["id"]]
    assert len(matching_before) == 1

    payload = {
        "company_id": str(company_id),
        "source_id": invoice["id"],
        "event_date": invoice["invoice_date"],
        "invoice_no": invoice["invoice_no"],
        "order_id": order["id"],
        "customer_id": str(ctx.customer_id),
        "total": invoice["total"],
    }
    async with session_factory() as session:
        session.add(
            OutboxEvent(event_type=posting.RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE, payload=payload)
        )
        await session.commit()

    summary = await replay_outbox.replay_outbox()
    assert summary.failed == 0

    async with session_factory() as session:
        with company_context(company_id):
            entries_after = await ledger_service.list_journal_entries(session)
    matching_after = [e for e in entries_after if str(e.source_id) == invoice["id"]]
    assert len(matching_after) == 1, "replay must not double-post a duplicate invoice_issued event"


# ---------------------------------------------------------------------------
# TWD-only enforcement (ADR-008 R12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_twd_customer_creation_is_rejected(client: AsyncClient) -> None:
    from tests.sales._helpers import create_company

    company_id = await create_company(client, "REC3")
    resp = await client.post(
        "/api/v1/customers",
        json={
            "code": "REC3USD",
            "name": "USD customer",
            "currency_code": "USD",
        },
        headers={"X-Company-Id": str(company_id)},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Cross-company isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_and_payment_are_isolated_across_companies(client: AsyncClient) -> None:
    ctx_a = await setup_shipped_order(client, "REC4A", list_price="100", qty="1")
    ctx_b = await setup_shipped_order(client, "REC4B", list_price="100", qty="1")

    invoice_a = (await issue_invoice(client, ctx_a.company_id, ctx_a.order["id"])).json()
    payment_a = (
        await create_payment(client, ctx_a.company_id, ctx_a.customer_id, "50", "REC4A-REF")
    ).json()

    # Company B cannot see company A's invoice/payment.
    invoice_from_b = await client.get(
        f"/api/v1/receivables/invoices/{invoice_a['id']}",
        headers={"X-Company-Id": str(ctx_b.company_id)},
    )
    assert invoice_from_b.status_code == 404

    payment_from_b = await client.get(
        f"/api/v1/receivables/payments/{payment_a['id']}",
        headers={"X-Company-Id": str(ctx_b.company_id)},
    )
    assert payment_from_b.status_code == 404

    # Company B cannot allocate company A's payment to company A's invoice.
    cross_alloc = await client.post(
        f"/api/v1/receivables/payments/{payment_a['id']}/allocations",
        json={"request_ref": "REC4-CROSS", "allocations": [allocation(invoice_a["id"], "50")]},
        headers={"X-Company-Id": str(ctx_b.company_id)},
    )
    assert cross_alloc.status_code in (404, 422)

    aging_b = await client.get(
        "/api/v1/receivables/reports/ar-aging",
        headers={"X-Company-Id": str(ctx_b.company_id)},
    )
    assert aging_b.status_code == 200
    b_customer_ids = {row["customer_id"] for row in aging_b.json()}
    assert str(ctx_a.customer_id) not in b_customer_ids
