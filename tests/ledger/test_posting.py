"""tests/ledger/test_posting.py — the posting engine (ADR-003) + replay (ADR-004 R1).

**Week 5 migration note (ADR-007 "Synthetic event retirement")**: every test
in this file used to drive `posting.SYNTHETIC_SALE_EVENT_TYPE`, a Week 3
self-validation event with no real business meaning. That event_type,
its schema, and its posting rule are deleted this week — this file is
migrated (not merely renamed) to drive the real `sales.goods_shipped` event
instead, preserving every Week 3 case's *logic* (rule lookup failure,
account resolution failure, duplicate-delivery idempotency, replay without
ambient tenancy context) against the new event shape and rule (debit `5000
COGS` / credit `1300 Inventory`).

One structural consequence of the migration: `sales.goods_shipped` now has
**two** subscribers (`inventory.service.handle_goods_shipped`, subscribed
before this module's posting handler — ADR-007 Decision 1), where the old
synthetic event had exactly one. Every test below that dispatches through
the real bus (`events.publish`/`redispatch`, as opposed to calling
`posting.handle_posting_event` directly) therefore sets up a real product
with sufficient stock first (`_setup_company_with_accounts`), so
inventory's handler is a quiet, successful participant and the test can
still isolate the *posting*-specific behavior under assertion. This is not
scope creep — it is what "the event this rule reacts to is now real"
necessarily implies once the bus's own two-subscriber wiring is in play.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import replay_outbox
from app.core import events
from app.core.tenancy import company_context, current_company_id_or_none
from app.modules.ledger import posting, service
from app.modules.masterdata.models import OutboxEvent
from tests.inventory._helpers import create_adjustment, create_product, on_hand
from tests.ledger._helpers import create_account, create_company, create_period


async def _setup_company_with_accounts(
    client: AsyncClient, code: str, *, with_accounts: bool = True
) -> uuid.UUID:
    company_id = await create_company(client, code)
    await create_period(client, company_id, 2026, 1)
    if with_accounts:
        await create_account(client, company_id, "5000", "COGS", "expense")
        await create_account(client, company_id, "1300", "Inventory", "asset")
    return company_id


async def _setup_shippable_company(
    client: AsyncClient, code: str, *, with_accounts: bool = True, stock: str = "1000"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Company (+ optional 5000/1300 accounts) and a real product with ample
    stock — the standard fixture for tests that dispatch `sales.goods_shipped`
    through the real bus, so `inventory`'s subscriber (which also fires) is
    a quiet, successful participant rather than an incidental source of
    failure. See module docstring.
    """
    company_id = await _setup_company_with_accounts(client, code, with_accounts=with_accounts)
    product_id = await create_product(client, company_id, f"{code}-SKU", standard_cost="5")
    adjust_resp = await create_adjustment(client, company_id, product_id, stock, "seed stock")
    assert adjust_resp.status_code == 201, adjust_resp.text
    return company_id, product_id


def _goods_shipped_payload(
    company_id: uuid.UUID,
    source_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    qty: str = "1",
    unit_cost: str = "5",
    order_no: str = "SO-TEST-0001",
) -> dict[str, object]:
    total_cost = str(Decimal(qty) * Decimal(unit_cost))
    return {
        "company_id": str(company_id),
        "source_id": str(source_id),
        "order_no": order_no,
        "lines": [{"product_id": str(product_id), "qty": qty, "unit_cost": unit_cost}],
        "total_cost": total_cost,
        "shipped_at": "2026-01-15T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_goods_shipped_event_posts_balanced_entry(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    company_id, product_id = await _setup_shippable_company(client, "POST1")
    source_id = uuid.uuid4()

    with company_context(company_id):
        await events.publish(
            db_session,
            posting.GOODS_SHIPPED_EVENT_TYPE,
            _goods_shipped_payload(company_id, source_id, product_id, qty="3", unit_cost="5"),
        )
        await db_session.commit()

        entries = await service.list_journal_entries(db_session)

    matching = [e for e in entries if e.source_id == source_id]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.source_type == posting.GOODS_SHIPPED_EVENT_TYPE
    assert len(entry.lines) == 2

    total_debit = sum((line.debit for line in entry.lines), Decimal("0"))
    total_credit = sum((line.credit for line in entry.lines), Decimal("0"))
    assert total_debit == total_credit == Decimal("15.000000")

    # inventory's subscriber (which ran first) also did its job.
    assert await on_hand(client, company_id, product_id) == Decimal("997")


@pytest.mark.asyncio
async def test_missing_posting_rule_aborts(db_session: AsyncSession, client: AsyncClient) -> None:
    company_id = await _setup_company_with_accounts(client, "POST2")
    payload = posting.GoodsShippedPayload(
        company_id=company_id,
        source_id=uuid.uuid4(),
        order_no="SO-TEST-0002",
        lines=[],
        total_cost=Decimal("10"),
        shipped_at="2026-01-15T12:00:00+00:00",
    )

    with company_context(company_id):
        with pytest.raises(posting.PostingRuleNotFoundError):
            await posting.handle_posting_event(db_session, "no.such.event.type", payload)

        entries = await service.list_journal_entries(db_session)
    assert entries == []


@pytest.mark.asyncio
async def test_missing_account_code_aborts_without_partial_write(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # No accounts created for this company at all — resolution must fail.
    company_id, product_id = await _setup_shippable_company(client, "POST3", with_accounts=False)
    source_id = uuid.uuid4()

    with company_context(company_id):
        with pytest.raises(posting.AccountResolutionError):
            await events.publish(
                db_session,
                posting.GOODS_SHIPPED_EVENT_TYPE,
                _goods_shipped_payload(company_id, source_id, product_id, unit_cost="7"),
            )
        await db_session.rollback()

        entries = await service.list_journal_entries(db_session)
    assert entries == []
    # The whole transaction rolled back — inventory's earlier (uncommitted)
    # deduction rolled back with it, not just the failed journal-entry half.
    assert await on_hand(client, company_id, product_id) == Decimal("1000")


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    company_id, product_id = await _setup_shippable_company(client, "POST4")
    source_id = uuid.uuid4()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    for _ in range(2):
        async with session_factory() as session:
            with company_context(company_id):
                # Must not raise on either delivery — the second is a
                # no-op skip, not an error (ADR-003 R2), for BOTH
                # subscribers (inventory's `uq_stock_moves_source` and
                # ledger's `uq_journal_entries_source` alike).
                await events.publish(
                    session,
                    posting.GOODS_SHIPPED_EVENT_TYPE,
                    _goods_shipped_payload(company_id, source_id, product_id, qty="2"),
                )
            await session.commit()

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
    matching = [e for e in entries if e.source_id == source_id]
    assert len(matching) == 1
    assert await on_hand(client, company_id, product_id) == Decimal("998")  # deducted once


@pytest.mark.asyncio
async def test_replay_deduplicates_and_marks_dispatched(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    company_id, product_id = await _setup_shippable_company(client, "POST5")
    source_id = uuid.uuid4()
    payload = _goods_shipped_payload(company_id, source_id, product_id, qty="4")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Simulate two at-least-once duplicate outbox rows for the same event.
    async with session_factory() as session:
        for _ in range(2):
            session.add(OutboxEvent(event_type=posting.GOODS_SHIPPED_EVENT_TYPE, payload=payload))
        await session.commit()

    summary_1 = await replay_outbox.replay_outbox()
    summary_2 = await replay_outbox.replay_outbox()

    assert summary_1.failed == 0
    # Diff-review fix: this used to be the weaker `succeeded + skipped == 2`
    # — which can't distinguish "classified both rows correctly" from "got
    # the split backwards but the total still adds up" and would not have
    # caught `_count_effects`'s predecessor misclassifying real work as
    # skipped (see `test_replay_of_zero_cost_shipment_reports_succeeded_
    # not_skipped` below for the case that heuristic actually got wrong).
    # Non-zero `unit_cost` here means the first of the two duplicate outbox
    # rows does real work on both `stock_moves` and `journal_entries`; the
    # second is a genuine idempotent no-op on both.
    assert summary_1.succeeded == 1
    assert summary_1.skipped == 1
    assert summary_2.total == 0  # nothing left un-dispatched

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
        matching_entries = [e for e in entries if e.source_id == source_id]
        assert len(matching_entries) == 1

        outbox_result = await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == posting.GOODS_SHIPPED_EVENT_TYPE,
                OutboxEvent.payload["source_id"].astext == str(source_id),
            )
        )
        outbox_rows = outbox_result.scalars().all()
        assert len(outbox_rows) == 2
        assert all(row.dispatched_at is not None for row in outbox_rows)


@pytest.mark.asyncio
async def test_replay_of_zero_cost_shipment_reports_succeeded_not_skipped(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    """Diff-review regression: `sales.goods_shipped` has two subscribers
    (inventory, then posting — ADR-007 Decision 1), and ADR-007 Decision 3's
    zero-cost skip means a genuinely successful dispatch can move stock
    *without* ever posting a journal entry (every line's `standard_cost` is
    0). Before this fix, `app.cli.replay_outbox`'s success/skip heuristic
    counted `journal_entries` alone, so this exact case — stock deducted,
    outbox row correctly marked dispatched, but zero new journal entries —
    was misclassified as `skipped`. It must report `succeeded`.
    """
    company_id, product_id = await _setup_shippable_company(client, "POST7")
    source_id = uuid.uuid4()
    payload = _goods_shipped_payload(company_id, source_id, product_id, qty="3", unit_cost="0")

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(OutboxEvent(event_type=posting.GOODS_SHIPPED_EVENT_TYPE, payload=payload))
        await session.commit()

    summary = await replay_outbox.replay_outbox()

    assert summary.failed == 0
    assert summary.succeeded == 1, f"expected succeeded=1 (real work happened), got {summary}"
    assert summary.skipped == 0

    # The effect that actually happened: stock moved.
    assert await on_hand(client, company_id, product_id) == Decimal("997")  # 1000 - 3

    # The effect that correctly did NOT happen (ADR-007 Decision 3): no
    # journal entry for an all-zero-cost shipment.
    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
        assert [e for e in entries if e.source_id == source_id] == []


@pytest.mark.asyncio
async def test_replay_works_without_ambient_tenancy_context(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    """The whole point of replay: it must work with zero HTTP request in sight.

    If this test fails, the replay CLI is unusable outside a request context
    — see ADR-004 R2/R3.
    """
    company_id, product_id = await _setup_shippable_company(client, "POST6")
    source_id = uuid.uuid4()
    payload = _goods_shipped_payload(company_id, source_id, product_id, qty="1")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(OutboxEvent(event_type=posting.GOODS_SHIPPED_EVENT_TYPE, payload=payload))
        await session.commit()

    # No `with company_context(...)` anywhere around this call — proves
    # replay binds its own context per row instead of relying on ambient
    # request state.
    assert current_company_id_or_none() is None
    summary = await replay_outbox.replay_outbox()
    assert current_company_id_or_none() is None  # context is unbound again afterwards

    assert summary.failed == 0
    assert summary.succeeded == 1

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
    matching_entries = [e for e in entries if e.source_id == source_id]
    assert len(matching_entries) == 1


@pytest.mark.asyncio
async def test_replay_poison_row_does_not_block_later_rows(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    """Diff-review regression (ADR-004 R3, poison-row isolation):
    `replay_outbox`'s `dict(row.payload)` conversion used to run outside the
    per-row try/except, so a malformed payload (here, a JSON scalar instead
    of an object — simulating a corrupted/legacy row) raised unhandled and
    aborted the whole call, taking every row after it down too. One bad row
    must fail only itself; a good row ordered after it must still process.
    """
    company_id, product_id = await _setup_shippable_company(client, "POST8")
    good_source_id = uuid.uuid4()
    good_payload = _goods_shipped_payload(company_id, good_source_id, product_id, qty="1")

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        # Inserted via raw SQL, not the ORM: `OutboxEvent.payload`'s
        # `dict[str, object]` type hint would reject a non-dict value at
        # the Python layer, but nothing stops a non-dict JSON value (or a
        # pre-existing corrupted row from before stricter validation
        # existed) from being present at the DB layer, which is exactly
        # the case this test exercises. `occurred_at` is backdated so this
        # row is replayed *before* the good one (replay orders by
        # `occurred_at`).
        await session.execute(
            text(
                "INSERT INTO outbox "
                "(id, event_type, payload, occurred_at, dispatched_at, attempts) "
                "VALUES (:id, :event_type, CAST(:payload AS jsonb), "
                "now() - interval '1 minute', NULL, 0)"
            ),
            {
                "id": str(uuid.uuid4()),
                "event_type": posting.GOODS_SHIPPED_EVENT_TYPE,
                "payload": json.dumps("not-a-dict"),
            },
        )
        session.add(OutboxEvent(event_type=posting.GOODS_SHIPPED_EVENT_TYPE, payload=good_payload))
        await session.commit()

    summary = await replay_outbox.replay_outbox()

    assert summary.failed == 1, f"expected exactly the poison row to fail, got {summary}"
    assert summary.succeeded == 1, f"the good row after it must still be processed, got {summary}"

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
    matching_entries = [e for e in entries if e.source_id == good_source_id]
    assert len(matching_entries) == 1


@pytest.mark.asyncio
async def test_zero_cost_event_skips_journal_entry(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """ADR-007 Decision 3: a shipment whose lines all cost 0 posts no entry."""
    company_id, product_id = await _setup_shippable_company(client, "POST7")
    source_id = uuid.uuid4()

    with company_context(company_id):
        await events.publish(
            db_session,
            posting.GOODS_SHIPPED_EVENT_TYPE,
            _goods_shipped_payload(company_id, source_id, product_id, qty="2", unit_cost="0"),
        )
        await db_session.commit()

        entries = await service.list_journal_entries(db_session)

    assert [e for e in entries if e.source_id == source_id] == []
    # Stock still moved even though nothing posted.
    assert await on_hand(client, company_id, product_id) == Decimal("998")
