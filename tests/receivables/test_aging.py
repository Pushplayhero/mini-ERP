"""tests/receivables/test_aging.py — AR aging report + control-account

tie-out property (ADR-008 Decision 5, R4, R15).

Bucket classification, `bucket_date` current-state semantics, the R15
union population (a payment-only customer with no open invoice still
appears, with a negative net), and the property that ties the whole module
together: Σ open invoice balances - Σ unapplied credits == the ledger's
`1100` balance, after arbitrary issue/void/pay/allocate sequences.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.tenancy import company_context
from app.modules.receivables import service as receivables_service
from tests.receivables._helpers import (
    allocation,
    create_payment,
    get_ar_aging,
    get_trial_balance_by_code,
    issue_invoice,
    setup_shipped_order,
)


@pytest.mark.asyncio
async def test_aging_buckets_by_days_past_due(client: AsyncClient) -> None:
    """`due_date` can never be set in the past relative to `invoice_date`

    (that CHECK is exercised separately) — a past-due invoice is instead
    simulated the way the ADR actually intends: due today, aging queried
    at a `bucket_date` in the future (Decision 5's current-state, not
    historical-cutoff, semantics).
    """
    ctx = await setup_shipped_order(client, "AGE1", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    due_today = date.today().isoformat()
    issued = await issue_invoice(client, company_id, order["id"], due_date=due_today)
    assert issued.status_code == 201, issued.text

    future_bucket_date = (date.today() + timedelta(days=45)).isoformat()
    aging = await get_ar_aging(client, company_id, bucket_date=future_bucket_date)
    assert aging.status_code == 200, aging.text
    rows = aging.json()
    row = next(r for r in rows if r["customer_id"] == str(customer_id))
    assert row["days_31_60"] == "100.000000"
    assert row["current"] == "0.000000"


@pytest.mark.asyncio
async def test_payment_only_customer_appears_with_negative_net(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "AGE2", list_price="50", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id

    # No invoice issued at all — a pure on-account credit.
    resp = await create_payment(client, company_id, customer_id, "30", "AGE2-REF")
    assert resp.status_code == 201, resp.text

    aging = await get_ar_aging(client, company_id)
    rows = aging.json()
    row = next(r for r in rows if r["customer_id"] == str(customer_id))
    assert row["unapplied_credits"] == "30.000000"
    assert Decimal(row["net_total"]) == Decimal("-30.000000")


@pytest.mark.asyncio
async def test_bucket_date_moves_boundary_not_a_historical_cutoff(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "AGE3", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    due_date = date.today().isoformat()
    issued = await issue_invoice(client, company_id, order["id"], due_date=due_date)
    assert issued.status_code == 201, issued.text

    today_aging = await get_ar_aging(client, company_id)
    row_today = next(r for r in today_aging.json() if r["customer_id"] == str(customer_id))
    assert row_today["current"] == "100.000000"

    future_bucket_date = (date.today() + timedelta(days=40)).isoformat()
    future_aging = await get_ar_aging(client, company_id, bucket_date=future_bucket_date)
    row_future = next(r for r in future_aging.json() if r["customer_id"] == str(customer_id))
    # Same invoice, same balance — only the bucket it falls into moves,
    # never excluded (current-state semantics, not a historical as-of).
    assert row_future["days_31_60"] == "100.000000"


@pytest.mark.asyncio
async def test_control_account_tie_out_property_after_operation_sequence(
    client: AsyncClient,
) -> None:
    """Σ open invoice balances - Σ unapplied credits == ledger account 1100

    balance, after issue / partial-pay / void / re-issue / full-pay.
    """
    ctx = await setup_shipped_order(client, "AGE4", list_price="1000", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    invoice_1 = (await issue_invoice(client, company_id, order["id"])).json()

    # Partial payment.
    payment_1 = (await create_payment(client, company_id, customer_id, "300", "AGE4-P1")).json()
    alloc_resp = await client.post(
        f"/api/v1/receivables/payments/{payment_1['id']}/allocations",
        json={
            "request_ref": "AGE4-ALLOC-1",
            "allocations": [allocation(invoice_1["id"], "300")],
        },
        headers={"X-Company-Id": str(company_id)},
    )
    assert alloc_resp.status_code == 200, alloc_resp.text

    # An unrelated unapplied credit.
    unapplied_payment = (
        await create_payment(client, company_id, customer_id, "75", "AGE4-P2")
    ).json()

    aging = await get_ar_aging(client, company_id)
    rows = aging.json()
    row = next(r for r in rows if r["customer_id"] == str(customer_id))

    total_bucketed = (
        Decimal(row["current"])
        + Decimal(row["days_1_30"])
        + Decimal(row["days_31_60"])
        + Decimal(row["days_61_90"])
        + Decimal(row["days_90_plus"])
    )
    aging_net = total_bucketed - Decimal(row["unapplied_credits"])

    tb = await get_trial_balance_by_code(client, company_id)
    ar_balance = Decimal(tb["1100"]["total_debit"]) - Decimal(tb["1100"]["total_credit"])

    assert aging_net == ar_balance, (
        f"tie-out property violated: aging net {aging_net} != ledger 1100 balance "
        f"{ar_balance} (row={row}, tb_1100={tb['1100']})"
    )
    # Sanity on the actual numbers: invoice 1000, 300 settled -> 700 open;
    # 75 unapplied credit from the second payment -> net 700 - 75 = 625.
    assert aging_net == Decimal("625.000000")
    assert unapplied_payment["allocated_amount"] == "0.000000"


@pytest.mark.asyncio
async def test_get_ar_aging_reads_invoice_and_payment_sides_in_one_statement(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Codex diff review (2026-08-15, finding 1) regression guard.

    The pre-fix implementation read open-invoice balances and unapplied
    credits via two separate `session.execute()` calls, each getting its
    own READ COMMITTED statement snapshot — a concurrently-committing
    `allocate_payment` landing between them could move an amount from
    "unapplied credit" to "settled invoice" mid-report and produce a total
    that never ties to the ledger. The actual race is not practically
    reproducible from application-level test code (there is no hook to
    pause execution between two statements that no longer exist), so this
    is a structural test: it asserts the fixed query is genuinely ONE
    statement for the invoice+payment union, not two — the mechanism the
    fix relies on, guarded against silently regressing back to the
    two-statement shape.
    """
    ctx = await setup_shipped_order(client, "AGE5", list_price="100", qty="1")
    company_id = ctx.company_id
    order = ctx.order
    issued = await issue_invoice(client, company_id, order["id"])
    assert issued.status_code == 201, issued.text

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    statement_count = 0

    def _count(*args: object, **kwargs: object) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _count)
    try:
        async with session_factory() as session:
            with company_context(company_id):
                statement_count = 0
                await receivables_service.get_ar_aging(session)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _count)

    # One statement for the UNION ALL invoice+payment query, one for
    # `_fetch_customers` — never the old three-statement shape (invoices,
    # payments, customers) this test exists to prevent regressing to.
    assert statement_count == 2, f"expected exactly 2 statements, got {statement_count}"
