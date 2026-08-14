"""tests/inventory/test_rebuild_cli.py — `app.cli.rebuild_stock_summary` (ADR-007 R2).

Two scenarios per the brief: a reconciliation test (rebuild against
already-correct data must be a no-op that reproduces `SUM(stock_moves)`
exactly) and a corruption-recovery test (manually drift `stock_summary`
away from `SUM(stock_moves)`, then prove rebuild restores it). Plus a
diff-review addition: a lock-contention proof for the phantom-row-race fix
(`app.core.locking`) rebuild and every normal writer now share.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.cli import rebuild_stock_summary
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import company_context
from tests.inventory._helpers import create_adjustment, create_company, create_product, on_hand


@pytest.mark.asyncio
async def test_rebuild_reconciles_with_sum_of_moves_when_already_correct(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_a = await create_company(client, "REBLD1A")
    company_b = await create_company(client, "REBLD1B")
    product_a1 = await create_product(client, company_a, "REBLD1A-1")
    product_a2 = await create_product(client, company_a, "REBLD1A-2")
    product_b1 = await create_product(client, company_b, "REBLD1B-1")

    await create_adjustment(client, company_a, product_a1, "10", "in")
    await create_adjustment(client, company_a, product_a1, "-3", "out")
    await create_adjustment(client, company_a, product_a2, "5", "in")
    await create_adjustment(client, company_b, product_b1, "20", "in")

    result = await rebuild_stock_summary.rebuild_stock_summary()
    assert result.companies >= 2
    assert result.products_updated >= 3

    assert await on_hand(client, company_a, product_a1) == Decimal("7")
    assert await on_hand(client, company_a, product_a2) == Decimal("5")
    assert await on_hand(client, company_b, product_b1) == Decimal("20")


@pytest.mark.asyncio
async def test_rebuild_recovers_from_corrupted_summary(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "REBLD2")
    product_id = await create_product(client, company_id, "REBLD2-1")

    await create_adjustment(client, company_id, product_id, "10", "in")
    await create_adjustment(client, company_id, product_id, "-2", "out")
    # True on_hand per SUM(stock_moves) is 8.

    # Manually corrupt the maintained summary row directly at the DB layer
    # (simulating drift — a bug, a manual DB edit, whatever) so it disagrees
    # with SUM(stock_moves).
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stock_summary SET on_hand = 999 "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(company_id), "pid": str(product_id)},
        )

    assert await on_hand(client, company_id, product_id) == Decimal("999")

    result = await rebuild_stock_summary.rebuild_stock_summary()
    assert result.companies >= 1

    assert await on_hand(client, company_id, product_id) == Decimal("8")


@pytest.mark.asyncio
async def test_rebuild_creates_missing_summary_row_from_moves(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "REBLD3")
    product_id = await create_product(client, company_id, "REBLD3-1")

    await create_adjustment(client, company_id, product_id, "6", "in")

    # Delete the maintained summary row entirely, simulating a worse form of
    # drift than a wrong value — no projection at all for a product that
    # does have moves.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM stock_summary WHERE company_id = :cid AND product_id = :pid"),
            {"cid": str(company_id), "pid": str(product_id)},
        )

    assert await on_hand(client, company_id, product_id) == Decimal("0")

    await rebuild_stock_summary.rebuild_stock_summary()

    assert await on_hand(client, company_id, product_id) == Decimal("6")


@pytest.mark.asyncio
async def test_rebuild_exclusive_lock_blocks_concurrent_writer(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Diff-review test for the phantom-row-race fix (Important finding
    #3): prove the EXCLUSIVE per-company advisory lock rebuild takes
    (`app.core.locking`) actually blocks a concurrent writer's SHARED
    acquisition of the same lock — the mechanism `get_or_lock_summary`
    relies on to close the race where a writer creates a `stock_summary`
    row rebuild's own `FOR UPDATE` step never saw because it didn't exist
    yet when rebuild locked "every existing row".
    """
    company_id = await create_company(client, "REBLD4")
    await create_product(client, company_id, "REBLD4-1")

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    exclusive_held = asyncio.Event()
    release_exclusive = asyncio.Event()
    results: dict[str, str] = {}

    async def hold_exclusive() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                await acquire_company_advisory_lock(session, company_id, exclusive=True)
                exclusive_held.set()
                await release_exclusive.wait()
                await session.commit()
        results["exclusive"] = "done"

    async def try_shared() -> None:
        await exclusive_held.wait()
        async with session_factory() as session:
            with company_context(company_id):
                await acquire_company_advisory_lock(session, company_id, exclusive=False)
                await session.commit()
        results["shared"] = "done"

    t_exclusive = asyncio.create_task(hold_exclusive())
    await exclusive_held.wait()
    t_shared = asyncio.create_task(try_shared())

    await asyncio.sleep(0.3)
    assert (
        not t_shared.done()
    ), "SHARED advisory lock acquisition did not block on the held EXCLUSIVE lock"

    release_exclusive.set()
    await asyncio.gather(t_exclusive, t_shared)
    assert results == {"exclusive": "done", "shared": "done"}
