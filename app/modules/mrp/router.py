"""mrp FastAPI routes (ADR-017) — commit + IntegrityError -> 409 boundary."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.mrp import service
from app.modules.mrp.models import WorkOrderStatus
from app.modules.mrp.schemas import (
    BomCreate,
    BomExplosionRead,
    BomRead,
    BomUpdate,
    MaterialIssueRequest,
    MaterialReturnRequest,
    MrpPlanRequest,
    MrpPlanResponse,
    ProductionReceiptRequest,
    WorkOrderCreate,
    WorkOrderRead,
    WorkOrderTransactionRead,
    WorkOrderUpdate,
)

router = APIRouter(prefix="/mrp", tags=["mrp"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _matches_constraint(exc: IntegrityError, constraint_name: str) -> bool:
    orig = getattr(exc, "orig", None)
    actual = getattr(orig, "constraint_name", None)
    if actual:
        return str(actual) == constraint_name
    return constraint_name in str(exc)


# ---------------------------------------------------------------------------
# Bills of Materials (BOM)
# ---------------------------------------------------------------------------


@router.post(
    "/boms",
    response_model=BomRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("mrp.bom.create"))],
)
async def create_bom(payload: BomCreate, session: DbSession) -> BomRead:
    try:
        bom = await service.create_bom(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _matches_constraint(exc, "uq_boms_company_product_active"):
            raise ConflictError(
                f"Product {payload.product_id} already has an active BOM; "
                "deactivate the existing BOM first"
            ) from exc
        if _matches_constraint(exc, "uq_boms_company_code"):
            raise ConflictError(
                f"BOM code {payload.code!r} already exists in this company"
            ) from exc
        raise ConflictError("BOM violates a uniqueness or reference constraint") from exc
    bom = await service.load_bom_unchecked(session, bom.id)
    return BomRead.model_validate(bom)


@router.get(
    "/boms",
    response_model=list[BomRead],
    dependencies=[Depends(RequiresPermission("mrp.bom.read"))],
)
async def list_boms(
    session: DbSession,
    product_id: uuid.UUID | None = None,
    active_only: bool = False,
) -> list[BomRead]:
    boms = await service.list_boms(session, product_id=product_id, active_only=active_only)
    return [BomRead.model_validate(b) for b in boms]


@router.get(
    "/boms/{bom_id}",
    response_model=BomRead,
    dependencies=[Depends(RequiresPermission("mrp.bom.read"))],
)
async def get_bom(bom_id: uuid.UUID, session: DbSession) -> BomRead:
    bom = await service.get_bom(session, bom_id)
    return BomRead.model_validate(bom)


@router.patch(
    "/boms/{bom_id}",
    response_model=BomRead,
    dependencies=[Depends(RequiresPermission("mrp.bom.update"))],
)
async def update_bom(bom_id: uuid.UUID, payload: BomUpdate, session: DbSession) -> BomRead:
    try:
        await service.update_bom(session, bom_id, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _matches_constraint(exc, "uq_boms_company_product_active"):
            raise ConflictError(
                "This product already has another active BOM; "
                "deactivate the existing active BOM first"
            ) from exc
        raise ConflictError(f"BOM {bom_id} violates a uniqueness or reference constraint") from exc
    bom = await service.load_bom_unchecked(session, bom_id)
    return BomRead.model_validate(bom)


@router.get(
    "/boms/{bom_id}/explode",
    response_model=BomExplosionRead,
    dependencies=[Depends(RequiresPermission("mrp.bom.read"))],
)
async def explode_bom(
    bom_id: uuid.UUID,
    session: DbSession,
    qty: Annotated[Decimal | None, Query(gt=0)] = None,
) -> BomExplosionRead:
    return await service.explode_bom(session, bom_id, qty=qty)


# ---------------------------------------------------------------------------
# Work Orders & Shop-Floor Transactions
# ---------------------------------------------------------------------------


@router.post(
    "/work-orders",
    response_model=WorkOrderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("mrp.work_order.create"))],
)
async def create_work_order(payload: WorkOrderCreate, session: DbSession) -> WorkOrderRead:
    try:
        wo = await service.create_work_order(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("WorkOrder violates a uniqueness or reference constraint") from exc
    wo = await service.load_work_order_unchecked(session, wo.id)
    return WorkOrderRead.model_validate(wo)


@router.get(
    "/work-orders",
    response_model=list[WorkOrderRead],
    dependencies=[Depends(RequiresPermission("mrp.work_order.read"))],
)
async def list_work_orders(
    session: DbSession,
    product_id: uuid.UUID | None = None,
    status_filter: Annotated[WorkOrderStatus | None, Query(alias="status")] = None,
) -> list[WorkOrderRead]:
    rows = await service.list_work_orders(session, product_id=product_id, status=status_filter)
    return [WorkOrderRead.model_validate(r) for r in rows]


@router.get(
    "/work-orders/{wo_id}",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.read"))],
)
async def get_work_order(wo_id: uuid.UUID, session: DbSession) -> WorkOrderRead:
    wo = await service.get_work_order(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.patch(
    "/work-orders/{wo_id}",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.update"))],
)
async def update_work_order(
    wo_id: uuid.UUID, payload: WorkOrderUpdate, session: DbSession
) -> WorkOrderRead:
    try:
        await service.update_work_order(session, wo_id, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"WorkOrder {wo_id} violates a uniqueness or reference constraint"
        ) from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.post(
    "/work-orders/{wo_id}/confirm",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.confirm"))],
)
async def confirm_work_order(wo_id: uuid.UUID, session: DbSession) -> WorkOrderRead:
    try:
        await service.confirm_work_order(session, wo_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"WorkOrder {wo_id} failed confirmation constraints") from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.post(
    "/work-orders/{wo_id}/cancel",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.cancel"))],
)
async def cancel_work_order(wo_id: uuid.UUID, session: DbSession) -> WorkOrderRead:
    try:
        await service.cancel_work_order(session, wo_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"WorkOrder {wo_id} failed cancellation constraints") from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.post(
    "/work-orders/{wo_id}/issue-materials",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.issue"))],
)
async def issue_materials(
    wo_id: uuid.UUID, payload: MaterialIssueRequest, session: DbSession
) -> WorkOrderRead:
    try:
        await service.issue_materials(session, wo_id, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"WorkOrder {wo_id} material issue violates a constraint or duplicate request_ref"
        ) from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.post(
    "/work-orders/{wo_id}/return-materials",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.issue"))],
)
async def return_materials(
    wo_id: uuid.UUID, payload: MaterialReturnRequest, session: DbSession
) -> WorkOrderRead:
    try:
        await service.return_materials(session, wo_id, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"WorkOrder {wo_id} material return violates a constraint or duplicate request_ref"
        ) from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.post(
    "/work-orders/{wo_id}/receive-production",
    response_model=WorkOrderRead,
    dependencies=[Depends(RequiresPermission("mrp.work_order.receive"))],
)
async def receive_production(
    wo_id: uuid.UUID, payload: ProductionReceiptRequest, session: DbSession
) -> WorkOrderRead:
    try:
        await service.receive_production(session, wo_id, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"WorkOrder {wo_id} production receipt violates a constraint or duplicate request_ref"
        ) from exc
    wo = await service.load_work_order_unchecked(session, wo_id)
    return WorkOrderRead.model_validate(wo)


@router.get(
    "/work-orders/{wo_id}/transactions",
    response_model=list[WorkOrderTransactionRead],
    dependencies=[Depends(RequiresPermission("mrp.work_order.read"))],
)
async def list_work_order_transactions(
    wo_id: uuid.UUID, session: DbSession
) -> list[WorkOrderTransactionRead]:
    rows = await service.list_work_order_transactions(session, wo_id)
    return [WorkOrderTransactionRead.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# MRP Net-Requirements Planning
# ---------------------------------------------------------------------------


@router.post(
    "/planning/net-requirements",
    response_model=MrpPlanResponse,
    dependencies=[Depends(RequiresPermission("mrp.plan.read"))],
)
async def calculate_net_requirements(
    payload: MrpPlanRequest, session: DbSession
) -> MrpPlanResponse:
    return await service.calculate_net_requirements(session, payload)
