"""mrp service layer — multi-level BOM, work orders, WIP subledger, & MRP engine (ADR-017).

Flush-only mutation cores mirror `sales`/`purchase`/`payables`/`costing`; router owns
commit + `IntegrityError -> 409 ConflictError` translation. Cross-module lookups read
`companies`, `products`, `stock_summary`, `product_cost_summaries`, `sales_orders`,
`sales_order_lines`, `purchase_orders`, and `purchase_order_lines` via lightweight
`sqlalchemy.table()` Core references with explicit `company_id` predicates
(`import-linter` compliant).
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import String, and_, cast, select
from sqlalchemy import column as sa_column
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import events, hooks
from app.core.authorization import require_current_actor, require_permission
from app.core.customfields import CustomFieldEntityType, validate_and_normalize_custom_data
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.hooks import HookContext
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import require_current_company_id
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowTransition,
    verify_workflow_approval,
)
from app.modules.mrp.events import (
    MRP_MATERIAL_ISSUED_EVENT_TYPE,
    MRP_MATERIAL_RETURNED_EVENT_TYPE,
    MRP_PRODUCTION_RECEIVED_EVENT_TYPE,
    MRP_WORK_ORDER_CONFIRMED_EVENT_TYPE,
)
from app.modules.mrp.models import (
    AMOUNT,
    Bom,
    BomLine,
    MrpSequence,
    WorkOrder,
    WorkOrderLine,
    WorkOrderStatus,
    WorkOrderTransaction,
    WorkOrderTxType,
)
from app.modules.mrp.schemas import (
    BomCreate,
    BomExplosionNode,
    BomExplosionRead,
    BomLineCreate,
    BomUpdate,
    MaterialIssueRequest,
    MaterialMoveLineInput,
    MaterialReturnRequest,
    MrpPlanRequest,
    MrpPlanResponse,
    MrpRequirementRow,
    ProductionReceiptRequest,
    WorkOrderCreate,
    WorkOrderUpdate,
)

MRP_WORK_ORDER_VALIDATE_CONFIRM = "mrp.work_order.validate_confirm"
hooks.define_hook_point(MRP_WORK_ORDER_VALIDATE_CONFIRM, "1.0.0")

_MONEY_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0.000000")
_ONE = Decimal("1.000000")
_MAX_BOM_DEPTH = 20

_COMPANIES = sa_table("companies", sa_column("id"))

_PRODUCTS = sa_table(
    "products",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("sku"),
    sa_column("name"),
    sa_column("uom_id"),
    sa_column("standard_cost", AMOUNT),
    sa_column("is_active"),
)

_STOCK_SUMMARY = sa_table(
    "stock_summary",
    sa_column("company_id"),
    sa_column("product_id"),
    sa_column("on_hand", AMOUNT),
)

_PRODUCT_COST_SUMMARIES = sa_table(
    "product_cost_summaries",
    sa_column("company_id"),
    sa_column("product_id"),
    sa_column("tracked_qty", AMOUNT),
    sa_column("total_cost_value", AMOUNT),
    sa_column("moving_avg_cost", AMOUNT),
)

_SALES_ORDERS = sa_table(
    "sales_orders",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("status"),
)

_SALES_ORDER_LINES = sa_table(
    "sales_order_lines",
    sa_column("company_id"),
    sa_column("order_id"),
    sa_column("product_id"),
    sa_column("qty", AMOUNT),
)

_PURCHASE_ORDERS = sa_table(
    "purchase_orders",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("status"),
)

_PURCHASE_ORDER_LINES = sa_table(
    "purchase_order_lines",
    sa_column("company_id"),
    sa_column("order_id"),
    sa_column("product_id"),
    sa_column("qty", AMOUNT),
)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


async def _lock_company_for_bom_graph(session: AsyncSession, company_id: uuid.UUID) -> None:
    await session.execute(
        select(_COMPANIES.c.id).where(_COMPANIES.c.id == company_id).with_for_update()
    )


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
            _PRODUCTS.c.is_active,
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


def _resolve_validated_uom(
    requested_uom_id: uuid.UUID | None, product_row: Any, *, label: str
) -> uuid.UUID:
    product_uom: uuid.UUID = product_row.uom_id
    if requested_uom_id is not None and requested_uom_id != product_uom:
        raise DomainValidationError(
            f"{label} uom_id {requested_uom_id} must match product's uom_id {product_uom}"
        )
    return product_uom


async def _allocate_wo_no(session: AsyncSession, company_id: uuid.UUID, year: int) -> str:
    await session.execute(
        pg_insert(MrpSequence)
        .values(company_id=company_id, year=year, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )
    res = await session.execute(
        select(MrpSequence)
        .where(MrpSequence.company_id == company_id, MrpSequence.year == year)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    seq = res.scalar_one()
    allocated = seq.next_no
    seq.next_no = allocated + 1
    return f"WO-{year:04d}-{allocated:06d}"


# ---------------------------------------------------------------------------
# BOM CRUD, Cycle Detection & Multi-Level Explosion
# ---------------------------------------------------------------------------


def _validate_bom_line_inputs(
    parent_product_id: uuid.UUID,
    lines_in: Sequence[BomLineCreate],
    products: dict[uuid.UUID, Any],
) -> list[tuple[int, uuid.UUID, Decimal, uuid.UUID, Decimal]]:
    seen_components: set[uuid.UUID] = set()
    validated: list[tuple[int, uuid.UUID, Decimal, uuid.UUID, Decimal]] = []
    for idx, line_in in enumerate(lines_in, start=1):
        comp_id = line_in.component_product_id
        if comp_id == parent_product_id:
            raise DomainValidationError(
                f"BOM self-cycle detected: product {parent_product_id} cannot contain itself"
            )
        if comp_id in seen_components:
            raise DomainValidationError(
                f"Duplicate component_product_id {comp_id} within a single BOM is not allowed"
            )
        seen_components.add(comp_id)
        comp_product = products[comp_id]
        comp_uom = _resolve_validated_uom(
            line_in.uom_id, comp_product, label=f"line {idx} component {comp_id}"
        )
        validated.append((idx, comp_id, line_in.qty_per, comp_uom, line_in.scrap_rate))
    return validated


async def _assert_no_bom_cycles(
    session: AsyncSession,
    *,
    parent_product_id: uuid.UUID,
    candidate_components: Sequence[uuid.UUID],
) -> None:
    """Ensure adding/activating `parent -> candidate_components` preserves a strict DAG."""
    active_boms = (
        (
            await session.execute(
                select(Bom).where(Bom.is_active.is_(True)).options(selectinload(Bom.lines))
            )
        )
        .scalars()
        .all()
    )
    adj: dict[uuid.UUID, list[uuid.UUID]] = {}
    for bom in active_boms:
        adj[bom.product_id] = [ln.component_product_id for ln in bom.lines]
    adj[parent_product_id] = list(candidate_components)

    # Color state: 0=unvisited, 1=visiting, 2=visited
    state: dict[uuid.UUID, int] = {}

    def _dfs(node: uuid.UUID, depth: int) -> None:
        if depth > _MAX_BOM_DEPTH:
            raise DomainValidationError(
                f"BOM hierarchy depth exceeds maximum supported depth ({_MAX_BOM_DEPTH})"
            )
        state[node] = 1
        for nxt in adj.get(node, ()):
            nxt_state = state.get(nxt, 0)
            if nxt_state == 1:
                raise DomainValidationError(
                    f"Circular BOM dependency detected involving product {nxt}"
                )
            if nxt_state == 0:
                _dfs(nxt, depth + 1)
        state[node] = 2

    _dfs(parent_product_id, 0)


async def create_bom(session: AsyncSession, data: BomCreate) -> Bom:
    """Create a Bill of Materials with cycle guard and custom fields (flush-only)."""
    require_permission("mrp.bom.create")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await _lock_company_for_bom_graph(session, company_id)

    product_ids = {data.product_id} | {ln.component_product_id for ln in data.lines}
    products = await _fetch_products(session, company_id, product_ids)
    parent_uom = _resolve_validated_uom(data.uom_id, products[data.product_id], label="BOM header")
    validated_lines = _validate_bom_line_inputs(data.product_id, data.lines, products)

    if data.is_active:
        await _assert_no_bom_cycles(
            session,
            parent_product_id=data.product_id,
            candidate_components=[c[1] for c in validated_lines],
        )

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.BOM,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    bom = Bom(
        company_id=company_id,
        code=data.code,
        product_id=data.product_id,
        uom_id=parent_uom,
        output_qty=data.output_qty,
        is_active=data.is_active,
        notes=data.notes,
        custom_data=normalized_custom_data,
        created_by=actor.user_id,
        updated_by=actor.user_id,
        lines=[
            BomLine(
                company_id=company_id,
                line_no=line_no,
                component_product_id=comp_id,
                qty_per=qty_per,
                uom_id=comp_uom,
                scrap_rate=scrap_rate,
            )
            for line_no, comp_id, qty_per, comp_uom, scrap_rate in validated_lines
        ],
    )
    session.add(bom)
    await session.flush()
    return bom


async def update_bom(session: AsyncSession, bom_id: uuid.UUID, data: BomUpdate) -> Bom:
    """Update an existing BOM (`is_active`, `notes`, `custom_data`, and/or draft recipe)."""
    require_permission("mrp.bom.update")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await _lock_company_for_bom_graph(session, company_id)

    bom = (
        await session.execute(
            select(Bom)
            .where(Bom.id == bom_id)
            .options(selectinload(Bom.lines))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if bom is None:
        raise NotFoundError("Bom", bom_id)

    structural_change = data.lines is not None or (
        data.output_qty is not None and data.output_qty != bom.output_qty
    )
    if structural_change:
        used_wo = (
            await session.execute(
                select(WorkOrder.id)
                .where(
                    WorkOrder.bom_id == bom.id,
                    WorkOrder.status != WorkOrderStatus.CANCELLED,
                )
                .limit(1)
            )
        ).first()
        if used_wo is not None:
            raise ConflictError(
                f"BOM {bom.code} is referenced by active/historical WorkOrders; "
                "deactivate this BOM and create a new BOM version instead"
            )

    if data.output_qty is not None:
        bom.output_qty = data.output_qty
    if data.notes is not None:
        bom.notes = data.notes
    if data.is_active is not None:
        bom.is_active = data.is_active

    if data.lines is not None:
        products = await _fetch_products(
            session, company_id, {ln.component_product_id for ln in data.lines}
        )
        validated_lines = _validate_bom_line_inputs(bom.product_id, data.lines, products)
        bom.lines.clear()
        await session.flush()
        bom.lines = [
            BomLine(
                company_id=company_id,
                line_no=line_no,
                component_product_id=comp_id,
                qty_per=qty_per,
                uom_id=comp_uom,
                scrap_rate=scrap_rate,
            )
            for line_no, comp_id, qty_per, comp_uom, scrap_rate in validated_lines
        ]

    if bom.is_active:
        await _assert_no_bom_cycles(
            session,
            parent_product_id=bom.product_id,
            candidate_components=[ln.component_product_id for ln in bom.lines],
        )

    if data.custom_data is not None:
        bom.custom_data = await validate_and_normalize_custom_data(
            session,
            entity_type=CustomFieldEntityType.BOM,
            raw_custom_data=data.custom_data,
            is_create=False,
            existing_custom_data=bom.custom_data,
        )

    bom.updated_by = actor.user_id
    await session.flush()
    return bom


async def load_bom_unchecked(session: AsyncSession, bom_id: uuid.UUID) -> Bom:
    result = await session.execute(
        select(Bom)
        .where(Bom.id == bom_id)
        .options(selectinload(Bom.lines))
        .execution_options(populate_existing=True)
    )
    bom = result.scalar_one_or_none()
    if bom is None:
        raise NotFoundError("Bom", bom_id)
    return bom


async def get_bom(session: AsyncSession, bom_id: uuid.UUID) -> Bom:
    require_permission("mrp.bom.read")
    return await load_bom_unchecked(session, bom_id)


async def list_boms(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
    active_only: bool = False,
) -> Sequence[Bom]:
    require_permission("mrp.bom.read")
    stmt = select(Bom).options(selectinload(Bom.lines)).order_by(Bom.code)
    if product_id is not None:
        stmt = stmt.where(Bom.product_id == product_id)
    if active_only:
        stmt = stmt.where(Bom.is_active.is_(True))
    return (await session.execute(stmt)).scalars().all()


async def _load_product_cost_lookup(
    session: AsyncSession, company_id: uuid.UUID
) -> tuple[dict[uuid.UUID, Any], dict[uuid.UUID, Decimal]]:
    prod_rows = (
        await session.execute(
            select(
                _PRODUCTS.c.id,
                _PRODUCTS.c.sku,
                _PRODUCTS.c.name,
                _PRODUCTS.c.uom_id,
                _PRODUCTS.c.standard_cost,
            ).where(_PRODUCTS.c.company_id == company_id)
        )
    ).all()
    products = {r.id: r for r in prod_rows}

    cost_rows = (
        await session.execute(
            select(
                _PRODUCT_COST_SUMMARIES.c.product_id,
                _PRODUCT_COST_SUMMARIES.c.tracked_qty,
                _PRODUCT_COST_SUMMARIES.c.moving_avg_cost,
            ).where(_PRODUCT_COST_SUMMARIES.c.company_id == company_id)
        )
    ).all()
    mwa_by_product = {
        r.product_id: Decimal(str(r.moving_avg_cost))
        for r in cost_rows
        if Decimal(str(r.tracked_qty)) > 0 or Decimal(str(r.moving_avg_cost)) > 0
    }
    return products, mwa_by_product


async def explode_bom(
    session: AsyncSession, bom_id: uuid.UUID, *, qty: Decimal | None = None
) -> BomExplosionRead:
    """Recursively explode a multi-level BOM with scrap factors and rolled-up costs."""
    require_permission("mrp.bom.read")
    company_id = require_current_company_id()

    root_bom = await load_bom_unchecked(session, bom_id)
    req_qty = _round_half_even_6dp(qty) if qty is not None else root_bom.output_qty
    if req_qty <= 0:
        raise DomainValidationError("Explosion qty must be greater than 0")

    active_boms = (
        (
            await session.execute(
                select(Bom).where(Bom.is_active.is_(True)).options(selectinload(Bom.lines))
            )
        )
        .scalars()
        .all()
    )
    active_by_product: dict[uuid.UUID, Bom] = {b.product_id: b for b in active_boms}
    products, mwa_by_product = await _load_product_cost_lookup(session, company_id)

    flat_nodes: list[BomExplosionNode] = []

    def _walk(
        bom: Bom, parent_qty: Decimal, level: int, ancestor_products: set[uuid.UUID]
    ) -> tuple[Decimal, Decimal]:
        if level > _MAX_BOM_DEPTH:
            raise DomainValidationError("BOM explosion exceeded maximum hierarchy depth")
        total_std = _ZERO
        total_rolled = _ZERO
        for line in bom.lines:
            comp_id = line.component_product_id
            if comp_id in ancestor_products:
                raise DomainValidationError(
                    f"Circular BOM dependency detected involving product {comp_id}"
                )
            prod = products[comp_id]
            eff_qty = _round_half_even_6dp(
                (parent_qty * line.qty_per / bom.output_qty) * (_ONE + line.scrap_rate)
            )
            std_unit = _round_half_even_6dp(Decimal(str(prod.standard_cost)))
            mwa_unit = _round_half_even_6dp(mwa_by_product.get(comp_id, std_unit))
            sub_bom = active_by_product.get(comp_id)

            node_idx = len(flat_nodes)
            placeholder = BomExplosionNode(
                level=level,
                bom_id=bom.id,
                parent_product_id=bom.product_id,
                component_product_id=comp_id,
                sku=str(prod.sku),
                product_name=str(prod.name),
                uom_id=line.uom_id,
                qty_per=line.qty_per,
                scrap_rate=line.scrap_rate,
                effective_qty=eff_qty,
                is_subassembly=sub_bom is not None,
                sub_bom_id=sub_bom.id if sub_bom is not None else None,
                standard_unit_cost=std_unit,
                moving_avg_cost=mwa_unit,
                rolled_up_unit_cost=mwa_unit,
                extended_cost=_ZERO,
            )
            flat_nodes.append(placeholder)

            if sub_bom is not None:
                sub_std, sub_rolled = _walk(
                    sub_bom, eff_qty, level + 1, ancestor_products | {comp_id}
                )
                rolled_unit = (
                    _round_half_even_6dp(sub_rolled / eff_qty) if eff_qty > 0 else mwa_unit
                )
                ext_cost = sub_rolled
                std_ext = sub_std
            else:
                rolled_unit = mwa_unit
                ext_cost = _round_half_even_6dp(eff_qty * rolled_unit)
                std_ext = _round_half_even_6dp(eff_qty * std_unit)

            flat_nodes[node_idx] = placeholder.model_copy(
                update={"rolled_up_unit_cost": rolled_unit, "extended_cost": ext_cost}
            )
            total_std = _round_half_even_6dp(total_std + std_ext)
            total_rolled = _round_half_even_6dp(total_rolled + ext_cost)
        return total_std, total_rolled

    total_std_cost, total_rolled_cost = _walk(root_bom, req_qty, 1, {root_bom.product_id})
    root_prod = products[root_bom.product_id]
    unit_rolled = _round_half_even_6dp(total_rolled_cost / req_qty)
    return BomExplosionRead(
        bom_id=root_bom.id,
        code=root_bom.code,
        product_id=root_bom.product_id,
        sku=str(root_prod.sku),
        product_name=str(root_prod.name),
        requested_qty=req_qty,
        total_standard_material_cost=total_std_cost,
        total_rolled_up_cost=total_rolled_cost,
        unit_rolled_up_cost=unit_rolled,
        nodes=flat_nodes,
    )


# ---------------------------------------------------------------------------
# Work Order Lifecycle (Draft -> Confirmed -> In Progress -> Completed / Cancelled)
# ---------------------------------------------------------------------------


def _build_wo_lines(company_id: uuid.UUID, bom: Bom, planned_qty: Decimal) -> list[WorkOrderLine]:
    lines: list[WorkOrderLine] = []
    for line in bom.lines:
        req_qty = _round_half_even_6dp(
            (planned_qty * line.qty_per / bom.output_qty) * (_ONE + line.scrap_rate)
        )
        if req_qty <= 0:
            raise DomainValidationError(
                f"Computed required_qty for component {line.component_product_id} rounded to 0"
            )
        lines.append(
            WorkOrderLine(
                company_id=company_id,
                line_no=line.line_no,
                component_product_id=line.component_product_id,
                uom_id=line.uom_id,
                required_qty=req_qty,
            )
        )
    return lines


async def create_work_order(session: AsyncSession, data: WorkOrderCreate) -> WorkOrder:
    """Create a DRAFT work order exploding component lines from the target BOM (flush-only)."""
    require_permission("mrp.work_order.create")
    company_id = require_current_company_id()
    actor = require_current_actor()

    if data.bom_id is not None:
        bom_res = await session.execute(
            select(Bom).where(Bom.id == data.bom_id).options(selectinload(Bom.lines))
        )
        bom = bom_res.scalar_one_or_none()
        if bom is None:
            raise DomainValidationError(f"bom_id {data.bom_id} not found or not in this company")
        if data.product_id is not None and bom.product_id != data.product_id:
            raise DomainValidationError(
                f"BOM {data.bom_id} builds product {bom.product_id}, not {data.product_id}"
            )
        if not bom.is_active:
            raise DomainValidationError(f"BOM {bom.code} is inactive; cannot create work order")
    else:
        assert data.product_id is not None
        bom_res = await session.execute(
            select(Bom)
            .where(Bom.product_id == data.product_id, Bom.is_active.is_(True))
            .options(selectinload(Bom.lines))
        )
        bom = bom_res.scalar_one_or_none()
        if bom is None:
            raise DomainValidationError(f"No active BOM found for product {data.product_id}")

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.WORK_ORDER,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    year = datetime.now(timezone.utc).year  # noqa: UP017
    wo_no = await _allocate_wo_no(session, company_id, year)

    wo = WorkOrder(
        company_id=company_id,
        wo_no=wo_no,
        bom_id=bom.id,
        product_id=bom.product_id,
        uom_id=bom.uom_id,
        status=WorkOrderStatus.DRAFT,
        planned_qty=data.planned_qty,
        notes=data.notes,
        custom_data=normalized_custom_data,
        created_by=actor.user_id,
        updated_by=actor.user_id,
        lines=_build_wo_lines(company_id, bom, data.planned_qty),
    )
    session.add(wo)
    await session.flush()
    return wo


async def update_work_order(
    session: AsyncSession, wo_id: uuid.UUID, data: WorkOrderUpdate
) -> WorkOrder:
    """Update a DRAFT work order (`planned_qty`, `notes`, `custom_data`; flush-only)."""
    require_permission("mrp.work_order.update")
    company_id = require_current_company_id()
    actor = require_current_actor()

    wo = (
        await session.execute(
            select(WorkOrder)
            .where(WorkOrder.id == wo_id)
            .options(selectinload(WorkOrder.lines))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if wo is None:
        raise NotFoundError("WorkOrder", wo_id)
    if wo.status is not WorkOrderStatus.DRAFT:
        raise ConflictError(
            f"WorkOrder {wo_id} is in status {wo.status.value}; "
            "only draft work orders can be edited"
        )

    if data.notes is not None:
        wo.notes = data.notes
    if data.planned_qty is not None and data.planned_qty != wo.planned_qty:
        bom = await load_bom_unchecked(session, wo.bom_id)
        wo.planned_qty = data.planned_qty
        new_lines = _build_wo_lines(company_id, bom, data.planned_qty)
        wo.lines.clear()
        await session.flush()
        wo.lines = new_lines
    if data.custom_data is not None:
        wo.custom_data = await validate_and_normalize_custom_data(
            session,
            entity_type=CustomFieldEntityType.WORK_ORDER,
            raw_custom_data=data.custom_data,
            is_create=False,
            existing_custom_data=wo.custom_data,
        )

    wo.updated_by = actor.user_id
    await session.flush()
    return wo


async def confirm_work_order(session: AsyncSession, wo_id: uuid.UUID) -> WorkOrder:
    """Confirm a DRAFT work order after snapshots, plugin hook, and workflow gate."""
    require_permission("mrp.work_order.confirm")
    company_id = require_current_company_id()
    actor = require_current_actor()

    wo = (
        await session.execute(
            select(WorkOrder)
            .where(WorkOrder.id == wo_id)
            .options(selectinload(WorkOrder.lines))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if wo is None:
        raise NotFoundError("WorkOrder", wo_id)
    if wo.status is not WorkOrderStatus.DRAFT:
        raise ConflictError(
            f"WorkOrder {wo_id} is not a draft (status={wo.status.value}); cannot confirm"
        )
    if not wo.lines:
        raise DomainValidationError(f"WorkOrder {wo_id} has no material lines")

    products, mwa_by_product = await _load_product_cost_lookup(session, company_id)
    parent_prod = products.get(wo.product_id)
    if parent_prod is None:
        raise DomainValidationError(f"WorkOrder product {wo.product_id} not found")
    wo.snapshot_sku = str(parent_prod.sku)
    wo.snapshot_product_name = str(parent_prod.name)

    estimated_total = _ZERO
    for line in wo.lines:
        comp_prod = products.get(line.component_product_id)
        if comp_prod is None:
            raise DomainValidationError(f"Component product {line.component_product_id} not found")
        est_unit = _round_half_even_6dp(
            mwa_by_product.get(line.component_product_id, Decimal(str(comp_prod.standard_cost)))
        )
        line.snapshot_sku = str(comp_prod.sku)
        line.snapshot_product_name = str(comp_prod.name)
        line.estimated_unit_cost = est_unit
        estimated_total = _round_half_even_6dp(
            estimated_total + _round_half_even_6dp(line.required_qty * est_unit)
        )

    await hooks.run(
        MRP_WORK_ORDER_VALIDATE_CONFIRM,
        session,
        HookContext(
            company_id=company_id,
            order_id=wo.id,
            order_no=wo.wo_no,
            customer_id=wo.id,
            total=estimated_total,
            product_id=wo.product_id,
        ),
    )

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.WORK_ORDER,
        entity_id=wo.id,
        transition=WorkflowTransition.CONFIRM,
        amount=estimated_total,
    )

    wo.status = WorkOrderStatus.CONFIRMED
    wo.confirmed_at = datetime.now(timezone.utc)  # noqa: UP017
    wo.updated_by = actor.user_id

    await events.publish(
        session,
        MRP_WORK_ORDER_CONFIRMED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": wo.id,
            "wo_no": wo.wo_no,
            "product_id": wo.product_id,
            "planned_qty": wo.planned_qty,
            "estimated_total": estimated_total,
        },
    )
    await session.flush()
    return wo


async def cancel_work_order(session: AsyncSession, wo_id: uuid.UUID) -> WorkOrder:
    """Cancel a DRAFT or CONFIRMED work order if it has zero net WIP/completed activity."""
    require_permission("mrp.work_order.cancel")
    actor = require_current_actor()

    wo = (
        await session.execute(
            select(WorkOrder)
            .where(WorkOrder.id == wo_id)
            .options(selectinload(WorkOrder.lines))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if wo is None:
        raise NotFoundError("WorkOrder", wo_id)
    if wo.status is WorkOrderStatus.CANCELLED:
        raise ConflictError(f"WorkOrder {wo_id} is already cancelled")
    if wo.status is WorkOrderStatus.COMPLETED:
        raise ConflictError(f"WorkOrder {wo_id} is already completed and cannot be cancelled")
    if wo.completed_qty > 0 or wo.wip_balance > 0:
        raise ConflictError(
            f"WorkOrder {wo_id} has completed_qty={wo.completed_qty} or "
            f"wip_balance={wo.wip_balance}; return materials before cancelling"
        )
    if any((ln.issued_qty - ln.returned_qty) > 0 for ln in wo.lines):
        raise ConflictError(
            f"WorkOrder {wo_id} still has unreturned material issues; return materials first"
        )

    company_id = require_current_company_id()
    products, mwa_by_product = await _load_product_cost_lookup(session, company_id)
    cancel_amount = _ZERO
    for ln in wo.lines:
        unit = (
            ln.estimated_unit_cost
            if wo.status is not WorkOrderStatus.DRAFT
            else mwa_by_product.get(
                ln.component_product_id,
                Decimal(str(products[ln.component_product_id].standard_cost)),
            )
        )
        cancel_amount = _round_half_even_6dp(
            cancel_amount + _round_half_even_6dp(ln.required_qty * _round_half_even_6dp(unit))
        )

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.WORK_ORDER,
        entity_id=wo.id,
        transition=WorkflowTransition.CANCEL,
        amount=cancel_amount,
    )

    wo.status = WorkOrderStatus.CANCELLED
    wo.cancelled_at = datetime.now(timezone.utc)  # noqa: UP017
    wo.updated_by = actor.user_id
    await session.flush()
    return wo


# ---------------------------------------------------------------------------
# Shop-Floor Execution (Issue / Return / Production Receipt + WIP Costing)
# ---------------------------------------------------------------------------


def _fingerprint_material_move(
    tx_type: WorkOrderTxType, lines: Sequence[MaterialMoveLineInput] | None
) -> str:
    if lines is None:
        raw = f"{tx_type.value}|AUTO"
    else:
        pairs = sorted((str(ln.component_product_id), str(ln.qty)) for ln in lines)
        raw = f"{tx_type.value}|" + "|".join(f"{pid}:{qty}" for pid, qty in pairs)
    return hashlib.sha256(raw.encode()).hexdigest()


def _fingerprint_receipt(qty: Decimal, mark_completed: bool) -> str:
    raw = f"{WorkOrderTxType.PRODUCTION_RECEIPT.value}|{qty}|{mark_completed}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _check_idempotent_wo_tx(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    wo_id: uuid.UUID,
    tx_type: WorkOrderTxType,
    request_ref: str,
    fingerprint: str,
) -> WorkOrderTransaction | None:
    existing = (
        await session.execute(
            select(WorkOrderTransaction).where(
                WorkOrderTransaction.company_id == company_id,
                WorkOrderTransaction.work_order_id == wo_id,
                WorkOrderTransaction.request_ref == request_ref,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.tx_type != tx_type or existing.request_fingerprint != fingerprint:
        raise ConflictError(
            f"request_ref {request_ref!r} on WorkOrder {wo_id} was already used "
            "with a different transaction type or payload"
        )
    return existing


async def _lock_cost_summaries_for_products(
    session: AsyncSession, company_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not product_ids:
        return {}
    await acquire_company_advisory_lock(session, company_id, exclusive=False)
    sorted_pids = sorted(set(product_ids))
    rows = (
        await session.execute(
            select(
                _PRODUCT_COST_SUMMARIES.c.product_id,
                _PRODUCT_COST_SUMMARIES.c.tracked_qty,
                _PRODUCT_COST_SUMMARIES.c.total_cost_value,
                _PRODUCT_COST_SUMMARIES.c.moving_avg_cost,
            )
            .where(
                _PRODUCT_COST_SUMMARIES.c.company_id == company_id,
                _PRODUCT_COST_SUMMARIES.c.product_id.in_(sorted_pids),
            )
            .order_by(_PRODUCT_COST_SUMMARIES.c.product_id)
            .with_for_update()
        )
    ).all()
    return {r.product_id: r for r in rows}


def _compute_issue_line_cost(
    qty: Decimal, summary_row: Any | None, standard_cost: Decimal
) -> tuple[Decimal, Decimal]:
    if summary_row is None or Decimal(str(summary_row.tracked_qty)) <= 0:
        line_cost = _round_half_even_6dp(qty * standard_cost)
        return standard_cost, line_cost

    tracked_qty = Decimal(str(summary_row.tracked_qty))
    total_val = Decimal(str(summary_row.total_cost_value))
    mwa_cost = Decimal(str(summary_row.moving_avg_cost))

    if tracked_qty > qty:
        line_cost = min(total_val, _round_half_even_6dp(qty * mwa_cost))
        return mwa_cost, line_cost
    if tracked_qty == qty:
        return mwa_cost, total_val

    untracked_qty = qty - tracked_qty
    line_cost = _round_half_even_6dp(
        total_val + _round_half_even_6dp(untracked_qty * standard_cost)
    )
    eff_unit = _round_half_even_6dp(line_cost / qty)
    return eff_unit, line_cost


async def _lock_work_order_for_shop_floor(
    session: AsyncSession, company_id: uuid.UUID, wo_id: uuid.UUID
) -> WorkOrder:
    await acquire_company_advisory_lock(session, company_id, exclusive=False)
    wo = (
        await session.execute(
            select(WorkOrder)
            .where(WorkOrder.id == wo_id)
            .options(selectinload(WorkOrder.lines))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if wo is None:
        raise NotFoundError("WorkOrder", wo_id)
    return wo


async def issue_materials(
    session: AsyncSession, wo_id: uuid.UUID, data: MaterialIssueRequest
) -> WorkOrder:
    """Issue components into `1310 Work in Process` (idempotent by `request_ref`)."""
    require_permission("mrp.work_order.issue")
    company_id = require_current_company_id()
    actor = require_current_actor()

    wo = await _lock_work_order_for_shop_floor(session, company_id, wo_id)

    fingerprint = _fingerprint_material_move(WorkOrderTxType.MATERIAL_ISSUE, data.lines)
    replay = await _check_idempotent_wo_tx(
        session,
        company_id=company_id,
        wo_id=wo.id,
        tx_type=WorkOrderTxType.MATERIAL_ISSUE,
        request_ref=data.request_ref,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return wo

    if wo.status not in (WorkOrderStatus.CONFIRMED, WorkOrderStatus.IN_PROGRESS):
        raise ConflictError(f"WorkOrder {wo_id} in status {wo.status.value} cannot issue materials")

    lines_by_comp = {ln.component_product_id: ln for ln in wo.lines}
    req_qty_by_comp: dict[uuid.UUID, Decimal] = {}
    if data.lines is None:
        for ln in wo.lines:
            remaining = _round_half_even_6dp(ln.required_qty - (ln.issued_qty - ln.returned_qty))
            if remaining > 0:
                req_qty_by_comp[ln.component_product_id] = remaining
    else:
        for item in data.lines:
            if item.component_product_id not in lines_by_comp:
                raise DomainValidationError(
                    f"component_product_id {item.component_product_id} is not on WorkOrder {wo_id}"
                )
            if item.component_product_id in req_qty_by_comp:
                raise DomainValidationError(
                    f"Duplicate component_product_id {item.component_product_id} in issue request"
                )
            req_qty_by_comp[item.component_product_id] = item.qty

    if not req_qty_by_comp:
        raise ConflictError(f"WorkOrder {wo_id} has no remaining component quantities to issue")

    sorted_pids = sorted(req_qty_by_comp)
    summaries = await _lock_cost_summaries_for_products(session, company_id, sorted_pids)
    products = await _fetch_products(session, company_id, set(sorted_pids))

    event_lines: list[dict[str, Any]] = []
    total_cost = _ZERO
    for pid in sorted_pids:
        qty = req_qty_by_comp[pid]
        ln = lines_by_comp[pid]
        std_cost = _round_half_even_6dp(Decimal(str(products[pid].standard_cost)))
        unit_cost, line_cost = _compute_issue_line_cost(qty, summaries.get(pid), std_cost)

        ln.issued_qty = _round_half_even_6dp(ln.issued_qty + qty)
        ln.issued_cost = _round_half_even_6dp(ln.issued_cost + line_cost)
        total_cost = _round_half_even_6dp(total_cost + line_cost)
        event_lines.append(
            {
                "product_id": str(pid),
                "qty": str(qty),
                "unit_cost": str(unit_cost),
                "line_cost": str(line_cost),
            }
        )

    now_utc = datetime.now(timezone.utc)  # noqa: UP017
    wo.wip_balance = _round_half_even_6dp(wo.wip_balance + total_cost)
    if wo.status is WorkOrderStatus.CONFIRMED:
        wo.status = WorkOrderStatus.IN_PROGRESS
        wo.started_at = now_utc
    wo.updated_by = actor.user_id

    tx = WorkOrderTransaction(
        company_id=company_id,
        work_order_id=wo.id,
        tx_type=WorkOrderTxType.MATERIAL_ISSUE,
        request_ref=data.request_ref,
        request_fingerprint=fingerprint,
        total_cost=total_cost,
        lines_payload=event_lines,
        created_by=actor.user_id,
    )
    session.add(tx)
    await session.flush()

    await events.publish(
        session,
        MRP_MATERIAL_ISSUED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": tx.id,
            "wo_id": wo.id,
            "wo_no": wo.wo_no,
            "event_date": now_utc.date(),
            "lines": event_lines,
            "total_cost": total_cost,
        },
    )
    await session.flush()
    return wo


def _apply_return_lines(
    wo: WorkOrder, data: MaterialReturnRequest
) -> tuple[list[dict[str, Any]], Decimal]:
    lines_by_comp = {ln.component_product_id: ln for ln in wo.lines}
    consumed_min_by_comp: dict[uuid.UUID, Decimal] = {
        ln.component_product_id: (
            _round_half_even_6dp(ln.required_qty * wo.completed_qty / wo.planned_qty)
            if wo.completed_qty > 0
            else _ZERO
        )
        for ln in wo.lines
    }
    req_qty_by_comp: dict[uuid.UUID, Decimal] = {}
    for item in data.lines:
        if item.component_product_id not in lines_by_comp:
            raise DomainValidationError(
                f"component_product_id {item.component_product_id} is not on WorkOrder {wo.id}"
            )
        if item.component_product_id in req_qty_by_comp:
            raise DomainValidationError(
                f"Duplicate component_product_id {item.component_product_id} in return request"
            )
        ln = lines_by_comp[item.component_product_id]
        net_qty = _round_half_even_6dp(ln.issued_qty - ln.returned_qty)
        consumed_min = consumed_min_by_comp[item.component_product_id]
        max_returnable = max(_ZERO, _round_half_even_6dp(net_qty - consumed_min))
        if item.qty > max_returnable:
            raise ConflictError(
                f"Cannot return {item.qty} of component {item.component_product_id}: "
                f"only {max_returnable} unconsumed issued qty remains"
            )
        req_qty_by_comp[item.component_product_id] = item.qty

    sorted_pids = sorted(req_qty_by_comp)
    event_lines: list[dict[str, Any]] = []
    pre_net_costs: dict[uuid.UUID, Decimal] = {}
    total_cost = _ZERO
    for pid in sorted_pids:
        qty = req_qty_by_comp[pid]
        ln = lines_by_comp[pid]
        net_qty = _round_half_even_6dp(ln.issued_qty - ln.returned_qty)
        net_cost = _round_half_even_6dp(ln.issued_cost - ln.returned_cost)
        pre_net_costs[pid] = net_cost
        if qty == net_qty:
            line_cost = net_cost
        else:
            avg_net = net_cost / net_qty
            line_cost = min(net_cost, _round_half_even_6dp(qty * avg_net))
        unit_cost = _round_half_even_6dp(line_cost / qty)

        ln.returned_qty = _round_half_even_6dp(ln.returned_qty + qty)
        ln.returned_cost = _round_half_even_6dp(ln.returned_cost + line_cost)
        total_cost = _round_half_even_6dp(total_cost + line_cost)
        event_lines.append(
            {
                "product_id": str(pid),
                "qty": str(qty),
                "unit_cost": str(unit_cost),
                "line_cost": str(line_cost),
            }
        )

    all_unconsumed_cleared = all(
        _round_half_even_6dp(
            (ln.issued_qty - ln.returned_qty) - consumed_min_by_comp[ln.component_product_id]
        )
        <= _ZERO
        for ln in wo.lines
    )
    if all_unconsumed_cleared:
        residual = _round_half_even_6dp(wo.wip_balance - total_cost)
        if residual != _ZERO and abs(residual) <= _MONEY_QUANTUM * max(1, len(wo.lines)):
            for idx in range(len(sorted_pids) - 1, -1, -1):
                pid = sorted_pids[idx]
                ln = lines_by_comp[pid]
                old_lc = Decimal(str(event_lines[idx]["line_cost"]))
                adj_lc = _round_half_even_6dp(old_lc + residual)
                if _ZERO <= adj_lc <= pre_net_costs[pid]:
                    ln.returned_cost = _round_half_even_6dp(ln.returned_cost + residual)
                    total_cost = _round_half_even_6dp(total_cost + residual)
                    qty = req_qty_by_comp[pid]
                    event_lines[idx]["line_cost"] = str(adj_lc)
                    event_lines[idx]["unit_cost"] = str(_round_half_even_6dp(adj_lc / qty))
                    break

    if total_cost > wo.wip_balance:
        raise ConflictError(
            f"Return cost {total_cost} exceeds remaining WorkOrder wip_balance {wo.wip_balance}"
        )
    return event_lines, total_cost


async def return_materials(
    session: AsyncSession, wo_id: uuid.UUID, data: MaterialReturnRequest
) -> WorkOrder:
    """Return unconsumed issued components from WIP back into `1300 Inventory`."""
    require_permission("mrp.work_order.issue")
    company_id = require_current_company_id()
    actor = require_current_actor()

    wo = await _lock_work_order_for_shop_floor(session, company_id, wo_id)

    fingerprint = _fingerprint_material_move(WorkOrderTxType.MATERIAL_RETURN, data.lines)
    replay = await _check_idempotent_wo_tx(
        session,
        company_id=company_id,
        wo_id=wo.id,
        tx_type=WorkOrderTxType.MATERIAL_RETURN,
        request_ref=data.request_ref,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return wo

    if wo.status not in (WorkOrderStatus.CONFIRMED, WorkOrderStatus.IN_PROGRESS):
        raise ConflictError(
            f"WorkOrder {wo_id} in status {wo.status.value} cannot return materials"
        )

    event_lines, total_cost = _apply_return_lines(wo, data)

    now_utc = datetime.now(timezone.utc)  # noqa: UP017
    wo.wip_balance = _round_half_even_6dp(wo.wip_balance - total_cost)
    wo.updated_by = actor.user_id

    tx = WorkOrderTransaction(
        company_id=company_id,
        work_order_id=wo.id,
        tx_type=WorkOrderTxType.MATERIAL_RETURN,
        request_ref=data.request_ref,
        request_fingerprint=fingerprint,
        total_cost=total_cost,
        lines_payload=event_lines,
        created_by=actor.user_id,
    )
    session.add(tx)
    await session.flush()

    await events.publish(
        session,
        MRP_MATERIAL_RETURNED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": tx.id,
            "wo_id": wo.id,
            "wo_no": wo.wo_no,
            "event_date": now_utc.date(),
            "lines": event_lines,
            "total_cost": total_cost,
        },
    )
    await session.flush()
    return wo


async def receive_production(
    session: AsyncSession, wo_id: uuid.UUID, data: ProductionReceiptRequest
) -> WorkOrder:
    """Receive finished goods from WIP into `1300 Inventory` and capitalize MWA cost."""
    require_permission("mrp.work_order.receive")
    company_id = require_current_company_id()
    actor = require_current_actor()

    wo = await _lock_work_order_for_shop_floor(session, company_id, wo_id)

    fingerprint = _fingerprint_receipt(data.qty, data.mark_completed)
    replay = await _check_idempotent_wo_tx(
        session,
        company_id=company_id,
        wo_id=wo.id,
        tx_type=WorkOrderTxType.PRODUCTION_RECEIPT,
        request_ref=data.request_ref,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return wo

    if wo.status not in (WorkOrderStatus.CONFIRMED, WorkOrderStatus.IN_PROGRESS):
        raise ConflictError(
            f"WorkOrder {wo_id} in status {wo.status.value} cannot receive production"
        )
    if all((ln.issued_qty - ln.returned_qty) <= 0 for ln in wo.lines):
        raise ConflictError(
            f"WorkOrder {wo_id} has no net issued materials; issue materials before receiving"
        )
    if wo.completed_qty + data.qty > wo.planned_qty:
        raise ConflictError(
            f"Receipt qty {data.qty} would exceed planned_qty {wo.planned_qty} "
            f"(completed={wo.completed_qty})"
        )

    remaining_planned = wo.planned_qty - wo.completed_qty
    is_final = data.mark_completed or (wo.completed_qty + data.qty == wo.planned_qty)
    if is_final:
        absorbed_cost = wo.wip_balance
    else:
        absorbed_cost = min(
            wo.wip_balance,
            _round_half_even_6dp(wo.wip_balance * data.qty / remaining_planned),
        )
    unit_cost = _round_half_even_6dp(absorbed_cost / data.qty)

    now_utc = datetime.now(timezone.utc)  # noqa: UP017
    wo.completed_qty = _round_half_even_6dp(wo.completed_qty + data.qty)
    wo.wip_balance = _round_half_even_6dp(wo.wip_balance - absorbed_cost)
    if is_final:
        wo.status = WorkOrderStatus.COMPLETED
        wo.completed_at = now_utc
    elif wo.status is WorkOrderStatus.CONFIRMED:
        wo.status = WorkOrderStatus.IN_PROGRESS
        wo.started_at = now_utc
    wo.updated_by = actor.user_id

    tx_line = [
        {
            "product_id": str(wo.product_id),
            "qty": str(data.qty),
            "unit_cost": str(unit_cost),
            "total_cost": str(absorbed_cost),
        }
    ]
    tx = WorkOrderTransaction(
        company_id=company_id,
        work_order_id=wo.id,
        tx_type=WorkOrderTxType.PRODUCTION_RECEIPT,
        request_ref=data.request_ref,
        request_fingerprint=fingerprint,
        total_cost=absorbed_cost,
        received_qty=data.qty,
        lines_payload=tx_line,
        created_by=actor.user_id,
    )
    session.add(tx)
    await session.flush()

    await events.publish(
        session,
        MRP_PRODUCTION_RECEIVED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": tx.id,
            "wo_id": wo.id,
            "wo_no": wo.wo_no,
            "product_id": wo.product_id,
            "qty": data.qty,
            "unit_cost": unit_cost,
            "total_cost": absorbed_cost,
            "received_at": now_utc,
        },
    )
    await session.flush()
    return wo


async def load_work_order_unchecked(session: AsyncSession, wo_id: uuid.UUID) -> WorkOrder:
    result = await session.execute(
        select(WorkOrder)
        .where(WorkOrder.id == wo_id)
        .options(selectinload(WorkOrder.lines))
        .execution_options(populate_existing=True)
    )
    wo = result.scalar_one_or_none()
    if wo is None:
        raise NotFoundError("WorkOrder", wo_id)
    return wo


async def get_work_order(session: AsyncSession, wo_id: uuid.UUID) -> WorkOrder:
    require_permission("mrp.work_order.read")
    return await load_work_order_unchecked(session, wo_id)


async def list_work_orders(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None = None,
    status: WorkOrderStatus | None = None,
) -> Sequence[WorkOrder]:
    require_permission("mrp.work_order.read")
    stmt = select(WorkOrder).options(selectinload(WorkOrder.lines)).order_by(WorkOrder.wo_no)
    if product_id is not None:
        stmt = stmt.where(WorkOrder.product_id == product_id)
    if status is not None:
        stmt = stmt.where(WorkOrder.status == status)
    return (await session.execute(stmt)).scalars().all()


async def list_work_order_transactions(
    session: AsyncSession, wo_id: uuid.UUID
) -> Sequence[WorkOrderTransaction]:
    require_permission("mrp.work_order.read")
    await load_work_order_unchecked(session, wo_id)
    stmt = (
        select(WorkOrderTransaction)
        .where(WorkOrderTransaction.work_order_id == wo_id)
        .order_by(WorkOrderTransaction.created_at, WorkOrderTransaction.id)
    )
    return (await session.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------------
# Topological Multi-Level MRP Net-Requirements Planning Engine
# ---------------------------------------------------------------------------


async def _gather_mrp_supply_and_allocations(
    session: AsyncSession, company_id: uuid.UUID, data: MrpPlanRequest
) -> tuple[
    dict[uuid.UUID, Decimal],
    dict[uuid.UUID, Decimal],
    dict[uuid.UUID, Decimal],
    dict[uuid.UUID, Decimal],
]:
    stock_rows = (
        await session.execute(
            select(_STOCK_SUMMARY.c.product_id, _STOCK_SUMMARY.c.on_hand).where(
                _STOCK_SUMMARY.c.company_id == company_id
            )
        )
    ).all()
    on_hand: dict[uuid.UUID, Decimal] = {
        r.product_id: _round_half_even_6dp(Decimal(str(r.on_hand))) for r in stock_rows
    }

    gross: dict[uuid.UUID, Decimal] = defaultdict(lambda: _ZERO)
    for item in data.demands:
        gross[item.product_id] = _round_half_even_6dp(gross[item.product_id] + item.qty)

    if data.include_open_sales_orders:
        so_rows = (
            await session.execute(
                select(_SALES_ORDER_LINES.c.product_id, _SALES_ORDER_LINES.c.qty)
                .select_from(
                    _SALES_ORDER_LINES.join(
                        _SALES_ORDERS,
                        and_(
                            _SALES_ORDERS.c.id == _SALES_ORDER_LINES.c.order_id,
                            _SALES_ORDERS.c.company_id == company_id,
                        ),
                    )
                )
                .where(
                    _SALES_ORDER_LINES.c.company_id == company_id,
                    cast(_SALES_ORDERS.c.status, String) == "CONFIRMED",
                )
            )
        ).all()
        for r in so_rows:
            gross[r.product_id] = _round_half_even_6dp(
                gross[r.product_id] + _round_half_even_6dp(Decimal(str(r.qty)))
            )

    scheduled: dict[uuid.UUID, Decimal] = defaultdict(lambda: _ZERO)
    if data.include_open_purchase_orders:
        po_rows = (
            await session.execute(
                select(_PURCHASE_ORDER_LINES.c.product_id, _PURCHASE_ORDER_LINES.c.qty)
                .select_from(
                    _PURCHASE_ORDER_LINES.join(
                        _PURCHASE_ORDERS,
                        and_(
                            _PURCHASE_ORDERS.c.id == _PURCHASE_ORDER_LINES.c.order_id,
                            _PURCHASE_ORDERS.c.company_id == company_id,
                        ),
                    )
                )
                .where(
                    _PURCHASE_ORDER_LINES.c.company_id == company_id,
                    cast(_PURCHASE_ORDERS.c.status, String) == "CONFIRMED",
                )
            )
        ).all()
        for r in po_rows:
            scheduled[r.product_id] = _round_half_even_6dp(
                scheduled[r.product_id] + _round_half_even_6dp(Decimal(str(r.qty)))
            )

    allocated: dict[uuid.UUID, Decimal] = defaultdict(lambda: _ZERO)
    if data.include_open_work_orders:
        open_wos = (
            (
                await session.execute(
                    select(WorkOrder)
                    .where(
                        WorkOrder.status.in_(
                            [WorkOrderStatus.CONFIRMED, WorkOrderStatus.IN_PROGRESS]
                        )
                    )
                    .options(selectinload(WorkOrder.lines))
                )
            )
            .scalars()
            .all()
        )
        for wo in open_wos:
            rem_out = _round_half_even_6dp(wo.planned_qty - wo.completed_qty)
            if rem_out > 0:
                scheduled[wo.product_id] = _round_half_even_6dp(scheduled[wo.product_id] + rem_out)
            for ln in wo.lines:
                rem_comp = _round_half_even_6dp(ln.required_qty - (ln.issued_qty - ln.returned_qty))
                if rem_comp > 0:
                    allocated[ln.component_product_id] = _round_half_even_6dp(
                        allocated[ln.component_product_id] + rem_comp
                    )

    return gross, on_hand, scheduled, allocated


def _run_topological_mrp_sweep(
    *,
    products: dict[uuid.UUID, Any],
    bom_by_product: dict[uuid.UUID, Bom],
    gross: dict[uuid.UUID, Decimal],
    on_hand: dict[uuid.UUID, Decimal],
    scheduled: dict[uuid.UUID, Decimal],
) -> list[MrpRequirementRow]:
    reachable: set[uuid.UUID] = set()
    bfs = deque(pid for pid, q in gross.items() if q > 0)
    while bfs:
        pid = bfs.popleft()
        if pid in reachable:
            continue
        reachable.add(pid)
        bom = bom_by_product.get(pid)
        if bom is not None:
            for ln in bom.lines:
                if ln.component_product_id not in reachable:
                    bfs.append(ln.component_product_id)

    in_degree: dict[uuid.UUID, int] = dict.fromkeys(reachable, 0)
    for pid in reachable:
        bom = bom_by_product.get(pid)
        if bom is not None:
            for ln in bom.lines:
                if ln.component_product_id in reachable:
                    in_degree[ln.component_product_id] += 1

    levels: dict[uuid.UUID, int] = dict.fromkeys(reachable, 0)
    ready: list[uuid.UUID] = sorted(
        (pid for pid, deg in in_degree.items() if deg == 0),
        key=lambda p: (str(products[p].sku), str(p)),
    )
    queue: deque[uuid.UUID] = deque(ready)
    rows: list[MrpRequirementRow] = []

    while queue:
        pid = queue.popleft()
        prod = products[pid]
        g_qty = _round_half_even_6dp(gross.get(pid, _ZERO))
        oh_qty = _round_half_even_6dp(on_hand.get(pid, _ZERO))
        sr_qty = _round_half_even_6dp(scheduled.get(pid, _ZERO))
        net_qty = max(_ZERO, _round_half_even_6dp(g_qty - oh_qty - sr_qty))
        bom = bom_by_product.get(pid)

        action: Any = "none" if net_qty <= 0 else ("manufacture" if bom is not None else "purchase")
        rows.append(
            MrpRequirementRow(
                product_id=pid,
                sku=str(prod.sku),
                product_name=str(prod.name),
                uom_id=prod.uom_id,
                level=levels[pid],
                gross_requirement=g_qty,
                on_hand_qty=oh_qty,
                scheduled_receipts_qty=sr_qty,
                net_requirement=net_qty,
                action=action,
                bom_id=bom.id if bom is not None else None,
            )
        )

        if bom is not None:
            for ln in bom.lines:
                cid = ln.component_product_id
                if net_qty > 0:
                    child_req = _round_half_even_6dp(
                        (net_qty * ln.qty_per / bom.output_qty) * (_ONE + ln.scrap_rate)
                    )
                    gross[cid] = _round_half_even_6dp(gross[cid] + child_req)
                    levels[cid] = max(levels[cid], levels[pid] + 1)
                in_degree[cid] -= 1
                if in_degree[cid] == 0:
                    queue.append(cid)

    rows.sort(key=lambda r: (r.level, r.sku))
    return rows


async def calculate_net_requirements(
    session: AsyncSession, data: MrpPlanRequest
) -> MrpPlanResponse:
    """Compute topologically ordered multi-level MRP net requirements without double counting."""
    require_permission("mrp.plan.read")
    company_id = require_current_company_id()

    products, _ = await _load_product_cost_lookup(session, company_id)
    for item in data.demands:
        if item.product_id not in products:
            raise DomainValidationError(
                f"product_id {item.product_id} not found or does not belong to this company"
            )

    gross, on_hand, scheduled, allocated = await _gather_mrp_supply_and_allocations(
        session, company_id, data
    )
    active_boms = (
        (
            await session.execute(
                select(Bom).where(Bom.is_active.is_(True)).options(selectinload(Bom.lines))
            )
        )
        .scalars()
        .all()
    )
    bom_by_product: dict[uuid.UUID, Bom] = {b.product_id: b for b in active_boms}

    for pid, alloc_qty in allocated.items():
        gross[pid] = _round_half_even_6dp(gross[pid] + alloc_qty)

    rows = _run_topological_mrp_sweep(
        products=products,
        bom_by_product=bom_by_product,
        gross=gross,
        on_hand=on_hand,
        scheduled=scheduled,
    )
    return MrpPlanResponse(requirements=rows)
