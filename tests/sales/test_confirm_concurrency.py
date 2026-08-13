"""tests/sales/test_confirm_concurrency.py — ADR-006 R1: double-confirm race.

Same simulation approach as `tests/ledger/test_period_close_concurrency.py`
(see that file's docstring for the full methodology note): two independent
`AsyncSession`s, each its own real connection, driven by two `asyncio.Task`s
synchronized with `asyncio.Event`s, calling the exact same service function
(`app.modules.sales.service.confirm_order`) a real request would call — no
mocking of locks or SQL.

Proves ADR-006 R1's core claim: `confirm_order`'s `SELECT ... FOR UPDATE`
on the order row, taken *before* the status re-check, serializes two
concurrent confirm attempts on the same draft order so that exactly one
succeeds (its transaction is left uncommitted here — this test drives the
service layer directly, not the HTTP router — but the important, unmocked
part is that the second task's lock acquisition genuinely blocks on the
first task holding the row lock, and by the time it proceeds it observes
`status=confirmed`), and only one `sales.order_confirmed` outbox row is
ever written.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.masterdata.models import OutboxEvent
from app.modules.sales import service as sales_service
from tests.sales._helpers import (
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)


@pytest.mark.asyncio
async def test_concurrent_confirm_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "RACES1")
    customer_id = await create_customer(client, company_id, "RACECUST1")
    product_id = await create_product(client, company_id, "RACE-SKU-1", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    order_id = order["id"]

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, object] = {}

    async def confirm_task_a() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                # Same lock `confirm_order` takes internally — but we need to
                # observe "task A holds the lock" from *outside* confirm_order,
                # so we run confirm_order in a background coroutine and use a
                # sentinel via the DB row itself: task B waiting on FOR UPDATE
                # is proof enough (see assertion below), so task A simply
                # calls confirm_order and *delays* its own commit by holding
                # the transaction open via a second, blocking statement.
                try:
                    order_obj = await sales_service.confirm_order(session, order_id)
                    first_locked.set()
                    await release_first.wait()
                    await session.commit()
                    results["a"] = ("ok", order_obj.status.value)
                except ConflictError as exc:
                    first_locked.set()
                    await session.rollback()
                    results["a"] = ("conflict", str(exc))

    async def confirm_task_b() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    order_obj = await sales_service.confirm_order(session, order_id)
                    await session.commit()
                    results["b"] = ("ok", order_obj.status.value)
                except ConflictError as exc:
                    await session.rollback()
                    results["b"] = ("conflict", str(exc))

    task_a = asyncio.create_task(confirm_task_a())
    await first_locked.wait()
    task_b = asyncio.create_task(confirm_task_b())

    # Give task B every chance to (wrongly) race past task A's held lock
    # before task A ever commits.
    await asyncio.sleep(0.3)
    assert not task_b.done(), (
        "task B's confirm_order (SELECT ... FOR UPDATE) did not block on task A's "
        "held lock — ADR-006 R1's core guarantee is violated"
    )

    release_first.set()
    await asyncio.gather(task_a, task_b)

    outcomes = {results["a"][0], results["b"][0]}
    assert outcomes == {"ok", "conflict"}, f"expected exactly one winner, got {results}"

    async with session_factory() as session:
        with company_context(company_id):
            result = await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "sales.order_confirmed",
                    OutboxEvent.payload["source_id"].astext == order_id,
                )
            )
            rows = result.scalars().all()
    assert len(rows) == 1, "exactly one sales.order_confirmed outbox row must exist"
