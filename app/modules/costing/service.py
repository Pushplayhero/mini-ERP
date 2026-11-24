"""costing service layer — moving weighted average, landed cost, PPV, and revaluation (ADR-016).

Flush-only mutation cores mirror `sales`/`purchase`/`payables`; router owns commit +
`IntegrityError -> 409 ConflictError` translation. Cross-module lookups read
`products`, `stock_summary`, `purchase_orders`, and `purchase_order_lines` via
lightweight `sqlalchemy.table()` Core references with explicit `company_id`
predicates (import-linter compliant).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import column as sa_column
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.authorization import require_current_actor, require_permission
from app.core.customfields import CustomFieldEntityType, validate_and_normalize_custom_data
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import require_current_company_id
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowTransition,
    verify_workflow_approval,
)
from app.modules.costing.events import (
    COSTING_LANDED_COST_POSTED_EVENT_TYPE,
    COSTING_LANDED_COST_VOIDED_EVENT_TYPE,
    COSTING_PPV_FAVORABLE_POSTED_EVENT_TYPE,
    COSTING_PPV_FAVORABLE_VOIDED_EVENT_TYPE,
    COSTING_PPV_UNFAVORABLE_POSTED_EVENT_TYPE,
    COSTING_PPV_UNFAVORABLE_VOIDED_EVENT_TYPE,
    COSTING_REVALUATION_DOWN_POSTED_EVENT_TYPE,
    COSTING_REVALUATION_DOWN_VOIDED_EVENT_TYPE,
    COSTING_REVALUATION_UP_POSTED_EVENT_TYPE,
    COSTING_REVALUATION_UP_VOIDED_EVENT_TYPE,
)
from app.modules.costing.models import (
    AMOUNT,
    CostAdjustment,
    CostAdjustmentStatus,
    CostAdjustmentType,
    CostingSequence,
    CostMove,
    CostMoveType,
    ProductCostSummary,
)
from app.modules.costing.schemas import CostAdjustmentCreate, ValuationVarianceRow

logger = logging.getLogger(__name__)

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)
_ZERO = Decimal("0.000000")

_GOODS_RECEIVED_SOURCE_TYPE = "purchase.goods_received"
_GOODS_SHIPPED_SOURCE_TYPE = "sales.goods_shipped"
_MATERIAL_ISSUED_SOURCE_TYPE = "mrp.material_issued"
_MATERIAL_RETURNED_SOURCE_TYPE = "mrp.material_returned"
_PRODUCTION_RECEIVED_SOURCE_TYPE = "mrp.production_received"
_ADJUSTMENT_POSTED_SOURCE_TYPE = "costing.adjustment_posted"
_ADJUSTMENT_VOIDED_SOURCE_TYPE = "costing.adjustment_voided"

_PRODUCTS = sa_table(
    "products",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("sku"),
    sa_column("name"),
    sa_column("standard_cost", AMOUNT),
    sa_column("updated_at"),
    sa_column("updated_by"),
)

_STOCK_SUMMARY = sa_table(
    "stock_summary",
    sa_column("company_id"),
    sa_column("product_id"),
    sa_column("on_hand", AMOUNT),
)

_PURCHASE_ORDERS = sa_table(
    "purchase_orders",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("order_no"),
    sa_column("status"),
)

_PURCHASE_ORDER_LINES = sa_table(
    "purchase_order_lines",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("order_id"),
    sa_column("product_id"),
)

# Maps (adjustment_type, is_positive_amount, is_void) -> ledger posting event_type.
_ADJUSTMENT_POSTING_EVENTS: dict[tuple[CostAdjustmentType, bool, bool], str] = {
    (CostAdjustmentType.LANDED_COST, True, False): COSTING_LANDED_COST_POSTED_EVENT_TYPE,
    (CostAdjustmentType.LANDED_COST, True, True): COSTING_LANDED_COST_VOIDED_EVENT_TYPE,
    (CostAdjustmentType.PPV, True, False): COSTING_PPV_UNFAVORABLE_POSTED_EVENT_TYPE,
    (CostAdjustmentType.PPV, True, True): COSTING_PPV_UNFAVORABLE_VOIDED_EVENT_TYPE,
    (CostAdjustmentType.PPV, False, False): COSTING_PPV_FAVORABLE_POSTED_EVENT_TYPE,
    (CostAdjustmentType.PPV, False, True): COSTING_PPV_FAVORABLE_VOIDED_EVENT_TYPE,
    (CostAdjustmentType.REVALUATION, True, False): COSTING_REVALUATION_UP_POSTED_EVENT_TYPE,
    (CostAdjustmentType.REVALUATION, True, True): COSTING_REVALUATION_UP_VOIDED_EVENT_TYPE,
    (CostAdjustmentType.REVALUATION, False, False): COSTING_REVALUATION_DOWN_POSTED_EVENT_TYPE,
    (CostAdjustmentType.REVALUATION, False, True): COSTING_REVALUATION_DOWN_VOIDED_EVENT_TYPE,
}


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _is_source_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name:
        return str(constraint_name) == "uq_cost_moves_source"
    return "uq_cost_moves_source" in str(exc)


async def _ensure_product_belongs_to_company(
    session: AsyncSession, company_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    res = await session.execute(
        select(_PRODUCTS.c.id).where(
            _PRODUCTS.c.id == product_id,
            _PRODUCTS.c.company_id == company_id,
        )
    )
    if res.first() is None:
        raise DomainValidationError(
            f"product_id {product_id} not found or does not belong to this company"
        )


async def _validate_order_for_adjustment(
    session: AsyncSession,
    company_id: uuid.UUID,
    adjustment_type: CostAdjustmentType,
    order_id: uuid.UUID | None,
    product_id: uuid.UUID,
) -> None:
    if adjustment_type == CostAdjustmentType.REVALUATION and order_id is not None:
        raise DomainValidationError("order_id is not permitted for revaluation cost adjustments")
    if order_id is None:
        return
    po_res = await session.execute(
        select(_PURCHASE_ORDERS.c.id, _PURCHASE_ORDERS.c.status).where(
            _PURCHASE_ORDERS.c.id == order_id,
            _PURCHASE_ORDERS.c.company_id == company_id,
        )
    )
    po = po_res.first()
    if po is None:
        raise DomainValidationError(
            f"order_id {order_id} not found or does not belong to this company"
        )

    if adjustment_type in (CostAdjustmentType.LANDED_COST, CostAdjustmentType.PPV):
        if str(po.status).upper() != "RECEIVED":
            raise DomainValidationError(
                f"PurchaseOrder {order_id} is not received (status={po.status}); "
                f"cannot create {adjustment_type.value} adjustment"
            )
        line_res = await session.execute(
            select(_PURCHASE_ORDER_LINES.c.id)
            .where(
                _PURCHASE_ORDER_LINES.c.order_id == order_id,
                _PURCHASE_ORDER_LINES.c.company_id == company_id,
                _PURCHASE_ORDER_LINES.c.product_id == product_id,
            )
            .limit(1)
        )
        if line_res.first() is None:
            raise DomainValidationError(
                f"product_id {product_id} does not appear on PurchaseOrder {order_id}"
            )


async def _allocate_adjustment_no(session: AsyncSession, company_id: uuid.UUID, year: int) -> str:
    await session.execute(
        pg_insert(CostingSequence)
        .values(company_id=company_id, year=year, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )
    res = await session.execute(
        select(CostingSequence)
        .where(CostingSequence.company_id == company_id, CostingSequence.year == year)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    seq = res.scalar_one()
    allocated = seq.next_no
    seq.next_no = allocated + 1
    return f"COST-{year:04d}-{allocated:06d}"


# ---------------------------------------------------------------------------
# Core Valuation Subledger Primitive (ADR-016)
# ---------------------------------------------------------------------------


async def get_or_lock_cost_summary(
    session: AsyncSession, company_id: uuid.UUID, product_id: uuid.UUID
) -> ProductCostSummary:
    """Get-or-create + lock the `(company_id, product_id)` cost summary row.

    Takes the SHARED per-company advisory lock (`app.core.locking`) before
    `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE`, pairing with
    `rebuild_cost_summaries`'s EXCLUSIVE lock.
    """
    await acquire_company_advisory_lock(session, company_id, exclusive=False)
    await session.execute(
        pg_insert(ProductCostSummary)
        .values(
            company_id=company_id,
            product_id=product_id,
            tracked_qty=_ZERO,
            total_cost_value=_ZERO,
            moving_avg_cost=_ZERO,
        )
        .on_conflict_do_nothing(index_elements=["company_id", "product_id"])
    )
    result = await session.execute(
        select(ProductCostSummary)
        .where(
            ProductCostSummary.company_id == company_id,
            ProductCostSummary.product_id == product_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Bus Subscribers for `purchase.goods_received` & `sales.goods_shipped`
# ---------------------------------------------------------------------------


async def handle_goods_received(session: AsyncSession, payload: BaseModel) -> None:
    """Increment moving-weighted-average cost summary on PO receipt inside SAVEPOINT."""
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    order_no = str(payload_dict.get("order_no", ""))
    lines: Sequence[dict[str, Any]] = payload_dict["lines"]

    agg: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for line in lines:
        product_id = uuid.UUID(str(line["product_id"]))
        qty = Decimal(str(line["qty"]))
        unit_cost = Decimal(str(line["unit_cost"]))
        line_val = _round_half_even_6dp(qty * unit_cost)
        prev_qty, prev_val = agg.get(product_id, (_ZERO, _ZERO))
        agg[product_id] = (prev_qty + qty, prev_val + line_val)

    try:
        async with session.begin_nested():
            for product_id in sorted(agg):
                raw_qty, raw_val = agg[product_id]
                delta_qty = _round_half_even_6dp(raw_qty)
                delta_val = _round_half_even_6dp(raw_val)
                if delta_qty <= 0:
                    continue

                summary = await get_or_lock_cost_summary(session, company_id, product_id)
                eff_unit_cost = _round_half_even_6dp(delta_val / delta_qty)
                new_qty = _round_half_even_6dp(summary.tracked_qty + delta_qty)
                new_val = _round_half_even_6dp(summary.total_cost_value + delta_val)
                new_avg = (
                    _round_half_even_6dp(new_val / new_qty)
                    if new_qty > 0
                    else summary.moving_avg_cost
                )

                move = CostMove(
                    company_id=company_id,
                    product_id=product_id,
                    move_type=CostMoveType.RECEIPT,
                    qty_delta=delta_qty,
                    value_delta=delta_val,
                    unit_cost=eff_unit_cost,
                    resulting_qty=new_qty,
                    resulting_value=new_val,
                    resulting_avg_cost=new_avg,
                    source_type=_GOODS_RECEIVED_SOURCE_TYPE,
                    source_id=source_id,
                    reason=f"PO receipt {order_no}"[:255] if order_no else "PO receipt",
                )
                session.add(move)
                summary.tracked_qty = new_qty
                summary.total_cost_value = new_val
                summary.moving_avg_cost = new_avg
            await session.flush()
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "purchase.goods_received source_id=%s already costed for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


def _build_shipment_cost_move(
    *,
    company_id: uuid.UUID,
    product_id: uuid.UUID,
    summary: ProductCostSummary,
    ship_qty: Decimal,
    std_unit_cost: Decimal,
    source_id: uuid.UUID,
    order_no: str,
) -> CostMove:
    if summary.tracked_qty == 0:
        # Standard-cost fallback for manual adjustment stock without tracked PO lots.
        return CostMove(
            company_id=company_id,
            product_id=product_id,
            move_type=CostMoveType.SHIPMENT,
            qty_delta=_ZERO,
            value_delta=_ZERO,
            unit_cost=std_unit_cost,
            resulting_qty=_ZERO,
            resulting_value=_ZERO,
            resulting_avg_cost=summary.moving_avg_cost,
            source_type=_GOODS_SHIPPED_SOURCE_TYPE,
            source_id=source_id,
            reason=(
                f"SO shipment {order_no} (standard cost fallback)"[:255]
                if order_no
                else "SO shipment (standard cost fallback)"
            ),
        )

    if summary.tracked_qty >= ship_qty:
        qty_deducted = ship_qty
        new_qty = _round_half_even_6dp(summary.tracked_qty - ship_qty)
        if new_qty == 0:
            val_deducted = summary.total_cost_value
        else:
            val_deducted = min(
                summary.total_cost_value,
                _round_half_even_6dp(ship_qty * summary.moving_avg_cost),
            )
        new_val = _round_half_even_6dp(summary.total_cost_value - val_deducted)
        new_avg = (
            _round_half_even_6dp(new_val / new_qty) if new_qty > 0 else summary.moving_avg_cost
        )
    else:
        qty_deducted = summary.tracked_qty
        val_deducted = summary.total_cost_value
        new_qty = _ZERO
        new_val = _ZERO
        new_avg = summary.moving_avg_cost

    move_unit_cost = summary.moving_avg_cost
    summary.tracked_qty = new_qty
    summary.total_cost_value = new_val
    summary.moving_avg_cost = new_avg
    return CostMove(
        company_id=company_id,
        product_id=product_id,
        move_type=CostMoveType.SHIPMENT,
        qty_delta=-qty_deducted,
        value_delta=-val_deducted,
        unit_cost=move_unit_cost,
        resulting_qty=new_qty,
        resulting_value=new_val,
        resulting_avg_cost=new_avg,
        source_type=_GOODS_SHIPPED_SOURCE_TYPE,
        source_id=source_id,
        reason=f"SO shipment {order_no}"[:255] if order_no else "SO shipment",
    )


async def handle_goods_shipped(session: AsyncSession, payload: BaseModel) -> None:
    """Relieve moving-average cost on sales shipment with standard-cost fallback."""
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    order_no = str(payload_dict.get("order_no", ""))
    lines: Sequence[dict[str, Any]] = payload_dict["lines"]

    agg: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for line in lines:
        product_id = uuid.UUID(str(line["product_id"]))
        qty = Decimal(str(line["qty"]))
        unit_cost = Decimal(str(line["unit_cost"]))
        line_val = _round_half_even_6dp(qty * unit_cost)
        prev_qty, prev_val = agg.get(product_id, (_ZERO, _ZERO))
        agg[product_id] = (prev_qty + qty, prev_val + line_val)

    try:
        async with session.begin_nested():
            for product_id in sorted(agg):
                raw_qty, raw_std_val = agg[product_id]
                ship_qty = _round_half_even_6dp(raw_qty)
                if ship_qty <= 0:
                    continue

                summary = await get_or_lock_cost_summary(session, company_id, product_id)
                std_unit_cost = _round_half_even_6dp(_round_half_even_6dp(raw_std_val) / ship_qty)
                move = _build_shipment_cost_move(
                    company_id=company_id,
                    product_id=product_id,
                    summary=summary,
                    ship_qty=ship_qty,
                    std_unit_cost=std_unit_cost,
                    source_id=source_id,
                    order_no=order_no,
                )
                session.add(move)
            await session.flush()
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "sales.goods_shipped source_id=%s already costed for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


def _build_production_issue_cost_move(
    *,
    company_id: uuid.UUID,
    product_id: uuid.UUID,
    summary: ProductCostSummary,
    issue_qty: Decimal,
    line_cost: Decimal,
    unit_cost: Decimal,
    source_id: uuid.UUID,
    wo_no: str,
) -> CostMove:
    reason = f"WO {wo_no} material issue"[:255] if wo_no else "WO material issue"
    if summary.tracked_qty == 0:
        return CostMove(
            company_id=company_id,
            product_id=product_id,
            move_type=CostMoveType.PRODUCTION_ISSUE,
            qty_delta=_ZERO,
            value_delta=_ZERO,
            unit_cost=unit_cost,
            resulting_qty=_ZERO,
            resulting_value=_ZERO,
            resulting_avg_cost=summary.moving_avg_cost,
            source_type=_MATERIAL_ISSUED_SOURCE_TYPE,
            source_id=source_id,
            reason=f"{reason} (standard cost fallback)"[:255],
        )

    if summary.tracked_qty >= issue_qty:
        qty_deducted = issue_qty
        new_qty = _round_half_even_6dp(summary.tracked_qty - issue_qty)
        val_deducted = (
            summary.total_cost_value
            if new_qty == 0
            else min(summary.total_cost_value, _round_half_even_6dp(line_cost))
        )
        new_val = _round_half_even_6dp(summary.total_cost_value - val_deducted)
        new_avg = (
            _round_half_even_6dp(new_val / new_qty) if new_qty > 0 else summary.moving_avg_cost
        )
    else:
        qty_deducted = summary.tracked_qty
        val_deducted = summary.total_cost_value
        new_qty = _ZERO
        new_val = _ZERO
        new_avg = summary.moving_avg_cost

    move_unit = summary.moving_avg_cost
    summary.tracked_qty = new_qty
    summary.total_cost_value = new_val
    summary.moving_avg_cost = new_avg
    return CostMove(
        company_id=company_id,
        product_id=product_id,
        move_type=CostMoveType.PRODUCTION_ISSUE,
        qty_delta=-qty_deducted,
        value_delta=-val_deducted,
        unit_cost=move_unit,
        resulting_qty=new_qty,
        resulting_value=new_val,
        resulting_avg_cost=new_avg,
        source_type=_MATERIAL_ISSUED_SOURCE_TYPE,
        source_id=source_id,
        reason=reason,
    )


async def handle_material_issued(session: AsyncSession, payload: BaseModel) -> None:
    """Relieve moving-average component cost on `mrp.material_issued` inside a SAVEPOINT."""
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    wo_no = str(payload_dict.get("wo_no", ""))
    lines: Sequence[dict[str, Any]] = payload_dict["lines"]

    agg: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for line in lines:
        product_id = uuid.UUID(str(line["product_id"]))
        qty = Decimal(str(line["qty"]))
        cost = Decimal(str(line["line_cost"]))
        prev_q, prev_v = agg.get(product_id, (_ZERO, _ZERO))
        agg[product_id] = (prev_q + qty, prev_v + cost)

    try:
        async with session.begin_nested():
            for product_id in sorted(agg):
                raw_q, raw_v = agg[product_id]
                issue_qty = _round_half_even_6dp(raw_q)
                line_cost = _round_half_even_6dp(raw_v)
                if issue_qty <= 0:
                    continue
                summary = await get_or_lock_cost_summary(session, company_id, product_id)
                eff_unit = _round_half_even_6dp(line_cost / issue_qty)
                move = _build_production_issue_cost_move(
                    company_id=company_id,
                    product_id=product_id,
                    summary=summary,
                    issue_qty=issue_qty,
                    line_cost=line_cost,
                    unit_cost=eff_unit,
                    source_id=source_id,
                    wo_no=wo_no,
                )
                session.add(move)
            await session.flush()
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "mrp.material_issued source_id=%s already costed for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


async def handle_material_returned(session: AsyncSession, payload: BaseModel) -> None:
    """Restore returned component cost into MWA summary on `mrp.material_returned`."""
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    wo_no = str(payload_dict.get("wo_no", ""))
    lines: Sequence[dict[str, Any]] = payload_dict["lines"]

    agg: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for line in lines:
        product_id = uuid.UUID(str(line["product_id"]))
        qty = Decimal(str(line["qty"]))
        cost = Decimal(str(line["line_cost"]))
        prev_q, prev_v = agg.get(product_id, (_ZERO, _ZERO))
        agg[product_id] = (prev_q + qty, prev_v + cost)

    try:
        async with session.begin_nested():
            for product_id in sorted(agg):
                raw_q, raw_v = agg[product_id]
                delta_qty = _round_half_even_6dp(raw_q)
                delta_val = _round_half_even_6dp(raw_v)
                if delta_qty <= 0:
                    continue
                summary = await get_or_lock_cost_summary(session, company_id, product_id)
                eff_unit = _round_half_even_6dp(delta_val / delta_qty)
                new_qty = _round_half_even_6dp(summary.tracked_qty + delta_qty)
                new_val = _round_half_even_6dp(summary.total_cost_value + delta_val)
                new_avg = (
                    _round_half_even_6dp(new_val / new_qty)
                    if new_qty > 0
                    else summary.moving_avg_cost
                )
                move = CostMove(
                    company_id=company_id,
                    product_id=product_id,
                    move_type=CostMoveType.PRODUCTION_RETURN,
                    qty_delta=delta_qty,
                    value_delta=delta_val,
                    unit_cost=eff_unit,
                    resulting_qty=new_qty,
                    resulting_value=new_val,
                    resulting_avg_cost=new_avg,
                    source_type=_MATERIAL_RETURNED_SOURCE_TYPE,
                    source_id=source_id,
                    reason=f"WO {wo_no} material return"[:255] if wo_no else "WO material return",
                )
                session.add(move)
                summary.tracked_qty = new_qty
                summary.total_cost_value = new_val
                summary.moving_avg_cost = new_avg
            await session.flush()
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "mrp.material_returned source_id=%s already costed for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


async def handle_production_received(session: AsyncSession, payload: BaseModel) -> None:
    """Capitalize WIP production cost into finished-good MWA summary without rounding drift."""
    company_id = require_current_company_id()
    payload_dict: dict[str, Any] = payload.model_dump()
    source_id = uuid.UUID(str(payload_dict["source_id"]))
    product_id = uuid.UUID(str(payload_dict["product_id"]))
    wo_no = str(payload_dict.get("wo_no", ""))
    delta_qty = _round_half_even_6dp(Decimal(str(payload_dict["qty"])))
    delta_val = _round_half_even_6dp(Decimal(str(payload_dict["total_cost"])))
    if delta_qty <= 0:
        return

    try:
        async with session.begin_nested():
            summary = await get_or_lock_cost_summary(session, company_id, product_id)
            eff_unit = _round_half_even_6dp(delta_val / delta_qty)
            new_qty = _round_half_even_6dp(summary.tracked_qty + delta_qty)
            new_val = _round_half_even_6dp(summary.total_cost_value + delta_val)
            new_avg = (
                _round_half_even_6dp(new_val / new_qty) if new_qty > 0 else summary.moving_avg_cost
            )
            move = CostMove(
                company_id=company_id,
                product_id=product_id,
                move_type=CostMoveType.PRODUCTION_RECEIPT,
                qty_delta=delta_qty,
                value_delta=delta_val,
                unit_cost=eff_unit,
                resulting_qty=new_qty,
                resulting_value=new_val,
                resulting_avg_cost=new_avg,
                source_type=_PRODUCTION_RECEIVED_SOURCE_TYPE,
                source_id=source_id,
                reason=f"WO {wo_no} production receipt"[:255] if wo_no else "WO production receipt",
            )
            session.add(move)
            summary.tracked_qty = new_qty
            summary.total_cost_value = new_val
            summary.moving_avg_cost = new_avg
            await session.flush()
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "mrp.production_received source_id=%s already costed for company %s; "
            "skipping duplicate delivery",
            source_id,
            company_id,
        )


# ---------------------------------------------------------------------------
# Cost Adjustments (Landed Cost, PPV, Revaluation)
# ---------------------------------------------------------------------------


def _apply_adjustment_to_summary(
    summary: ProductCostSummary,
    adjustment_type: CostAdjustmentType,
    signed_delta: Decimal,
    *,
    is_void: bool,
) -> tuple[CostMoveType, Decimal, Decimal]:
    if adjustment_type == CostAdjustmentType.PPV:
        return CostMoveType.PPV, _ZERO, summary.moving_avg_cost

    if summary.tracked_qty <= 0:
        action = "void" if is_void else "apply"
        raise ConflictError(
            f"Product {summary.product_id} has zero tracked quantity; "
            f"cannot {action} {adjustment_type.value} adjustment"
        )

    new_val = _round_half_even_6dp(summary.total_cost_value + signed_delta)
    if new_val < 0:
        raise ConflictError(
            f"Cost adjustment delta {signed_delta} would drive tracked inventory value "
            f"negative ({summary.total_cost_value} + {signed_delta} = {new_val})"
        )
    new_avg = _round_half_even_6dp(new_val / summary.tracked_qty)
    summary.total_cost_value = new_val
    summary.moving_avg_cost = new_avg
    move_type = (
        CostMoveType.LANDED_COST
        if adjustment_type == CostAdjustmentType.LANDED_COST
        else CostMoveType.REVALUATION
    )
    return move_type, signed_delta, new_avg


async def create_adjustment(session: AsyncSession, data: CostAdjustmentCreate) -> CostAdjustment:
    """Create and post a Landed Cost, PPV, or Revaluation adjustment (flush-only)."""
    require_permission("costing.adjustment.create")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await _ensure_product_belongs_to_company(session, company_id, data.product_id)
    await _validate_order_for_adjustment(
        session, company_id, data.adjustment_type, data.order_id, data.product_id
    )

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.COST_ADJUSTMENT,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    summary = await get_or_lock_cost_summary(session, company_id, data.product_id)
    move_type, value_delta, unit_cost = _apply_adjustment_to_summary(
        summary, data.adjustment_type, data.amount, is_void=False
    )

    adj_date = data.adjustment_date or datetime.now(timezone.utc).date()  # noqa: UP017
    adj_no = await _allocate_adjustment_no(session, company_id, adj_date.year)

    adjustment = CostAdjustment(
        company_id=company_id,
        adjustment_no=adj_no,
        adjustment_type=data.adjustment_type,
        status=CostAdjustmentStatus.POSTED,
        product_id=data.product_id,
        order_id=data.order_id,
        amount=data.amount,
        adjustment_date=adj_date,
        reason=data.reason,
        custom_data=normalized_custom_data,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    session.add(adjustment)
    await session.flush()

    move = CostMove(
        company_id=company_id,
        product_id=data.product_id,
        move_type=move_type,
        qty_delta=_ZERO,
        value_delta=value_delta,
        unit_cost=unit_cost,
        resulting_qty=summary.tracked_qty,
        resulting_value=summary.total_cost_value,
        resulting_avg_cost=summary.moving_avg_cost,
        source_type=_ADJUSTMENT_POSTED_SOURCE_TYPE,
        source_id=adjustment.id,
        reason=f"{adj_no}: {data.reason}"[:255],
        created_by=actor.user_id,
    )
    session.add(move)

    event_type = _ADJUSTMENT_POSTING_EVENTS[(data.adjustment_type, data.amount > 0, False)]
    await events.publish(
        session,
        event_type,
        {
            "company_id": company_id,
            "source_id": adjustment.id,
            "event_date": adj_date,
            "adjustment_no": adj_no,
            "product_id": data.product_id,
            "order_id": data.order_id,
            "amount": abs(data.amount),
        },
    )
    await session.flush()
    return adjustment


async def void_adjustment(session: AsyncSession, adjustment_id: uuid.UUID) -> CostAdjustment:
    """Void a posted cost adjustment, reverse valuation impact & ledger entry (flush-only)."""
    require_permission("costing.adjustment.void")
    company_id = require_current_company_id()
    actor = require_current_actor()

    result = await session.execute(
        select(CostAdjustment)
        .where(CostAdjustment.id == adjustment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    adjustment = result.scalar_one_or_none()
    if adjustment is None:
        raise NotFoundError("CostAdjustment", adjustment_id)
    if adjustment.status == CostAdjustmentStatus.VOIDED:
        raise ConflictError(f"CostAdjustment {adjustment_id} is already voided")

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.COST_ADJUSTMENT,
        entity_id=adjustment.id,
        transition=WorkflowTransition.VOID,
        amount=abs(adjustment.amount),
    )

    summary = await get_or_lock_cost_summary(session, company_id, adjustment.product_id)
    move_type, value_delta, unit_cost = _apply_adjustment_to_summary(
        summary, adjustment.adjustment_type, -adjustment.amount, is_void=True
    )

    voided_at = datetime.now(timezone.utc)  # noqa: UP017
    adjustment.status = CostAdjustmentStatus.VOIDED
    adjustment.voided_at = voided_at
    adjustment.updated_by = actor.user_id

    move = CostMove(
        company_id=company_id,
        product_id=adjustment.product_id,
        move_type=move_type,
        qty_delta=_ZERO,
        value_delta=value_delta,
        unit_cost=unit_cost,
        resulting_qty=summary.tracked_qty,
        resulting_value=summary.total_cost_value,
        resulting_avg_cost=summary.moving_avg_cost,
        source_type=_ADJUSTMENT_VOIDED_SOURCE_TYPE,
        source_id=adjustment.id,
        reason=f"Void {adjustment.adjustment_no}"[:255],
        created_by=actor.user_id,
    )
    session.add(move)

    event_type = _ADJUSTMENT_POSTING_EVENTS[
        (adjustment.adjustment_type, adjustment.amount > 0, True)
    ]
    await events.publish(
        session,
        event_type,
        {
            "company_id": company_id,
            "source_id": adjustment.id,
            "event_date": voided_at.date(),
            "adjustment_no": adjustment.adjustment_no,
            "product_id": adjustment.product_id,
            "order_id": adjustment.order_id,
            "amount": abs(adjustment.amount),
        },
    )
    await session.flush()
    return adjustment


async def load_adjustment_unchecked(
    session: AsyncSession, adjustment_id: uuid.UUID
) -> CostAdjustment:
    result = await session.execute(
        select(CostAdjustment)
        .where(CostAdjustment.id == adjustment_id)
        .execution_options(populate_existing=True)
    )
    adj = result.scalar_one_or_none()
    if adj is None:
        raise NotFoundError("CostAdjustment", adjustment_id)
    return adj


async def get_adjustment(session: AsyncSession, adjustment_id: uuid.UUID) -> CostAdjustment:
    require_permission("costing.adjustment.read")
    return await load_adjustment_unchecked(session, adjustment_id)


async def list_adjustments(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
    adjustment_type: CostAdjustmentType | None = None,
) -> Sequence[CostAdjustment]:
    require_permission("costing.adjustment.read")
    stmt = select(CostAdjustment).order_by(CostAdjustment.adjustment_no)
    if product_id is not None:
        stmt = stmt.where(CostAdjustment.product_id == product_id)
    if adjustment_type is not None:
        stmt = stmt.where(CostAdjustment.adjustment_type == adjustment_type)
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Queries, Standard Cost Sync & Valuation Variance Report
# ---------------------------------------------------------------------------


async def list_cost_summaries(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
    limit: int | None = None,
    after_product_id: uuid.UUID | None = None,
) -> Sequence[ProductCostSummary]:
    """Explicit `company_id` filter — `ProductCostSummary` omits `TenantScopedMixin`."""
    require_permission("costing.summary.read")
    company_id = require_current_company_id()
    stmt = select(ProductCostSummary).where(ProductCostSummary.company_id == company_id)
    if product_id is not None:
        stmt = stmt.where(ProductCostSummary.product_id == product_id)
    if after_product_id is not None:
        stmt = stmt.where(ProductCostSummary.product_id > after_product_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt.order_by(ProductCostSummary.product_id))
    return result.scalars().all()


async def list_cost_moves(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
    limit: int | None = None,
    after_created_at: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> Sequence[CostMove]:
    require_permission("costing.move.read")
    stmt = select(CostMove).order_by(CostMove.created_at, CostMove.id)
    if product_id is not None:
        stmt = stmt.where(CostMove.product_id == product_id)
    if after_created_at is not None and after_id is not None:
        stmt = stmt.where(
            or_(
                CostMove.created_at > after_created_at,
                and_(CostMove.created_at == after_created_at, CostMove.id > after_id),
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def load_cost_summary_unchecked(
    session: AsyncSession, product_id: uuid.UUID
) -> ProductCostSummary:
    """Reload a `ProductCostSummary` row for the active company without RBAC check."""
    company_id = require_current_company_id()
    result = await session.execute(
        select(ProductCostSummary)
        .where(
            ProductCostSummary.company_id == company_id,
            ProductCostSummary.product_id == product_id,
        )
        .execution_options(populate_existing=True)
    )
    summary = result.scalar_one_or_none()
    if summary is None:
        raise NotFoundError("ProductCostSummary", product_id)
    return summary


async def sync_standard_cost(session: AsyncSession, product_id: uuid.UUID) -> ProductCostSummary:
    """Copy a product's moving-average unit cost into `products.standard_cost` (flush-only)."""
    require_permission("costing.adjustment.create")
    company_id = require_current_company_id()
    actor = require_current_actor()
    await _ensure_product_belongs_to_company(session, company_id, product_id)

    summary = await get_or_lock_cost_summary(session, company_id, product_id)
    if summary.tracked_qty <= 0 and summary.moving_avg_cost <= 0:
        raise DomainValidationError(
            f"Product {product_id} has no moving-average cost history to sync to standard_cost"
        )

    await session.execute(
        update(_PRODUCTS)
        .where(_PRODUCTS.c.id == product_id, _PRODUCTS.c.company_id == company_id)
        .values(
            standard_cost=summary.moving_avg_cost,
            updated_at=datetime.now(timezone.utc),  # noqa: UP017
            updated_by=actor.user_id,
        )
    )
    await session.flush()
    return summary


async def get_valuation_variance_report(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
) -> list[ValuationVarianceRow]:
    """Compare moving-average inventory valuation against `products.standard_cost`."""
    require_permission("costing.report.read")
    company_id = require_current_company_id()

    stmt = (
        select(
            _PRODUCTS.c.id.label("product_id"),
            _PRODUCTS.c.sku.label("sku"),
            _PRODUCTS.c.name.label("product_name"),
            _PRODUCTS.c.standard_cost.label("standard_cost"),
            func.coalesce(_STOCK_SUMMARY.c.on_hand, _ZERO).label("stock_on_hand"),
            func.coalesce(ProductCostSummary.tracked_qty, _ZERO).label("tracked_qty"),
            func.coalesce(ProductCostSummary.total_cost_value, _ZERO).label("total_cost_value"),
            func.coalesce(ProductCostSummary.moving_avg_cost, _ZERO).label("moving_avg_cost"),
        )
        .select_from(
            _PRODUCTS.outerjoin(
                _STOCK_SUMMARY,
                and_(
                    _STOCK_SUMMARY.c.product_id == _PRODUCTS.c.id,
                    _STOCK_SUMMARY.c.company_id == company_id,
                ),
            ).outerjoin(
                ProductCostSummary,
                and_(
                    ProductCostSummary.product_id == _PRODUCTS.c.id,
                    ProductCostSummary.company_id == company_id,
                ),
            )
        )
        .where(_PRODUCTS.c.company_id == company_id)
        .order_by(_PRODUCTS.c.sku)
    )
    if product_id is not None:
        stmt = stmt.where(_PRODUCTS.c.id == product_id)

    rows = (await session.execute(stmt)).all()
    report: list[ValuationVarianceRow] = []
    for row in rows:
        tracked_qty = _round_half_even_6dp(Decimal(row.tracked_qty))
        total_val = _round_half_even_6dp(Decimal(row.total_cost_value))
        mwa = _round_half_even_6dp(Decimal(row.moving_avg_cost))
        std_cost = _round_half_even_6dp(Decimal(row.standard_cost))
        std_val = _round_half_even_6dp(tracked_qty * std_cost)
        report.append(
            ValuationVarianceRow(
                product_id=row.product_id,
                sku=str(row.sku),
                product_name=str(row.product_name),
                stock_on_hand=_round_half_even_6dp(Decimal(row.stock_on_hand)),
                tracked_qty=tracked_qty,
                moving_avg_cost=mwa,
                total_cost_value=total_val,
                standard_cost=std_cost,
                standard_valuation=std_val,
                valuation_variance=_round_half_even_6dp(total_val - std_val),
                unit_cost_variance=_round_half_even_6dp(mwa - std_cost),
            )
        )
    return report
