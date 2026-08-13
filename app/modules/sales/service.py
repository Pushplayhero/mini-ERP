"""sales service layer — business logic + transaction boundary (ADR-006).

Routers stay thin, same convention as masterdata/ledger. Every write to a
tenant-scoped table stamps `company_id` from
`app.core.tenancy.require_current_company_id()`, never from the DTO.

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`. Two places would ordinarily want to — resolving
`customer_id`/`product_id` references and reading current
name/code/list_price/uom for snapshots and pricing — so both use a
lightweight `sqlalchemy.table()`/`column()` reference to the physical
`customers`/`products` tables instead of importing
`masterdata.models.Customer`/`Product`. This is the exact Core-level
reference pattern `ledger.service`/`ledger.posting` already use for
`accounts` (see those modules' docstrings for the full rationale); a DB
foreign key alone would prove the row exists, not that it belongs to the
requesting company, which is why the explicit `company_id` predicate below
matters.

**Quoted-price freezing (ADR-006 P3, resolved here)**: `unit_price` is
resolved — client override, or `product.list_price` if omitted — at the
moment a line is *written* (`create_order` or `update_order`), not deferred
to `confirm_order`. A draft that sits untouched for a week and is then
confirmed keeps whatever `unit_price` its lines already carry; re-editing a
draft (PATCH, full line replacement) re-resolves any line that still omits
`unit_price` against *current* `product.list_price`, which is how "a draft
confirms at current prices unless lines carried manual overrides" (ADR-006
Decision 3) actually happens in this implementation — through re-editing,
not through `confirm` silently repricing anything. `confirm_order` never
touches `unit_price`/`amount`/`total`; it only re-freezes the
name/code/sku *snapshot* fields (see that function's docstring).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import column as sa_column
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import events, hooks
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.hooks import HookContext
from app.core.tenancy import require_current_company_id
from app.modules.sales.events import SALES_ORDER_CONFIRMED_EVENT_TYPE
from app.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus, SalesSequence
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate, SalesOrderUpdate

# One hook point this week (ADR-006 Decision 1): owned here, not in
# `app.core.hooks`, mirroring how `ledger.posting` (not `app.core.events`)
# owns `SYNTHETIC_SALE_EVENT_TYPE` — the hook-point name belongs to the
# module that fires it, the registry module stays generic.
SALES_ORDER_VALIDATE_CONFIRM = "sales.order.validate_confirm"

_CUSTOMERS = sa_table(
    "customers",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("code"),
    sa_column("name"),
    sa_column("currency_code"),
)

_PRODUCTS = sa_table(
    "products",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("sku"),
    sa_column("name"),
    sa_column("list_price"),
    sa_column("uom_id"),
)


async def _commit_or_conflict(session: AsyncSession, *, entity: str) -> None:
    """Same choke point as `masterdata.service`/`ledger.service`'s equivalents."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{entity} violates a uniqueness or reference constraint") from exc


# ---------------------------------------------------------------------------
# Customer / product resolution (Core-level references — see module docstring)
# ---------------------------------------------------------------------------


async def _fetch_customer(
    session: AsyncSession, company_id: uuid.UUID, customer_id: uuid.UUID
) -> Any:
    result = await session.execute(
        select(
            _CUSTOMERS.c.id, _CUSTOMERS.c.code, _CUSTOMERS.c.name, _CUSTOMERS.c.currency_code
        ).where(_CUSTOMERS.c.id == customer_id, _CUSTOMERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"customer_id {customer_id} not found or does not belong to this company"
        )
    return row


async def _fetch_products(
    session: AsyncSession, company_id: uuid.UUID, product_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not product_ids:
        return {}
    result = await session.execute(
        select(
            _PRODUCTS.c.id,
            _PRODUCTS.c.sku,
            _PRODUCTS.c.name,
            _PRODUCTS.c.list_price,
            _PRODUCTS.c.uom_id,
        ).where(_PRODUCTS.c.id.in_(product_ids), _PRODUCTS.c.company_id == company_id)
    )
    found: dict[uuid.UUID, Any] = {row.id: row for row in result.all()}
    missing = product_ids - found.keys()
    if missing:
        missing_str = ", ".join(str(p) for p in sorted(missing, key=str))
        raise DomainValidationError(
            f"product_id(s) not found or do not belong to this company: {missing_str}"
        )
    return found


def _build_lines(
    company_id: uuid.UUID,
    lines_in: Sequence[SalesOrderLineCreate],
    products: dict[uuid.UUID, Any],
) -> tuple[list[SalesOrderLine], Decimal]:
    built: list[SalesOrderLine] = []
    total = Decimal("0")
    for idx, line_in in enumerate(lines_in, start=1):
        product = products[line_in.product_id]
        unit_price = line_in.unit_price if line_in.unit_price is not None else product.list_price
        uom_id = line_in.uom_id if line_in.uom_id is not None else product.uom_id
        amount = line_in.qty * unit_price
        built.append(
            SalesOrderLine(
                company_id=company_id,
                line_no=idx,
                product_id=line_in.product_id,
                qty=line_in.qty,
                uom_id=uom_id,
                unit_price=unit_price,
                snapshot_sku=product.sku,
                snapshot_product_name=product.name,
                amount=amount,
            )
        )
        total += amount
    return built, total


# ---------------------------------------------------------------------------
# order_no allocation (ADR-006 Decision 3 — same locking pattern as ledger's
# gapless numbering, but gaplessness is explicitly NOT required for orders)
# ---------------------------------------------------------------------------


async def _allocate_order_no(session: AsyncSession, company_id: uuid.UUID, year: int) -> str:
    """`(company_id, year) -> next_no` allocation.

    Same race-free "get-or-create, then lock" pattern as
    `ledger.service._allocate_entry_no` (`INSERT ... ON CONFLICT DO NOTHING`
    followed by `SELECT ... FOR UPDATE`) — see that function's docstring for
    the full concurrency argument. The one difference: ADR-006 Decision 3
    explicitly does NOT require `order_no` to be gapless the way `entry_no`
    is (ADR-005 R2) — a cancelled order, or a rolled-back confirm attempt
    that never got this far, simply leaves a gap, and that is documented as
    acceptable. Reusing the exact same locking pattern anyway (rather than a
    weaker one) costs nothing and keeps one mental model for "how does this
    codebase hand out sequential numbers" across both modules.
    """
    await session.execute(
        pg_insert(SalesSequence)
        .values(company_id=company_id, year=year, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )
    result = await session.execute(
        select(SalesSequence)
        .where(SalesSequence.company_id == company_id, SalesSequence.year == year)
        .with_for_update()
    )
    seq = result.scalar_one()
    allocated_no = seq.next_no
    seq.next_no = allocated_no + 1
    return f"SO-{year:04d}-{allocated_no:06d}"


# ---------------------------------------------------------------------------
# CRUD (draft)
# ---------------------------------------------------------------------------


async def create_order(session: AsyncSession, data: SalesOrderCreate) -> SalesOrder:
    company_id = require_current_company_id()
    customer = await _fetch_customer(session, company_id, data.customer_id)
    products = await _fetch_products(session, company_id, {line.product_id for line in data.lines})
    lines, total = _build_lines(company_id, data.lines, products)

    order_no = await _allocate_order_no(session, company_id, date.today().year)

    order = SalesOrder(
        company_id=company_id,
        order_no=order_no,
        customer_id=data.customer_id,
        status=SalesOrderStatus.DRAFT,
        currency_code=customer.currency_code,
        total=total,
        snapshot_customer_code=customer.code,
        snapshot_customer_name=customer.name,
        custom_data=data.custom_data,
    )
    order.lines = lines
    session.add(order)
    await _commit_or_conflict(session, entity="SalesOrder")
    # Re-select rather than `session.refresh(order, attribute_names=["lines"])`:
    # `SalesOrder.updated_at` is a server-side `onupdate=func.now()` column
    # (`TimestampAuditMixin`), and a partial `refresh()` does not repopulate
    # it — a plain re-select (same shape `get_order` already does) is the
    # simplest way to guarantee every column and the `lines` relationship
    # are both loaded and non-expired before Pydantic serializes the result.
    return await get_order(session, order.id)


async def list_orders(session: AsyncSession) -> Sequence[SalesOrder]:
    result = await session.execute(
        select(SalesOrder).options(selectinload(SalesOrder.lines)).order_by(SalesOrder.order_no)
    )
    return result.scalars().all()


async def get_order(session: AsyncSession, order_id: uuid.UUID) -> SalesOrder:
    result = await session.execute(
        select(SalesOrder).where(SalesOrder.id == order_id).options(selectinload(SalesOrder.lines))
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("SalesOrder", order_id)
    return order


async def update_order(
    session: AsyncSession, order_id: uuid.UUID, data: SalesOrderUpdate
) -> SalesOrder:
    """Draft-only. `SELECT ... FOR UPDATE` before the status check — same R1

    doctrine as `confirm_order`/`cancel_order`: a concurrent confirm cannot
    slip in between "check status" and "apply the edit" and leave a
    confirmed order silently mutated.
    """
    company_id = require_current_company_id()
    result = await session.execute(
        select(SalesOrder)
        .where(SalesOrder.id == order_id)
        .options(selectinload(SalesOrder.lines))
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("SalesOrder", order_id)
    if order.status is not SalesOrderStatus.DRAFT:
        raise ConflictError(
            f"SalesOrder {order_id} is not a draft (status={order.status.value}); "
            "only draft orders can be edited"
        )

    # `.options(selectinload(SalesOrder.lines))` above is required, not
    # cosmetic: replacing `order.lines` below (cascade="all, delete-orphan")
    # makes SQLAlchemy diff the new collection against the *current* one to
    # decide what to delete, which means it must load the current collection
    # first — without eager-loading it here, that load would be a lazy
    # (implicit IO) load attempted outside of an async-safe context.
    if data.lines is not None:
        products = await _fetch_products(
            session, company_id, {line.product_id for line in data.lines}
        )
        lines, total = _build_lines(company_id, data.lines, products)
        # Two steps, not one straight `order.lines = lines`: SQLAlchemy's
        # unit of work issues INSERTs before DELETEs within a single flush,
        # so a new line reusing `line_no=1` (the common case — a draft's
        # line numbers restart at 1 every edit) would collide with the old
        # line_no=1 row's still-not-yet-deleted UNIQUE(order_id, line_no)
        # entry. Clearing (and flushing the resulting delete-orphan
        # deletes) *before* attaching the new lines avoids that collision.
        order.lines.clear()
        await session.flush()
        order.lines = lines
        order.total = total
    if data.custom_data is not None:
        order.custom_data = data.custom_data

    await _commit_or_conflict(session, entity="SalesOrder")
    return await get_order(session, order.id)


# ---------------------------------------------------------------------------
# State transitions (ADR-006 R1, R2)
# ---------------------------------------------------------------------------


async def confirm_order(session: AsyncSession, order_id: uuid.UUID) -> SalesOrder:
    """Non-committing core of order confirmation (ADR-006 Decision 3, R1, R2).

    Flow, all inside one `SELECT ... FOR UPDATE`-locked window:

    1. Lock the order row, THEN re-check `status` (R1) — locking before
       checking is what makes two concurrent confirms serialize instead of
       race: the second caller's `SELECT ... FOR UPDATE` blocks until the
       first's transaction ends, and by the time it acquires the lock it
       observes `status=confirmed` and 409s. Same doctrine as ADR-005 R4.
    2. Require at least one line and `total > 0` (R2) — before hooks run,
       so a plugin never even sees an order that cannot legally confirm.
    3. Re-freeze `snapshot_customer_*`/`snapshot_sku`/`snapshot_product_name`
       from *current* masterdata — this is the actual "freeze" moment
       (ADR-006 Decision 3); pricing (`unit_price`/`amount`/`total`) is
       deliberately NOT touched here (see module docstring).
    4. Run `hooks.SALES_ORDER_VALIDATE_CONFIRM` handlers — any exception
       propagates, aborting the whole transaction (nothing committed yet).
    5. Set `status=confirmed`, `confirmed_at=now()`.
    6. `events.publish(...)` the `sales.order_confirmed` event (outbox write
       + synchronous dispatch, still inside this same uncommitted
       transaction).
    7. `flush()` — **never commit or rollback**. ADR-003 R1's "the
       transaction belongs to whoever opened it" applies here exactly as it
       does to `ledger.service.post_journal_entry`: the HTTP route
       (`router.confirm_sales_order`) owns the commit boundary and the
       `IntegrityError` -> 409 translation for it, per ADR-006's explicit
       instruction to keep that translation out of this core function.
    """
    company_id = require_current_company_id()
    result = await session.execute(
        select(SalesOrder)
        .where(SalesOrder.id == order_id)
        .options(selectinload(SalesOrder.lines))
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("SalesOrder", order_id)
    if order.status is not SalesOrderStatus.DRAFT:
        raise ConflictError(
            f"SalesOrder {order_id} is not a draft (status={order.status.value}); cannot confirm"
        )

    if not order.lines or order.total <= 0:
        raise DomainValidationError(
            f"SalesOrder {order_id} cannot be confirmed: at least one line and a positive "
            "total are required (ADR-006 R2)"
        )

    customer = await _fetch_customer(session, company_id, order.customer_id)
    order.snapshot_customer_code = customer.code
    order.snapshot_customer_name = customer.name

    products = await _fetch_products(session, company_id, {line.product_id for line in order.lines})
    for line in order.lines:
        product = products[line.product_id]
        line.snapshot_sku = product.sku
        line.snapshot_product_name = product.name

    context = HookContext(
        company_id=company_id,
        order_id=order.id,
        order_no=order.order_no,
        customer_id=order.customer_id,
        total=order.total,
    )
    await hooks.run(SALES_ORDER_VALIDATE_CONFIRM, session, context)

    order.status = SalesOrderStatus.CONFIRMED
    # `timezone.utc`, not the `datetime.UTC` alias ruff's UP017 would prefer
    # for this project's declared py312 target: this codebase's own
    # sandboxed verification environment (see tests/conftest.py's pgserver
    # fallback docstring) runs on Python 3.10, where `datetime.UTC` does not
    # exist yet — `timezone.utc` is the portable spelling that works on both.
    order.confirmed_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        SALES_ORDER_CONFIRMED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": order.id,
            "order_no": order.order_no,
            "customer_id": order.customer_id,
            "total": order.total,
        },
    )

    await session.flush()
    return order


async def cancel_order(session: AsyncSession, order_id: uuid.UUID) -> SalesOrder:
    """`draft` or `confirmed` -> `cancelled`. `SELECT ... FOR UPDATE` first (R1),

    same doctrine as `confirm_order`. Week 4 has no downstream shipment to
    conflict with a cancelled-but-confirmed order (ADR-006 Decision 3), so
    both source statuses are legal.

    Not idempotent: cancelling an already-cancelled order is a 409, not a
    silent no-op — a deliberate divergence from `ledger.service.close_period`
    (which treats a repeat close as idempotent). `close_period` has no
    "wrong request" story worth flagging (Week 2 has no reopen workflow, so
    a duplicate close request is just... satisfied twice, harmlessly);
    `cancel` sits in the same state-machine family `confirm` does, where
    ADR-006 R1's whole point is that an illegal transition from the current
    state must be visible to the caller as a 409, not silently swallowed —
    a client re-cancelling something it does not realize is already
    cancelled deserves to find out, the same way a client re-confirming
    something already confirmed does.
    """
    result = await session.execute(
        select(SalesOrder).where(SalesOrder.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("SalesOrder", order_id)
    if order.status is SalesOrderStatus.CANCELLED:
        raise ConflictError(f"SalesOrder {order_id} is already cancelled")

    order.status = SalesOrderStatus.CANCELLED
    order.cancelled_at = datetime.now(timezone.utc)  # noqa: UP017 — see confirm_order's comment

    await _commit_or_conflict(session, entity="SalesOrder")
    return await get_order(session, order.id)
