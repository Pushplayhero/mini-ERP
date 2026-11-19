"""purchase service layer — business logic + transaction boundary (ADR-014).

Every write stamps `company_id` from `require_current_company_id()`. Cross-module
references (`suppliers`, `products`) use lightweight `sqlalchemy.table()` references
with explicit `company_id` predicates to preserve strict module independence.
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
from app.core.authorization import require_permission
from app.core.customfields import CustomFieldEntityType, validate_and_normalize_custom_data
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.hooks import HookContext
from app.core.tenancy import require_current_company_id
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowTransition,
    verify_workflow_approval,
)
from app.modules.purchase.events import (
    PURCHASE_GOODS_RECEIVED_EVENT_TYPE,
    PURCHASE_ORDER_CONFIRMED_EVENT_TYPE,
)
from app.modules.purchase.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseSequence,
)
from app.modules.purchase.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderLineCreate,
    PurchaseOrderUpdate,
    _round_half_even_6dp,
)

PURCHASE_ORDER_VALIDATE_CONFIRM = "purchase.order.validate_confirm"
hooks.define_hook_point(PURCHASE_ORDER_VALIDATE_CONFIRM, "1.0.0")

_SUPPLIERS = sa_table(
    "suppliers",
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
    sa_column("uom_id"),
    sa_column("standard_cost"),
)


async def _commit_or_conflict(session: AsyncSession, *, entity: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{entity} violates a uniqueness or reference constraint") from exc


async def _fetch_supplier(
    session: AsyncSession, company_id: uuid.UUID, supplier_id: uuid.UUID
) -> Any:
    result = await session.execute(
        select(
            _SUPPLIERS.c.id,
            _SUPPLIERS.c.code,
            _SUPPLIERS.c.name,
            _SUPPLIERS.c.currency_code,
        ).where(_SUPPLIERS.c.id == supplier_id, _SUPPLIERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"supplier_id {supplier_id} not found or does not belong to this company"
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
            _PRODUCTS.c.uom_id,
            _PRODUCTS.c.standard_cost,
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
    lines_in: Sequence[PurchaseOrderLineCreate],
    products: dict[uuid.UUID, Any],
) -> tuple[list[PurchaseOrderLine], Decimal]:
    built: list[PurchaseOrderLine] = []
    total = Decimal("0")
    for idx, line_in in enumerate(lines_in, start=1):
        product = products[line_in.product_id]
        is_override = line_in.unit_cost is not None
        unit_cost = line_in.unit_cost if line_in.unit_cost is not None else product.standard_cost
        uom_id = line_in.uom_id if line_in.uom_id is not None else product.uom_id
        amount = _round_half_even_6dp(line_in.qty * unit_cost)
        built.append(
            PurchaseOrderLine(
                company_id=company_id,
                line_no=idx,
                product_id=line_in.product_id,
                qty=line_in.qty,
                uom_id=uom_id,
                unit_cost=unit_cost,
                unit_cost_is_override=is_override,
                snapshot_sku=product.sku,
                snapshot_product_name=product.name,
                amount=amount,
            )
        )
        total += amount
    return built, _round_half_even_6dp(total)


async def _allocate_order_no(session: AsyncSession, company_id: uuid.UUID, year: int) -> str:
    """`(company_id, year) -> next_no` allocation under `SELECT ... FOR UPDATE`."""
    await session.execute(
        pg_insert(PurchaseSequence)
        .values(company_id=company_id, year=year, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )
    result = await session.execute(
        select(PurchaseSequence)
        .where(PurchaseSequence.company_id == company_id, PurchaseSequence.year == year)
        .with_for_update()
    )
    seq = result.scalar_one()
    allocated_no = seq.next_no
    seq.next_no = allocated_no + 1
    return f"PO-{year:04d}-{allocated_no:06d}"


async def create_order(session: AsyncSession, data: PurchaseOrderCreate) -> PurchaseOrder:
    require_permission("purchase.order.create")
    company_id = require_current_company_id()
    supplier = await _fetch_supplier(session, company_id, data.supplier_id)
    products = await _fetch_products(session, company_id, {line.product_id for line in data.lines})
    lines, total = _build_lines(company_id, data.lines, products)

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.PURCHASE_ORDER,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    order_no = await _allocate_order_no(session, company_id, date.today().year)

    order = PurchaseOrder(
        company_id=company_id,
        order_no=order_no,
        supplier_id=data.supplier_id,
        status=PurchaseOrderStatus.DRAFT,
        currency_code=supplier.currency_code,
        total=total,
        snapshot_supplier_code=supplier.code,
        snapshot_supplier_name=supplier.name,
        custom_data=normalized_custom_data,
    )
    order.lines = lines
    session.add(order)
    await _commit_or_conflict(session, entity="PurchaseOrder")
    return await load_order_unchecked(session, order.id)


async def list_orders(session: AsyncSession) -> Sequence[PurchaseOrder]:
    require_permission("purchase.order.read")
    result = await session.execute(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .order_by(PurchaseOrder.order_no)
    )
    return result.scalars().all()


async def load_order_unchecked(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder:
    result = await session.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == order_id)
        .options(selectinload(PurchaseOrder.lines))
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("PurchaseOrder", order_id)
    return order


async def get_order(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder:
    require_permission("purchase.order.read")
    return await load_order_unchecked(session, order_id)


async def update_order(
    session: AsyncSession, order_id: uuid.UUID, data: PurchaseOrderUpdate
) -> PurchaseOrder:
    """Draft-only. Takes `SELECT ... FOR UPDATE` before status check."""
    require_permission("purchase.order.update")
    company_id = require_current_company_id()
    result = await session.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == order_id)
        .options(selectinload(PurchaseOrder.lines))
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("PurchaseOrder", order_id)
    if order.status is not PurchaseOrderStatus.DRAFT:
        raise ConflictError(
            f"PurchaseOrder {order_id} is not a draft (status={order.status.value}); "
            "only draft orders can be edited"
        )

    if data.lines is not None:
        products = await _fetch_products(
            session, company_id, {line.product_id for line in data.lines}
        )
        lines, total = _build_lines(company_id, data.lines, products)
        order.lines.clear()
        await session.flush()
        order.lines = lines
        order.total = total

    if data.custom_data is not None:
        order.custom_data = await validate_and_normalize_custom_data(
            session,
            entity_type=CustomFieldEntityType.PURCHASE_ORDER,
            raw_custom_data=data.custom_data,
            is_create=False,
            existing_custom_data=order.custom_data,
        )

    await _commit_or_conflict(session, entity="PurchaseOrder")
    return await load_order_unchecked(session, order.id)


async def confirm_order(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder:
    """Flush-only state transition: draft -> confirmed."""
    require_permission("purchase.order.confirm")
    company_id = require_current_company_id()
    result = await session.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == order_id)
        .options(selectinload(PurchaseOrder.lines))
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("PurchaseOrder", order_id)
    if order.status is not PurchaseOrderStatus.DRAFT:
        raise ConflictError(
            f"PurchaseOrder {order_id} is not a draft (status={order.status.value}); cannot confirm"
        )

    if not order.lines:
        raise DomainValidationError(
            f"PurchaseOrder {order_id} cannot be confirmed: at least one line is required (ADR-014)"
        )

    supplier = await _fetch_supplier(session, company_id, order.supplier_id)
    order.snapshot_supplier_code = supplier.code
    order.snapshot_supplier_name = supplier.name

    products = await _fetch_products(session, company_id, {line.product_id for line in order.lines})
    new_total = Decimal("0")
    for line in order.lines:
        product = products[line.product_id]
        line.snapshot_sku = product.sku
        line.snapshot_product_name = product.name
        if not line.unit_cost_is_override:
            line.unit_cost = product.standard_cost
            line.amount = _round_half_even_6dp(line.qty * product.standard_cost)
        else:
            line.amount = _round_half_even_6dp(line.amount)
        new_total += line.amount
    order.total = _round_half_even_6dp(new_total)

    if order.total <= 0:
        raise DomainValidationError(
            f"PurchaseOrder {order_id} cannot be confirmed: a positive total is required (ADR-014)"
        )

    context = HookContext(
        company_id=company_id,
        order_id=order.id,
        order_no=order.order_no,
        customer_id=order.supplier_id,
        total=order.total,
        supplier_id=order.supplier_id,
    )
    await hooks.run(PURCHASE_ORDER_VALIDATE_CONFIRM, session, context)

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.PURCHASE_ORDER,
        entity_id=order.id,
        transition=WorkflowTransition.CONFIRM,
        amount=order.total,
    )

    order.status = PurchaseOrderStatus.CONFIRMED
    order.confirmed_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        PURCHASE_ORDER_CONFIRMED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": order.id,
            "order_no": order.order_no,
            "supplier_id": order.supplier_id,
            "total": order.total,
        },
    )

    await session.flush()
    return order


async def receive_order(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder:
    """Flush-only state transition: confirmed -> received."""
    require_permission("purchase.order.receive")
    company_id = require_current_company_id()
    result = await session.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == order_id)
        .options(selectinload(PurchaseOrder.lines))
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("PurchaseOrder", order_id)
    if order.status is not PurchaseOrderStatus.CONFIRMED:
        raise ConflictError(
            f"PurchaseOrder {order_id} is not confirmed (status={order.status.value}); "
            "cannot receive"
        )

    lines_payload: list[dict[str, Any]] = []
    total_cost = Decimal("0")
    for line in order.lines:
        total_cost += line.amount
        lines_payload.append(
            {
                "product_id": str(line.product_id),
                "qty": str(line.qty),
                "unit_cost": str(line.unit_cost),
            }
        )
    total_cost = _round_half_even_6dp(total_cost)

    received_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        PURCHASE_GOODS_RECEIVED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": order.id,
            "order_no": order.order_no,
            "supplier_id": order.supplier_id,
            "lines": lines_payload,
            "total_cost": str(total_cost),
            "received_at": received_at.isoformat(),
        },
    )

    order.status = PurchaseOrderStatus.RECEIVED
    order.received_at = received_at

    await session.flush()
    return order


async def cancel_order(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder:
    require_permission("purchase.order.cancel")
    result = await session.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("PurchaseOrder", order_id)
    if order.status is PurchaseOrderStatus.CANCELLED:
        raise ConflictError(f"PurchaseOrder {order_id} is already cancelled")
    if order.status is PurchaseOrderStatus.RECEIVED:
        raise ConflictError(
            f"PurchaseOrder {order_id} has already received goods and cannot be cancelled"
        )

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.PURCHASE_ORDER,
        entity_id=order.id,
        transition=WorkflowTransition.CANCEL,
        amount=order.total,
    )

    order.status = PurchaseOrderStatus.CANCELLED
    order.cancelled_at = datetime.now(timezone.utc)  # noqa: UP017

    await _commit_or_conflict(session, entity="PurchaseOrder")
    return await load_order_unchecked(session, order.id)
