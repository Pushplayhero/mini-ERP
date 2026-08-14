"""sales FastAPI routes — thin, same convention as masterdata/ledger routers.

Every endpoint but `confirm` calls a single committing service function,
same shape as every other module's router. `confirm` is the one exception:
`service.confirm_order` is deliberately the ADR-003 R1 "flush only, caller
commits" core (ADR-006's explicit instruction — see that function's
docstring), so this router owns the commit boundary and the
`IntegrityError` -> 409 translation for it, mirroring
`ledger.service.create_journal_entry`'s wrapper *except* that here the
wrapping happens in the router layer instead of a second service function,
per the brief's explicit request to keep that translation out of the
service core.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.sales import service
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderRead, SalesOrderUpdate

router = APIRouter(tags=["sales"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.post("/sales-orders", response_model=SalesOrderRead, status_code=status.HTTP_201_CREATED)
async def create_sales_order(payload: SalesOrderCreate, session: DbSession) -> SalesOrderRead:
    order = await service.create_order(session, payload)
    return SalesOrderRead.model_validate(order)


@router.get("/sales-orders", response_model=list[SalesOrderRead])
async def list_sales_orders(session: DbSession) -> list[SalesOrderRead]:
    orders = await service.list_orders(session)
    return [SalesOrderRead.model_validate(o) for o in orders]


@router.get("/sales-orders/{order_id}", response_model=SalesOrderRead)
async def get_sales_order(order_id: uuid.UUID, session: DbSession) -> SalesOrderRead:
    order = await service.get_order(session, order_id)
    return SalesOrderRead.model_validate(order)


@router.patch("/sales-orders/{order_id}", response_model=SalesOrderRead)
async def update_sales_order(
    order_id: uuid.UUID, payload: SalesOrderUpdate, session: DbSession
) -> SalesOrderRead:
    order = await service.update_order(session, order_id, payload)
    return SalesOrderRead.model_validate(order)


@router.post("/sales-orders/{order_id}/confirm", response_model=SalesOrderRead)
async def confirm_sales_order(order_id: uuid.UUID, session: DbSession) -> SalesOrderRead:
    # Diff-review fix: the try/except must wrap `service.confirm_order`
    # itself, not just the later `commit()`. `confirm_order`'s own
    # `session.flush()` can raise `IntegrityError` on its own — e.g.
    # confirm-time repricing (ADR-006 Decision 3) writing a line's
    # `unit_price`/`amount` from `product.list_price` could violate
    # `ck_sales_order_lines_unit_price_nonneg`/`_amount_nonneg` if that
    # value were ever negative. A flush-time failure used to bypass this
    # translation entirely and surface as an unhandled 500 instead of a
    # 409 — same class of bug already fixed in `ledger.service`'s
    # `create_journal_entry`/`reverse_journal_entry`.
    try:
        await service.confirm_order(session, order_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"SalesOrder {order_id} violates a uniqueness or reference constraint"
        ) from exc
    # Re-select rather than `session.refresh(order, attribute_names=["lines"])`:
    # `SalesOrder.updated_at` is a server-side `onupdate=func.now()` column,
    # which a partial `refresh()` does not repopulate — see
    # `service.create_order`'s comment for the same fix applied there.
    order = await service.get_order(session, order_id)
    return SalesOrderRead.model_validate(order)


@router.post("/sales-orders/{order_id}/cancel", response_model=SalesOrderRead)
async def cancel_sales_order(order_id: uuid.UUID, session: DbSession) -> SalesOrderRead:
    order = await service.cancel_order(session, order_id)
    return SalesOrderRead.model_validate(order)


@router.post("/sales-orders/{order_id}/ship", response_model=SalesOrderRead)
async def ship_sales_order(order_id: uuid.UUID, session: DbSession) -> SalesOrderRead:
    """Same commit-boundary division of labor as `confirm_sales_order`:
    `service.ship_order` is the flush-only R1 core; this router owns the
    commit and the `IntegrityError` -> 409 translation for it — including,
    per the same diff-review fix, wrapping the core call itself so a
    flush-time failure inside `ship_order` cannot bypass this translation.
    """
    try:
        await service.ship_order(session, order_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"SalesOrder {order_id} violates a uniqueness or reference constraint"
        ) from exc
    order = await service.get_order(session, order_id)
    return SalesOrderRead.model_validate(order)
