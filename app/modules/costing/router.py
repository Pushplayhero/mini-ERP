"""costing FastAPI routes (ADR-016) — commit + IntegrityError -> 409 boundary."""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.costing import service
from app.modules.costing.models import CostAdjustmentType
from app.modules.costing.schemas import (
    CostAdjustmentCreate,
    CostAdjustmentRead,
    CostMoveRead,
    ProductCostSummaryRead,
    ValuationVarianceRow,
)

router = APIRouter(prefix="/costing", tags=["costing"])

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


# ---------------------------------------------------------------------------
# Cost Summaries & Standard Cost Sync
# ---------------------------------------------------------------------------


@router.get(
    "/summaries",
    response_model=list[ProductCostSummaryRead],
    dependencies=[Depends(RequiresPermission("costing.summary.read"))],
)
async def list_cost_summaries(
    session: DbSession,
    product_id: uuid.UUID | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    after_product_id: uuid.UUID | None = None,
) -> list[ProductCostSummaryRead]:
    summaries = await service.list_cost_summaries(
        session,
        product_id=product_id,
        limit=limit,
        after_product_id=after_product_id,
    )
    return [ProductCostSummaryRead.model_validate(s) for s in summaries]


@router.post(
    "/summaries/{product_id}/sync-standard-cost",
    response_model=ProductCostSummaryRead,
    dependencies=[Depends(RequiresPermission("costing.adjustment.create"))],
)
async def sync_standard_cost(product_id: uuid.UUID, session: DbSession) -> ProductCostSummaryRead:
    try:
        await service.sync_standard_cost(session, product_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Product {product_id} violates a constraint while syncing standard cost"
        ) from exc
    summary = await service.load_cost_summary_unchecked(session, product_id)
    return ProductCostSummaryRead.model_validate(summary)


# ---------------------------------------------------------------------------
# Append-Only Cost Moves
# ---------------------------------------------------------------------------


@router.get(
    "/moves",
    response_model=list[CostMoveRead],
    dependencies=[Depends(RequiresPermission("costing.move.read"))],
)
async def list_cost_moves(
    session: DbSession,
    product_id: uuid.UUID | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    after_created_at: AwareDatetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[CostMoveRead]:
    if (after_created_at is None) != (after_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="after_created_at and after_id must be provided together",
        )
    if after_created_at is not None:
        try:
            after_created_at = after_created_at.astimezone(UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="after_created_at must be representable as UTC",
            ) from exc
    moves = await service.list_cost_moves(
        session,
        product_id=product_id,
        limit=limit,
        after_created_at=after_created_at,
        after_id=after_id,
    )
    return [CostMoveRead.model_validate(m) for m in moves]


# ---------------------------------------------------------------------------
# Cost Adjustments (Landed Cost, PPV, Revaluation)
# ---------------------------------------------------------------------------


@router.post(
    "/adjustments",
    response_model=CostAdjustmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("costing.adjustment.create"))],
)
async def create_adjustment(
    payload: CostAdjustmentCreate, session: DbSession
) -> CostAdjustmentRead:
    try:
        adjustment = await service.create_adjustment(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("CostAdjustment violates a uniqueness or reference constraint") from exc
    adjustment = await service.load_adjustment_unchecked(session, adjustment.id)
    return CostAdjustmentRead.model_validate(adjustment)


@router.get(
    "/adjustments",
    response_model=list[CostAdjustmentRead],
    dependencies=[Depends(RequiresPermission("costing.adjustment.read"))],
)
async def list_adjustments(
    session: DbSession,
    product_id: uuid.UUID | None = None,
    adjustment_type: CostAdjustmentType | None = None,
) -> list[CostAdjustmentRead]:
    rows = await service.list_adjustments(
        session, product_id=product_id, adjustment_type=adjustment_type
    )
    return [CostAdjustmentRead.model_validate(r) for r in rows]


@router.get(
    "/adjustments/{adjustment_id}",
    response_model=CostAdjustmentRead,
    dependencies=[Depends(RequiresPermission("costing.adjustment.read"))],
)
async def get_adjustment(adjustment_id: uuid.UUID, session: DbSession) -> CostAdjustmentRead:
    adjustment = await service.get_adjustment(session, adjustment_id)
    return CostAdjustmentRead.model_validate(adjustment)


@router.post(
    "/adjustments/{adjustment_id}/void",
    response_model=CostAdjustmentRead,
    dependencies=[Depends(RequiresPermission("costing.adjustment.void"))],
)
async def void_adjustment(adjustment_id: uuid.UUID, session: DbSession) -> CostAdjustmentRead:
    try:
        await service.void_adjustment(session, adjustment_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"CostAdjustment {adjustment_id} violates a uniqueness or reference constraint"
        ) from exc
    adjustment = await service.load_adjustment_unchecked(session, adjustment_id)
    return CostAdjustmentRead.model_validate(adjustment)


# ---------------------------------------------------------------------------
# Valuation Variance Report
# ---------------------------------------------------------------------------


@router.get(
    "/reports/valuation-variance",
    response_model=list[ValuationVarianceRow],
    dependencies=[Depends(RequiresPermission("costing.report.read"))],
)
async def get_valuation_variance_report(
    session: DbSession,
    product_id: uuid.UUID | None = None,
) -> list[ValuationVarianceRow]:
    return await service.get_valuation_variance_report(session, product_id=product_id)
