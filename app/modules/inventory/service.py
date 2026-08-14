"""inventory service layer — business logic + transaction boundary (ADR-007).

Routers stay thin, same convention as masterdata/ledger/sales. Every write
stamps `company_id` from `app.core.tenancy.require_current_company_id()` (or
takes it as an explicit parameter from a caller that already resolved it —
see `apply_stock_move` below), never from client input.

Independence-boundary note (import-linter): this module must never import
`app.modules.sales` or `app.modules.masterdata`. `create_adjustment`
resolves `product_id` via a lightweight `sqlalchemy.table()` Core reference
to the physical `products` table (same pattern `sales.service`/
`ledger.service`/`ledger.posting` already use for `accounts`/`products`;
see those modules' docstrings for the full rationale) rather than importing
`masterdata.models.Product` — a DB foreign key alone proves the product
exists, not that it belongs to the requesting company, which is why this
explicit `company_id`-scoped lookup matters. `handle_goods_shipped` (the bus
subscriber for `sales.goods_shipped`) needs no such lookup: the event
payload's `product_id`s were already validated as belonging to this company
by `sales.service.ship_order` before the event was ever published, so
trusting them here is not a boundary violation — it is exactly the same
trust `ledger.posting.handle_posting_event` places in a payload's resolved
`amount`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import column as sa_column
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, DomainValidationError
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import require_current_company_id
from app.modules.inventory.models import StockMove, StockMoveType, StockSummary
from app.modules.inventory.schemas import StockAdjustmentCreate

logger = logging.getLogger(__name__)

# `sales.goods_shipped` is owned/published by `sales` (the event_type string
# constant lives in `app.modules.sales.events.SALES_GOODS_SHIPPED_EVENT_TYPE`)
# and its payload schema is registered from `app.modules.ledger.posting`
# (see that module's docstring for why) — this module deliberately does not
# import either symbol (independence boundary): the bus contract is the
# `event_type` *string* + a `BaseModel` payload accessed via
# `.model_dump()`, never a shared Python type. The literal below must stay
# byte-for-byte identical to `sales.events.SALES_GOODS_SHIPPED_EVENT_TYPE`;
# it only needs to match consistently across replays of the *same* stored
# `stock_moves.source_type` value, which is what makes duplicate delivery
# detectable via `uq_stock_moves_source`.
_GOODS_SHIPPED_SOURCE_TYPE = "sales.goods_shipped"

_PRODUCTS = sa_table(
    "products",
    sa_column("id"),
    sa_column("company_id"),
)


class InsufficientStockError(ConflictError):
    """Raised when a stock deduction would take `on_hand` below zero (ADR-007
    Decision 2, HTTP 409).
    """

    def __init__(self, product_id: uuid.UUID, *, on_hand: Decimal, qty_delta: Decimal) -> None:
        self.product_id = product_id
        self.on_hand = on_hand
        self.qty_delta = qty_delta
        super().__init__(
            f"insufficient stock for product {product_id}: on_hand={on_hand}, requested "
            f"qty_delta={qty_delta} would result in on_hand={on_hand + qty_delta} (< 0)"
        )


def _is_source_conflict(exc: IntegrityError) -> bool:
    """True iff `exc` is the `uq_stock_moves_source` unique-index conflict.

    Mirrors `ledger.posting._is_source_conflict` exactly — same reasoning:
    any other `IntegrityError` must propagate, only this one specific,
    expected "duplicate delivery" conflict is a no-op (ADR-007 Decision 4 /
    ADR-003 R2).
    """
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name:
        return str(constraint_name) == "uq_stock_moves_source"
    return "uq_stock_moves_source" in str(exc)


async def _commit_or_conflict(session: AsyncSession, *, entity: str) -> None:
    """Same choke point as every other module's equivalent."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{entity} violates a uniqueness or reference constraint") from exc


async def _ensure_product_belongs_to_company(
    session: AsyncSession, company_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    result = await session.execute(
        select(_PRODUCTS.c.id).where(
            _PRODUCTS.c.id == product_id, _PRODUCTS.c.company_id == company_id
        )
    )
    if result.first() is None:
        raise DomainValidationError(
            f"product_id {product_id} not found or does not belong to this company"
        )


# ---------------------------------------------------------------------------
# Core stock-mutation primitive (ADR-007 Decision 2)
# ---------------------------------------------------------------------------


async def get_or_lock_summary(
    session: AsyncSession, company_id: uuid.UUID, product_id: uuid.UUID
) -> StockSummary:
    """Get-or-create + lock the `(company_id, product_id)` summary row.

    Same race-free "get-or-create, then lock" pattern as
    `ledger.service._allocate_entry_no` / `sales.service._allocate_order_no`:
    `INSERT ... ON CONFLICT DO NOTHING` followed by `SELECT ... FOR UPDATE`.
    Two concurrent callers deducting stock for the same product cannot both
    read the same `on_hand` and both pass the sufficiency check below — the
    second blocks on this row's lock until the first's transaction ends,
    and by the time it proceeds it observes whatever `on_hand` the first
    left behind (committed) or the pre-attempt value (rolled back).

    Also takes the SHARED per-company advisory lock (`app.core.locking`)
    before touching the row — see that module's docstring for why a
    row-level lock alone doesn't protect a row that doesn't exist yet, and
    how this pairs with `rebuild_stock_summary`'s EXCLUSIVE acquisition of
    the same lock.
    """
    await acquire_company_advisory_lock(session, company_id, exclusive=False)
    await session.execute(
        pg_insert(StockSummary)
        .values(company_id=company_id, product_id=product_id, on_hand=Decimal("0"))
        .on_conflict_do_nothing(index_elements=["company_id", "product_id"])
    )
    result = await session.execute(
        select(StockSummary)
        .where(StockSummary.company_id == company_id, StockSummary.product_id == product_id)
        .with_for_update()
    )
    return result.scalar_one()


async def apply_stock_move(
    session: AsyncSession,
    company_id: uuid.UUID,
    product_id: uuid.UUID,
    qty_delta: Decimal,
    move_type: StockMoveType,
    *,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> StockMove:
    """Lock summary -> check sufficiency -> insert move -> update summary -> flush.

    Flush-only, **never commits** — ADR-003 R1's "the transaction belongs to
    whoever opened it" doctrine, applied here exactly as it is to
    `ledger.service.post_journal_entry`: this is the core primitive both
    `create_adjustment` (which commits, HTTP-facing) and
    `handle_goods_shipped` (which wraps a whole shipment's lines in one
    SAVEPOINT it owns) call, and neither delegates its commit/rollback
    decision to this function.

    Raises `InsufficientStockError` (409) if `on_hand + qty_delta < 0` —
    checked *after* the row lock is held, so a concurrent deduction cannot
    slip in between the check and the write and leave `on_hand` negative
    (the `stock_summary.on_hand >= 0` CHECK constraint is the DB-layer
    backstop for the same invariant, defense in depth against any writer
    that bypasses this function).
    """
    summary = await get_or_lock_summary(session, company_id, product_id)
    new_on_hand = summary.on_hand + qty_delta
    if new_on_hand < 0:
        raise InsufficientStockError(product_id, on_hand=summary.on_hand, qty_delta=qty_delta)

    move = StockMove(
        company_id=company_id,
        product_id=product_id,
        qty_delta=qty_delta,
        move_type=move_type,
        source_type=source_type,
        source_id=source_id,
        reason=reason,
    )
    session.add(move)
    summary.on_hand = new_on_hand
    await session.flush()
    return move


# ---------------------------------------------------------------------------
# Manual intake (ADR-007 Decision 2 — Phase 1 stand-in for purchase receipts)
# ---------------------------------------------------------------------------


async def create_adjustment(session: AsyncSession, data: StockAdjustmentCreate) -> StockMove:
    """HTTP-facing: the only committing path in this module (same choke-point
    convention as `masterdata.service`/`ledger.service`). `source_type`/
    `source_id` stay `NULL` — manual, user-initiated, no replay path, no
    dedup needed, excluded from `uq_stock_moves_source` by its partial
    `WHERE source_type IS NOT NULL` clause, exactly as manual journal
    entries are excluded from `uq_journal_entries_source`.
    """
    company_id = require_current_company_id()
    await _ensure_product_belongs_to_company(session, company_id, data.product_id)
    move = await apply_stock_move(
        session,
        company_id,
        data.product_id,
        data.qty_delta,
        StockMoveType.ADJUSTMENT,
        reason=data.reason,
    )
    await _commit_or_conflict(session, entity="StockMove")
    return move


# ---------------------------------------------------------------------------
# Bus subscriber for `sales.goods_shipped` (ADR-007 Decision 1 / Decision 4)
# ---------------------------------------------------------------------------


async def handle_goods_shipped(session: AsyncSession, payload: BaseModel) -> None:
    """Deduct stock for every line of a shipped order, as one atomic batch.

    Subscribed onto `sales.goods_shipped` *before* ledger's posting handler
    (ADR-007 Decision 1, wired in `app.main`: "move the goods, then account
    for them") — both run inside the same transaction `sales.service.ship_order`
    opened, so ordering here only affects error attribution, not atomicity:
    either both handlers succeed and the whole `ship` commits, or the first
    one to fail aborts everything (the order stays `confirmed`, nothing
    moves, nothing posts).

    **Aggregate by `product_id` before touching stock (diff-review fix).**
    Nothing in sales' schema or the DB forbids an order carrying the same
    `product_id` on more than one line. `uq_stock_moves_source` is scoped
    to one row per `(company_id, source_type, source_id, product_id)` — one
    row per *product* per shipment, not one row per *line*. Inserting a
    `stock_moves` row per line would make a repeated product's second line
    collide with its first on that index, and the collision handler below
    (correctly, for genuine replays) treats *any* collision as "this whole
    shipment was already deducted, skip" — misdiagnosing a first delivery
    as a replay would then roll back the SAVEPOINT and silently skip
    deducting *any* of this shipment's stock, while ledger still posts COGS
    for the full amount and the order still ships as if nothing went wrong.
    Summing `qty` per `product_id` first guarantees exactly one insert per
    product, matching the unique index's actual grain, so a repeated
    product can never trigger this misdiagnosis.

    **Lock in a fixed, deterministic order (diff-review fix).** Iterating
    `sorted(qty_by_product)` (ascending `product_id`) rather than payload
    order means every caller — this handler and `rebuild_stock_summary`
    alike — always acquires a given company's `stock_summary` row locks in
    the same global order. Two shipments that both touch products A and B,
    in opposite line order, can no longer deadlock: both now lock A before
    B, so the second simply blocks on A instead of each holding one lock
    and waiting on the other's.

    Mirrors `ledger.posting.handle_posting_event`'s SAVEPOINT pattern
    exactly: the whole batch (every product) is wrapped in one
    `session.begin_nested()`. A conflict on `uq_stock_moves_source` means
    "this shipment's deduction already happened" (an at-least-once replay)
    — the whole savepoint rolls back and the batch is skipped as a unit,
    never partially re-applied product-by-product. Any other exception (in
    particular `InsufficientStockError`) propagates unchanged and aborts
    the caller's whole transaction (ADR-004 Decision 1, fail-closed).
    """
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    lines: Sequence[dict[str, Any]] = payload_dict["lines"]

    qty_by_product: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        product_id = uuid.UUID(str(line["product_id"]))
        qty = Decimal(str(line["qty"]))
        qty_by_product[product_id] = qty_by_product.get(product_id, Decimal("0")) + qty

    try:
        async with session.begin_nested():
            for product_id in sorted(qty_by_product):
                qty = qty_by_product[product_id]
                await apply_stock_move(
                    session,
                    company_id,
                    product_id,
                    -qty,
                    StockMoveType.SHIPMENT,
                    source_type=_GOODS_SHIPPED_SOURCE_TYPE,
                    source_id=source_id,
                )
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "sales.goods_shipped source_id=%s already deducted for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def list_stock_summary(
    session: AsyncSession, *, product_id: uuid.UUID | None = None
) -> Sequence[StockSummary]:
    """Explicit `company_id` filter — see module/model docstrings for why
    `StockSummary` cannot rely on the automatic tenant-scoping ORM hook.
    """
    company_id = require_current_company_id()
    stmt = select(StockSummary).where(StockSummary.company_id == company_id)
    if product_id is not None:
        stmt = stmt.where(StockSummary.product_id == product_id)
    result = await session.execute(stmt.order_by(StockSummary.product_id))
    return result.scalars().all()


async def list_stock_moves(
    session: AsyncSession, *, product_id: uuid.UUID | None = None
) -> Sequence[StockMove]:
    """`StockMove` inherits `TenantScopedMixin`, so the automatic
    `do_orm_execute` hook already restricts this to the active company —
    no explicit `company_id` predicate needed (unlike `list_stock_summary`).
    """
    stmt = select(StockMove).order_by(StockMove.created_at)
    if product_id is not None:
        stmt = stmt.where(StockMove.product_id == product_id)
    result = await session.execute(stmt)
    return result.scalars().all()
