"""tests/sales/test_credit_limit_plugin.py — `app.plugins.credit_limit` (ADR-006).

Covers the plugin's own behavior (within-limit pass, over-limit 409 with
the order left untouched in `draft`, the `credit_limit == 0` sentinel,
confirmed orders accumulating exposure, cancelled orders dropping out of
it, and a customer-row-lock concurrency proof) plus the ADR's central
demonstration: `hooks.unregister(...)` the plugin and prove a previously
blocked confirm now succeeds — `sales` never knows the plugin existed or
stopped existing.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core import hooks
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.sales import service as sales_service
from app.plugins import credit_limit
from tests.sales._helpers import (
    cancel_order,
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)


@pytest.mark.asyncio
async def test_confirm_within_credit_limit_succeeds(client: AsyncClient) -> None:
    company_id = await create_company(client, "CLA1")
    customer_id = await create_customer(client, company_id, "CLCUSTA1", credit_limit="1000")
    product_id = await create_product(client, company_id, "CL-SKU-A1", list_price="100")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "5", unit_price="100")]
    )
    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_confirm_exceeding_credit_limit_is_409_and_order_stays_draft(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "CLA2")
    customer_id = await create_customer(client, company_id, "CLCUSTA2", credit_limit="100")
    product_id = await create_product(client, company_id, "CL-SKU-A2", list_price="100")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "5", unit_price="100")]
    )
    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 409

    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    body = reread.json()
    assert body["status"] == "draft"
    # Confirm aborted entirely — snapshot fields were never (re-)written.
    assert body["confirmed_at"] is None


@pytest.mark.asyncio
async def test_credit_limit_zero_means_unchecked(client: AsyncClient) -> None:
    company_id = await create_company(client, "CLA3")
    customer_id = await create_customer(client, company_id, "CLCUSTA3", credit_limit="0")
    product_id = await create_product(client, company_id, "CL-SKU-A3", list_price="100000")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "999", unit_price="100000")]
    )
    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_confirmed_orders_accumulate_toward_credit_limit(client: AsyncClient) -> None:
    company_id = await create_company(client, "CLA4")
    customer_id = await create_customer(client, company_id, "CLCUSTA4", credit_limit="150")
    product_id = await create_product(client, company_id, "CL-SKU-A4", list_price="100")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    first = await confirm_order(client, company_id, order_1["id"])
    assert first.status_code == 200, first.text

    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    second = await confirm_order(client, company_id, order_2["id"])
    assert second.status_code == 409, "100 (confirmed) + 100 (this order) = 200 > 150 limit"


@pytest.mark.asyncio
async def test_cancelled_orders_do_not_count_toward_credit_limit(client: AsyncClient) -> None:
    company_id = await create_company(client, "CLA5")
    customer_id = await create_customer(client, company_id, "CLCUSTA5", credit_limit="150")
    product_id = await create_product(client, company_id, "CL-SKU-A5", list_price="100")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    first = await confirm_order(client, company_id, order_1["id"])
    assert first.status_code == 200, first.text
    cancel_resp = await cancel_order(client, company_id, order_1["id"])
    assert cancel_resp.status_code == 200, cancel_resp.text

    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    second = await confirm_order(client, company_id, order_2["id"])
    assert second.status_code == 200, (
        "the cancelled order's total must not count toward exposure: " f"{second.text}"
    )


# ---------------------------------------------------------------------------
# Customer-row lock concurrency (same doctrine as ADR-006 R1, applied to the
# plugin's own SELECT ... FOR UPDATE on the customer row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_confirms_against_same_near_limit_customer_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "CLB1")
    customer_id = await create_customer(client, company_id, "CLCUSTB1", credit_limit="100")
    product_id = await create_product(client, company_id, "CL-SKU-B1", list_price="100")

    order_1 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )
    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="100")]
    )

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, object] = {}

    async def confirm_task(key: str, order_id: str, *, is_first: bool) -> None:
        if not is_first:
            await first_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    order_obj = await sales_service.confirm_order(session, order_id)
                    if is_first:
                        first_locked.set()
                        await release_first.wait()
                    await session.commit()
                    results[key] = ("ok", order_obj.status.value)
                except ConflictError as exc:
                    if is_first:
                        first_locked.set()
                    await session.rollback()
                    results[key] = ("conflict", str(exc))

    task_a = asyncio.create_task(confirm_task("a", order_1["id"], is_first=True))
    await first_locked.wait()
    task_b = asyncio.create_task(confirm_task("b", order_2["id"], is_first=False))

    await asyncio.sleep(0.3)
    assert not task_b.done(), (
        "task B's credit-limit customer-row FOR UPDATE did not block on task A's " "held lock"
    )

    release_first.set()
    await asyncio.gather(task_a, task_b)

    outcomes = {results["a"][0], results["b"][0]}
    assert outcomes == {"ok", "conflict"}, f"expected exactly one winner, got {results}"


# ---------------------------------------------------------------------------
# The decoupling demonstration (ADR-006's central claim)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_the_plugin_allows_confirm_that_would_otherwise_be_blocked(
    client: AsyncClient,
) -> None:
    """Pull `credit_limit.check_credit_limit` out of the hook registry and prove

    an order that would otherwise be blocked by it now confirms successfully
    — `app.modules.sales` never imports, calls, or knows about
    `app.plugins.credit_limit`; removing the one registration line (here,
    via `hooks.unregister`) is the entire footprint of "uninstalling" this
    customization. Registration is restored in `finally` so no other test in
    the session is affected by this one unregistering the real, process-wide
    plugin binding `app.main` set up at import time.
    """
    company_id = await create_company(client, "CLC1")
    customer_id = await create_customer(client, company_id, "CLCUSTC1", credit_limit="10")
    product_id = await create_product(client, company_id, "CL-SKU-C1", list_price="100")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "5", unit_price="100")]
    )

    # Sanity check: with the plugin installed, this order is blocked.
    blocked = await confirm_order(client, company_id, order["id"])
    assert blocked.status_code == 409

    hooks.unregister(sales_service.SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)
    try:
        allowed = await confirm_order(client, company_id, order["id"])
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["status"] == "confirmed"
    finally:
        hooks.register(sales_service.SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)
