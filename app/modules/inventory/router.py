"""inventory FastAPI routes — thin, same convention as every other module's router."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_session
from app.modules.inventory import service
from app.modules.inventory.schemas import StockAdjustmentCreate, StockMoveRead, StockSummaryRead

router = APIRouter(prefix="/inventory", tags=["inventory"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.post("/adjustments", response_model=StockMoveRead, status_code=status.HTTP_201_CREATED)
async def create_adjustment(payload: StockAdjustmentCreate, session: DbSession) -> StockMoveRead:
    move = await service.create_adjustment(session, payload)
    return StockMoveRead.model_validate(move)


@router.get("/summary", response_model=list[StockSummaryRead])
async def get_stock_summary(
    session: DbSession, product_id: uuid.UUID | None = None
) -> list[StockSummaryRead]:
    summaries = await service.list_stock_summary(session, product_id=product_id)
    return [StockSummaryRead.model_validate(s) for s in summaries]


@router.get("/moves", response_model=list[StockMoveRead])
async def list_stock_moves(
    session: DbSession, product_id: uuid.UUID | None = None
) -> list[StockMoveRead]:
    moves = await service.list_stock_moves(session, product_id=product_id)
    return [StockMoveRead.model_validate(m) for m in moves]
