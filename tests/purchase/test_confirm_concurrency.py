"""tests/purchase/test_confirm_concurrency.py — row-lock serialization races (ADR-014)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.authorization import actor_context
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.inventory.models import StockMove, StockSummary
from app.modules.ledger.models import JournalEntry
from app.modules.masterdata.models import OutboxEvent
from app.modules.purchase import service as purchase_service
from tests.conftest import TEST_ADMIN_ACTOR
from tests.ledger._helpers import create_account, create_period
from tests.purchase._helpers import (
    confirm_order,
    create_company,
    create_draft_order,
    create_product,
    create_supplier,
    order_line,
)


@pytest.mark.asyncio
async def test_concurrent_po_confirm_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "PRACE1")
    supplier_id = await create_supplier(client, company_id, "PSUP1")
    product_id = await create_product(client, company_id, "PSKU-R1", standard_cost="10")

    order = await create_draft_order(
        client, company_id, supplier_id, [order_line(product_id, "2", unit_cost="15")]
    )
    order_id = uuid.UUID(order["id"])

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, tuple[str, str]] = {}

    async def task_a() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    po = await purchase_service.confirm_order(session, order_id)
                    first_locked.set()
                    await release_first.wait()
                    await session.commit()
                    results["a"] = ("ok", po.status.value)
                except ConflictError as exc:
                    first_locked.set()
                    await session.rollback()
                    results["a"] = ("conflict", str(exc))

    async def task_b() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    po = await purchase_service.confirm_order(session, order_id)
                    await session.commit()
                    results["b"] = ("ok", po.status.value)
                except ConflictError as exc:
                    await session.rollback()
                    results["b"] = ("conflict", str(exc))

    t_a = asyncio.create_task(task_a())
    await first_locked.wait()
    t_b = asyncio.create_task(task_b())

    await asyncio.sleep(0.3)
    assert not t_b.done(), "task_b confirm_order must block on task_a FOR UPDATE lock"

    release_first.set()
    await asyncio.gather(t_a, t_b)

    assert {results["a"][0], results["b"][0]} == {"ok", "conflict"}

    async with session_factory() as session:
        with company_context(company_id):
            outbox_count = len(
                (
                    await session.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.event_type == "purchase.order_confirmed"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert outbox_count == 1


async def _setup_confirmed_po_with_grir(
    client: AsyncClient,
    code: str,
    *,
    sku: str,
    qty: str,
    standard_cost: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    company_id = await create_company(client, code)
    today = datetime.now(timezone.utc)  # noqa: UP017
    await create_period(client, company_id, today.year, today.month)
    await create_account(client, company_id, "1300", "Inventory", "asset")
    await create_account(client, company_id, "2110", "Accrued Purchases", "liability")

    supplier_id = await create_supplier(client, company_id, f"{code}-SUP")
    product_id = await create_product(client, company_id, sku, standard_cost=standard_cost)
    order = await create_draft_order(client, company_id, supplier_id, [order_line(product_id, qty)])
    assert (await confirm_order(client, company_id, order["id"])).status_code == 200
    return company_id, product_id, uuid.UUID(order["id"])


@pytest.mark.asyncio
async def test_concurrent_po_receive_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id, product_id, order_id = await _setup_confirmed_po_with_grir(
        client, "PRACE2", sku="PSKU-R2", qty="3", standard_cost="25"
    )

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, tuple[str, str]] = {}

    async def task_a() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    po = await purchase_service.receive_order(session, order_id)
                    first_locked.set()
                    await release_first.wait()
                    await session.commit()
                    results["a"] = ("ok", po.status.value)
                except ConflictError as exc:
                    first_locked.set()
                    await session.rollback()
                    results["a"] = ("conflict", str(exc))

    async def task_b() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    po = await purchase_service.receive_order(session, order_id)
                    await session.commit()
                    results["b"] = ("ok", po.status.value)
                except ConflictError as exc:
                    await session.rollback()
                    results["b"] = ("conflict", str(exc))

    t_a = asyncio.create_task(task_a())
    await first_locked.wait()
    t_b = asyncio.create_task(task_b())

    await asyncio.sleep(0.3)
    assert not t_b.done(), "task_b receive_order must block on task_a FOR UPDATE lock"

    release_first.set()
    await asyncio.gather(t_a, t_b)

    assert {results["a"][0], results["b"][0]} == {"ok", "conflict"}

    async with session_factory() as session:
        with company_context(company_id):
            summary = (
                await session.execute(
                    select(StockSummary).where(
                        StockSummary.company_id == company_id,
                        StockSummary.product_id == product_id,
                    )
                )
            ).scalar_one()
            assert summary.on_hand == Decimal("3")

            moves = (
                (await session.execute(select(StockMove).where(StockMove.product_id == product_id)))
                .scalars()
                .all()
            )
            assert len(moves) == 1

            entries = (
                (
                    await session.execute(
                        select(JournalEntry).where(
                            JournalEntry.source_type == "purchase.goods_received"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(entries) == 1


@pytest.mark.asyncio
async def test_concurrent_po_receive_vs_cancel_exactly_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id, _product_id, order_id = await _setup_confirmed_po_with_grir(
        client, "PRACE3", sku="PSKU-R3", qty="2", standard_cost="20"
    )

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, str] = {}

    async def recv_task() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await purchase_service.receive_order(session, order_id)
                    first_locked.set()
                    await release_first.wait()
                    await session.commit()
                    results["receive"] = "ok"
                except ConflictError:
                    first_locked.set()
                    await session.rollback()
                    results["receive"] = "conflict"

    async def cancel_task() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await purchase_service.cancel_order(session, order_id)
                    results["cancel"] = "ok"
                except ConflictError:
                    await session.rollback()
                    results["cancel"] = "conflict"

    t_r = asyncio.create_task(recv_task())
    await first_locked.wait()
    t_c = asyncio.create_task(cancel_task())

    await asyncio.sleep(0.3)
    assert not t_c.done(), "cancel_order must block on receive_order FOR UPDATE lock"

    release_first.set()
    await asyncio.gather(t_r, t_c)

    assert set(results.values()) == {"ok", "conflict"}
