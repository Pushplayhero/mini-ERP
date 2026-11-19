"""purchase FastAPI routes — thin: parse request, call service, serialize (ADR-014)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.purchase import service
from app.modules.purchase.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderRead,
    PurchaseOrderUpdate,
)

router = APIRouter(tags=["purchase"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.post(
    "/purchase-orders",
    response_model=PurchaseOrderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("purchase.order.create"))],
)
async def create_purchase_order(
    payload: PurchaseOrderCreate, session: DbSession
) -> PurchaseOrderRead:
    order = await service.create_order(session, payload)
    return PurchaseOrderRead.model_validate(order)


@router.get(
    "/purchase-orders",
    response_model=list[PurchaseOrderRead],
    dependencies=[Depends(RequiresPermission("purchase.order.read"))],
)
async def list_purchase_orders(session: DbSession) -> list[PurchaseOrderRead]:
    orders = await service.list_orders(session)
    return [PurchaseOrderRead.model_validate(o) for o in orders]


@router.get(
    "/purchase-orders/{order_id}",
    response_model=PurchaseOrderRead,
    dependencies=[Depends(RequiresPermission("purchase.order.read"))],
)
async def get_purchase_order(order_id: uuid.UUID, session: DbSession) -> PurchaseOrderRead:
    order = await service.get_order(session, order_id)
    return PurchaseOrderRead.model_validate(order)


@router.patch(
    "/purchase-orders/{order_id}",
    response_model=PurchaseOrderRead,
    dependencies=[Depends(RequiresPermission("purchase.order.update"))],
)
async def update_purchase_order(
    order_id: uuid.UUID, payload: PurchaseOrderUpdate, session: DbSession
) -> PurchaseOrderRead:
    order = await service.update_order(session, order_id, payload)
    return PurchaseOrderRead.model_validate(order)


@router.post(
    "/purchase-orders/{order_id}/confirm",
    response_model=PurchaseOrderRead,
    dependencies=[Depends(RequiresPermission("purchase.order.confirm"))],
)
async def confirm_purchase_order(order_id: uuid.UUID, session: DbSession) -> PurchaseOrderRead:
    try:
        await service.confirm_order(session, order_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"PurchaseOrder {order_id} violates a uniqueness or reference constraint"
        ) from exc
    order = await service.load_order_unchecked(session, order_id)
    return PurchaseOrderRead.model_validate(order)


@router.post(
    "/purchase-orders/{order_id}/cancel",
    response_model=PurchaseOrderRead,
    dependencies=[Depends(RequiresPermission("purchase.order.cancel"))],
)
async def cancel_purchase_order(order_id: uuid.UUID, session: DbSession) -> PurchaseOrderRead:
    order = await service.cancel_order(session, order_id)
    return PurchaseOrderRead.model_validate(order)


@router.post(
    "/purchase-orders/{order_id}/receive",
    response_model=PurchaseOrderRead,
    dependencies=[Depends(RequiresPermission("purchase.order.receive"))],
)
async def receive_purchase_order(order_id: uuid.UUID, session: DbSession) -> PurchaseOrderRead:
    try:
        await service.receive_order(session, order_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"PurchaseOrder {order_id} violates a uniqueness or reference constraint"
        ) from exc
    order = await service.load_order_unchecked(session, order_id)
    return PurchaseOrderRead.model_validate(order)
