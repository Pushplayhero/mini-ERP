"""tests/sales/test_shipping.py — `POST /sales-orders/{id}/ship` (ADR-007).

The flagship demo: one call moves stock AND books COGS/Inventory, or does
neither. Covers the full happy path (stock down, COGS posted, trial balance
moved), atomic rollback on insufficient stock, the zero-cost skip, the two
concurrency races R1 doctrine promises to serialize (ship-vs-ship on shared
stock, ship-vs-cancel on the same order), replay's duplicate-delivery
safety for stock, and the post-ship state-machine edges (`shipped` orders
cannot cancel or re-ship).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import replay_outbox
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.modules.ledger import posting
from app.modules.ledger import service as ledger_service
from app.modules.masterdata.models import OutboxEvent
from app.modules.sales import service as sales_service
from tests.inventory._helpers import create_adjustment as inventory_adjust
from tests.inventory._helpers import get_summary as inventory_get_summary
from tests.ledger._helpers import create_account, create_period
from tests.sales._helpers import (
    cancel_order,
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
    ship_order,
)


async def _setup_company_with_product(
    client: AsyncClient,
    code: str,
    *,
    standard_cost: str = "30",
    list_price: str = "100",
    stock: str = "10",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Company with an open period + 5000/1300 accounts, and a product with
    `stock` units seeded via a manual adjustment. Returns `(company_id,
    product_id)`. Shared setup for every test below that needs a shippable
    product — kept separate from `_setup_shippable` (which also builds a
    confirmed order) so the concurrency tests, which need two *separate*
    orders against the same product, can reuse just this half.
    """
    company_id = await create_company(client, code)
    today = datetime.now(timezone.utc)  # noqa: UP017 — see confirm_order's comment in service.py
    await create_period(client, company_id, today.year, today.month)
    await create_account(client, company_id, "5000", "COGS", "expense")
    await create_account(client, company_id, "1300", "Inventory", "asset")
    product_id = await create_product(
        client, company_id, f"{code}-SKU", list_price=list_price, standard_cost=standard_cost
    )
    if Decimal(stock) != 0:
        adjust_resp = await inventory_adjust(client, company_id, product_id, stock, "initial stock")
        assert adjust_resp.status_code == 201, adjust_resp.text
    return company_id, product_id


async def _setup_shippable(
    client: AsyncClient,
    code: str,
    *,
    standard_cost: str = "30",
    list_price: str = "100",
    initial_stock: str = "10",
    qty: str = "2",
) -> tuple[uuid.UUID, uuid.UUID, dict]:
    """`_setup_company_with_product` plus a customer and a *confirmed* order
    for `qty` of that product. Returns `(company_id, product_id, order)`.
    """
    company_id, product_id = await _setup_company_with_product(
        client, code, standard_cost=standard_cost, list_price=list_price, stock=initial_stock
    )
    customer_id = await create_customer(client, company_id, f"{code}CUST")
    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, qty, unit_price=list_price)]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text

    return company_id, product_id, order


async def _on_hand(client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    resp = await inventory_get_summary(client, company_id, product_id)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    return Decimal(rows[0]["on_hand"]) if rows else Decimal("0")


# ---------------------------------------------------------------------------
# Happy path — the flagship demo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_happy_path_moves_stock_and_posts_cogs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id, product_id, order = await _setup_shippable(
        client, "SHIP1", standard_cost="30", initial_stock="10", qty="2"
    )

    resp = await ship_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    shipped = resp.json()
    assert shipped["status"] == "shipped"

    # Stock: 10 in, 2 shipped out.
    assert await _on_hand(client, company_id, product_id) == Decimal("8")

    moves_resp = await client.get(
        "/api/v1/inventory/moves",
        params={"product_id": str(product_id)},
        headers={"X-Company-Id": str(company_id)},
    )
    shipment_moves = [m for m in moves_resp.json() if m["move_type"] == "shipment"]
    assert len(shipment_moves) == 1
    assert shipment_moves[0]["qty_delta"] == "-2.000000"
    assert shipment_moves[0]["source_type"] == "sales.goods_shipped"
    assert shipment_moves[0]["source_id"] == order["id"]

    # Ledger: one balanced entry, debit COGS / credit Inventory, amount = 2 * 30 = 60.
    with company_context(company_id):
        entries = await ledger_service.list_journal_entries(db_session)
    matching = [e for e in entries if str(e.source_id) == order["id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.source_type == "sales.goods_shipped"
    total_debit = sum((line.debit for line in entry.lines), Decimal("0"))
    total_credit = sum((line.credit for line in entry.lines), Decimal("0"))
    assert total_debit == total_credit == Decimal("60.000000")

    # Trial balance reflects the movement.
    with company_context(company_id):
        trial_balance = await ledger_service.get_trial_balance(db_session)
    tb_by_code = {line.account_code: line for line in trial_balance}
    assert tb_by_code["5000"].total_debit == Decimal("60.000000")
    assert tb_by_code["1300"].total_credit == Decimal("60.000000")


@pytest.mark.asyncio
async def test_ship_order_with_duplicate_product_line_deducts_full_quantity_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Diff-review regression for Critical finding #1: nothing in sales'
    schema or the DB forbids an order listing the same product on more than
    one line. Before the fix, the second line's `stock_moves` insert
    collided with the first's on `uq_stock_moves_source` and was
    misdiagnosed as an at-least-once replay, rolling back the whole
    SAVEPOINT — stock silently stayed untouched while ledger still posted
    COGS and the order still shipped as if nothing went wrong. Two lines of
    the same product must now aggregate into exactly one `stock_moves` row
    for the combined quantity.
    """
    company_id, product_id = await _setup_company_with_product(
        client, "SHIP6", standard_cost="30", stock="10"
    )
    customer_id = await create_customer(client, company_id, "SHIP6CUST")
    order = await create_draft_order(
        client,
        company_id,
        customer_id,
        [
            order_line(product_id, "2", unit_price="100"),
            order_line(product_id, "3", unit_price="100"),
        ],
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text

    resp = await ship_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "shipped"

    # Stock: 10 in, 2 + 3 = 5 shipped out — deducted exactly once, never
    # silently skipped.
    assert await _on_hand(client, company_id, product_id) == Decimal("5")

    moves_resp = await client.get(
        "/api/v1/inventory/moves",
        params={"product_id": str(product_id)},
        headers={"X-Company-Id": str(company_id)},
    )
    shipment_moves = [m for m in moves_resp.json() if m["move_type"] == "shipment"]
    assert len(shipment_moves) == 1, "duplicate product lines must aggregate into one move"
    assert shipment_moves[0]["qty_delta"] == "-5.000000"

    # Ledger: COGS reflects the combined quantity (5 * 30 = 150), not a
    # skipped/zero entry.
    with company_context(company_id):
        entries = await ledger_service.list_journal_entries(db_session)
    matching = [e for e in entries if str(e.source_id) == order["id"]]
    assert len(matching) == 1
    total_debit = sum((line.debit for line in matching[0].lines), Decimal("0"))
    assert total_debit == Decimal("150.000000")


# ---------------------------------------------------------------------------
# Insufficient stock — whole ship aborts atomically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_insufficient_stock_aborts_everything(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id, product_id, order = await _setup_shippable(
        client, "SHIP2", standard_cost="30", initial_stock="1", qty="5"
    )

    resp = await ship_order(client, company_id, order["id"])
    assert resp.status_code == 409, resp.text

    # Order is still confirmed, not half-shipped.
    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert reread.json()["status"] == "confirmed"

    # Stock untouched — no partial deduction.
    assert await _on_hand(client, company_id, product_id) == Decimal("1")

    # No journal entry was posted either.
    with company_context(company_id):
        entries = await ledger_service.list_journal_entries(db_session)
    assert [e for e in entries if str(e.source_id) == order["id"]] == []


# ---------------------------------------------------------------------------
# Zero-cost shipment — stock still moves, no journal entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_zero_cost_product_skips_journal_entry(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id, product_id, order = await _setup_shippable(
        client, "SHIP3", standard_cost="0", initial_stock="10", qty="3"
    )

    resp = await ship_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "shipped"

    assert await _on_hand(client, company_id, product_id) == Decimal("7")

    with company_context(company_id):
        entries = await ledger_service.list_journal_entries(db_session)
    assert [e for e in entries if str(e.source_id) == order["id"]] == []


# ---------------------------------------------------------------------------
# Post-ship state machine edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shipped_order_cannot_be_cancelled(client: AsyncClient) -> None:
    company_id, _product_id, order = await _setup_shippable(client, "SHIP4")
    ship_resp = await ship_order(client, company_id, order["id"])
    assert ship_resp.status_code == 200, ship_resp.text

    cancel_resp = await cancel_order(client, company_id, order["id"])
    assert cancel_resp.status_code == 409


@pytest.mark.asyncio
async def test_shipped_order_cannot_be_shipped_again(client: AsyncClient) -> None:
    company_id, _product_id, order = await _setup_shippable(client, "SHIP5")
    first = await ship_order(client, company_id, order["id"])
    assert first.status_code == 200, first.text

    second = await ship_order(client, company_id, order["id"])
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Concurrency: two orders racing for the same, scarce stock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_ship_for_scarce_stock_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id, product_id = await _setup_company_with_product(
        client, "SHIPR1", standard_cost="4", list_price="10", stock="1"
    )
    customer_id = await create_customer(client, company_id, "SHIPR1CUST")

    order_a = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    order_b = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    assert (await confirm_order(client, company_id, order_a["id"])).status_code == 200
    assert (await confirm_order(client, company_id, order_b["id"])).status_code == 200

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    a_locked = asyncio.Event()
    release_a = asyncio.Event()
    results: dict[str, object] = {}

    async def ship_task_a() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.ship_order(session, uuid.UUID(order_a["id"]))
                    a_locked.set()
                    await release_a.wait()
                    await session.commit()
                    results["a"] = "ok"
                except ConflictError as exc:
                    a_locked.set()
                    await session.rollback()
                    results["a"] = ("conflict", str(exc))

    async def ship_task_b() -> None:
        await a_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.ship_order(session, uuid.UUID(order_b["id"]))
                    await session.commit()
                    results["b"] = "ok"
                except ConflictError as exc:
                    await session.rollback()
                    results["b"] = ("conflict", str(exc))

    task_a = asyncio.create_task(ship_task_a())
    await a_locked.wait()
    task_b = asyncio.create_task(ship_task_b())

    # Give task B every chance to (wrongly) race past task A's held
    # `stock_summary` row lock before task A ever commits.
    await asyncio.sleep(0.3)
    assert not task_b.done(), (
        "task B's ship_order (stock_summary FOR UPDATE, via "
        "inventory.service.get_or_lock_summary) did not block on task A's held lock"
    )

    release_a.set()
    await asyncio.gather(task_a, task_b)

    outcomes = [results["a"], results["b"]]
    winners = [r for r in outcomes if r == "ok"]
    losers = [r for r in outcomes if r != "ok"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1

    # Stock never goes negative, and ends at exactly 0 (the one winner
    # consumed the only unit).
    assert await _on_hand(client, company_id, product_id) == Decimal("0")


# ---------------------------------------------------------------------------
# Concurrency: ship vs. ship on the same order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_ship_vs_ship_same_order_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Diff-review test-coverage fix: `test_concurrent_ship_for_scarce_stock_
    only_one_wins` above races two *different* orders for the same scarce
    product; it never proves the order-row `SELECT ... FOR UPDATE` lock
    itself serializes two *simultaneous* ship attempts on the *same* order,
    which is what ADR-007's own lifecycle section promises ("concurrent
    ship-vs-ship or ship-vs-cancel serialize on the row lock"). This drives
    that exact race directly, mirroring
    `test_concurrent_ship_vs_cancel_only_one_wins`'s structure.
    """
    company_id, product_id, order = await _setup_shippable(client, "SHIPR4", initial_stock="10")
    order_id = uuid.UUID(order["id"])

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    results: dict[str, object] = {}

    async def ship_task(name: str) -> None:
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.ship_order(session, order_id)
                    first_locked.set()
                    await release_first.wait()
                    await session.commit()
                    results[name] = "ok"
                except ConflictError as exc:
                    first_locked.set()
                    await session.rollback()
                    results[name] = ("conflict", str(exc))

    async def second_ship_task(name: str) -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.ship_order(session, order_id)
                    await session.commit()
                    results[name] = "ok"
                except ConflictError as exc:
                    await session.rollback()
                    results[name] = ("conflict", str(exc))

    task_a = asyncio.create_task(ship_task("a"))
    await first_locked.wait()
    task_b = asyncio.create_task(second_ship_task("b"))

    await asyncio.sleep(0.3)
    assert (
        not task_b.done()
    ), "task B's ship_order (SalesOrder FOR UPDATE) did not block on task A's held lock"

    release_first.set()
    await asyncio.gather(task_a, task_b)

    outcomes = [results["a"], results["b"]]
    winners = [r for r in outcomes if r == "ok"]
    losers = [r for r in outcomes if r != "ok"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1
    assert isinstance(losers[0], tuple) and losers[0][0] == "conflict"

    # Stock deducted exactly once (qty=2, _setup_shippable's default).
    assert await _on_hand(client, company_id, product_id) == Decimal("8")


# ---------------------------------------------------------------------------
# Concurrency: ship vs. cancel on the same order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_ship_vs_cancel_only_one_wins(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id, _product_id, order = await _setup_shippable(client, "SHIPR2", initial_stock="10")
    order_id = uuid.UUID(order["id"])

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    ship_locked = asyncio.Event()
    release_ship = asyncio.Event()
    results: dict[str, object] = {}

    async def ship_task() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.ship_order(session, order_id)
                    ship_locked.set()
                    await release_ship.wait()
                    await session.commit()
                    results["ship"] = "ok"
                except ConflictError as exc:
                    ship_locked.set()
                    await session.rollback()
                    results["ship"] = ("conflict", str(exc))

    async def cancel_task() -> None:
        await ship_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                try:
                    await sales_service.cancel_order(session, order_id)
                    results["cancel"] = "ok"
                except ConflictError as exc:
                    await session.rollback()
                    results["cancel"] = ("conflict", str(exc))

    t_ship = asyncio.create_task(ship_task())
    await ship_locked.wait()
    t_cancel = asyncio.create_task(cancel_task())

    await asyncio.sleep(0.3)
    assert (
        not t_cancel.done()
    ), "cancel_order's SELECT ... FOR UPDATE did not block on ship_order's held lock"

    release_ship.set()
    await asyncio.gather(t_ship, t_cancel)

    outcomes = {results["ship"], results["cancel"]}
    assert "ok" in outcomes
    non_ok = [v for v in (results["ship"], results["cancel"]) if v != "ok"]
    assert len(non_ok) == 1, f"expected exactly one winner, got {results}"


# ---------------------------------------------------------------------------
# Replay: duplicate delivery must not double-deduct stock or double-post
# ---------------------------------------------------------------------------


def _goods_shipped_payload(
    company_id: uuid.UUID,
    source_id: uuid.UUID,
    order_no: str,
    product_id: uuid.UUID,
    qty: str,
    unit_cost: str,
) -> dict[str, object]:
    total_cost = str(Decimal(qty) * Decimal(unit_cost))
    return {
        "company_id": str(company_id),
        "source_id": str(source_id),
        "order_no": order_no,
        "lines": [{"product_id": str(product_id), "qty": qty, "unit_cost": unit_cost}],
        "total_cost": total_cost,
        "shipped_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
    }


@pytest.mark.asyncio
async def test_replay_of_duplicate_goods_shipped_rows_deducts_stock_once(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id, product_id = await _setup_company_with_product(
        client, "SHIPR3", standard_cost="4", list_price="10", stock="20"
    )

    source_id = uuid.uuid4()
    payload = _goods_shipped_payload(company_id, source_id, "SO-REPLAY-1", product_id, "3", "4")

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        for _ in range(2):
            session.add(OutboxEvent(event_type=posting.GOODS_SHIPPED_EVENT_TYPE, payload=payload))
        await session.commit()

    summary_1 = await replay_outbox.replay_outbox()
    summary_2 = await replay_outbox.replay_outbox()

    assert summary_1.failed == 0
    assert summary_2.total == 0

    assert await _on_hand(client, company_id, product_id) == Decimal("17")  # 20 - 3, only once

    async with session_factory() as session:
        with company_context(company_id):
            entries = await ledger_service.list_journal_entries(session)
    matching = [e for e in entries if e.source_id == source_id]
    assert len(matching) == 1
