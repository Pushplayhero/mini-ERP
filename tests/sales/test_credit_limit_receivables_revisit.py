"""tests/sales/test_credit_limit_receivables_revisit.py — ADR-008 Decision 6.

`tests/sales/test_credit_limit_plugin.py` covers the plugin's original
(Week 4) behavior, which is unaffected for orders that never ship or
invoice — those tests stay green unmodified. This file covers exactly the
revisit ADR-006 scheduled for Week 6: the Week 5 gap where a `SHIPPED`
order silently dropped out of exposure, and the new invoice-balance term.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.hooks import HookContext
from app.core.tenancy import company_context
from app.plugins.credit_limit import check_credit_limit
from tests.inventory._helpers import create_adjustment
from tests.ledger._helpers import create_account, create_period
from tests.receivables._helpers import allocation, create_payment, issue_invoice
from tests.sales._helpers import (
    confirm_order,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)
from tests.sales._helpers import create_company as sales_create_company


async def _company_with_chart(client: AsyncClient, code: str) -> uuid.UUID:
    company_id = await sales_create_company(client, code)
    await create_period(client, company_id, 2026, 8)
    for account_code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
        ("1300", "Inventory", "asset"),
    ):
        await create_account(client, company_id, account_code, name, acct_type)
    return company_id


@pytest.mark.asyncio
async def test_shipped_uninvoiced_order_still_counts_toward_exposure(client: AsyncClient) -> None:
    """The Week 5 gap this revisit closes: shipping an order used to drop

    it out of the old (CONFIRMED-only) exposure sum entirely.
    """
    from tests.inventory._helpers import create_adjustment
    from tests.sales._helpers import ship_order

    company_id = await _company_with_chart(client, "CLR1")
    customer_id = await create_customer(client, company_id, "CLR1CUST", credit_limit="150")
    product_id = await create_product(client, company_id, "CLR1-SKU", list_price="100")
    await create_adjustment(client, company_id, product_id, "10", "seed")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    assert (await confirm_order(client, company_id, order_1["id"])).status_code == 200
    ship_resp = await ship_order(client, company_id, order_1["id"])
    assert ship_resp.status_code == 200, ship_resp.text

    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    second = await confirm_order(client, company_id, order_2["id"])
    assert second.status_code == 409, (
        "shipped-but-uninvoiced order 1 (100) + order 2 (100) = 200 > 150 limit — "
        "the old formula would have wrongly allowed this because SHIPPED dropped out"
    )


@pytest.mark.asyncio
async def test_invoiced_order_counts_via_open_invoice_balance_not_twice(
    client: AsyncClient,
) -> None:
    """Once an order is invoiced, it must count exactly once — via the open

    invoice-balance term, not also via the uninvoiced-order term.
    """
    from tests.inventory._helpers import create_adjustment
    from tests.sales._helpers import ship_order

    company_id = await _company_with_chart(client, "CLR2")
    customer_id = await create_customer(client, company_id, "CLR2CUST", credit_limit="150")
    product_id = await create_product(client, company_id, "CLR2-SKU", list_price="100")
    await create_adjustment(client, company_id, product_id, "10", "seed")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    assert (await confirm_order(client, company_id, order_1["id"])).status_code == 200
    assert (await ship_order(client, company_id, order_1["id"])).status_code == 200
    issued = await issue_invoice(client, company_id, order_1["id"])
    assert issued.status_code == 201, issued.text

    # Still exactly 100 of exposure (not 200 from double-counting order 1
    # through both the uninvoiced-order and open-invoice terms).
    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="50")]
    )
    second = await confirm_order(client, company_id, order_2["id"])
    assert second.status_code == 200, (
        f"100 (open invoice) + 50 (this order) = 150 should be within the 150 limit: "
        f"{second.text}"
    )


@pytest.mark.asyncio
async def test_fully_paid_invoice_drops_out_of_exposure(client: AsyncClient) -> None:
    from tests.inventory._helpers import create_adjustment
    from tests.sales._helpers import ship_order

    company_id = await _company_with_chart(client, "CLR3")
    customer_id = await create_customer(client, company_id, "CLR3CUST", credit_limit="150")
    product_id = await create_product(client, company_id, "CLR3-SKU", list_price="100")
    await create_adjustment(client, company_id, product_id, "10", "seed")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    assert (await confirm_order(client, company_id, order_1["id"])).status_code == 200
    assert (await ship_order(client, company_id, order_1["id"])).status_code == 200
    invoice = (await issue_invoice(client, company_id, order_1["id"])).json()

    payment_resp = await create_payment(
        client,
        company_id,
        customer_id,
        "100",
        "CLR3-REF",
        allocations=[allocation(invoice["id"], "100")],
    )
    assert payment_resp.status_code == 201, payment_resp.text

    # Fully settled — exposure from order 1 is now zero; a new 150 order
    # fits exactly at the limit.
    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="150")]
    )
    second = await confirm_order(client, company_id, order_2["id"])
    assert second.status_code == 200, second.text


@pytest.mark.asyncio
async def test_check_credit_limit_reads_exposure_in_two_statements(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Codex diff review (2026-08-15, finding 2) regression guard.

    The pre-fix implementation summed the uninvoiced-orders term and the
    open-invoice-balance term via two separate `session.execute()` calls.
    The customer row lock (`with_for_update()`) serializes concurrent
    *confirms* against each other, but does not serialize against
    `receivables.void_invoice`/`create_invoice`, which touch only the
    invoice row — so an invoice void committing between the two reads
    could drop an order out of BOTH sums at once (excluded from the first
    query because a live invoice existed, excluded from the second because
    that invoice is now voided), silently understating exposure. The
    actual race is not practically reproducible from application-level
    test code, so this is a structural test: it asserts the fixed read is
    genuinely ONE combined SELECT for both exposure terms, plus the
    customer lock — two statements total, never the old three.
    """
    company_id = await _company_with_chart(client, "CLR4")
    customer_id = await create_customer(client, company_id, "CLR4CUST", credit_limit="1000")
    product_id = await create_product(client, company_id, "CLR4-SKU", list_price="100")
    await create_adjustment(client, company_id, product_id, "10", "seed")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )

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
                await check_credit_limit(
                    session,
                    HookContext(
                        company_id=company_id,
                        order_id=uuid.UUID(order["id"]),
                        order_no=order["order_no"],
                        customer_id=customer_id,
                        total=Decimal("100"),
                    ),
                )
            await session.rollback()
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _count)

    assert statement_count == 2, f"expected exactly 2 statements, got {statement_count}"
