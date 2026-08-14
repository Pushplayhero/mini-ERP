"""inventory Pydantic v2 DTOs.

Same convention as every other module: `*Create` schemas never accept
`company_id` (always injected server-side from the tenancy context).
`StockAdjustmentCreate` is the Phase 1 manual-intake stand-in (ADR-007
Decision 2) — `reason` is mandatory so every adjustment carries an audit
trail, since there is no purchasing/receiving module yet to explain *why*
stock appeared or vanished.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.inventory.models import StockMoveType


class StockAdjustmentCreate(BaseModel):
    product_id: uuid.UUID
    qty_delta: Decimal
    reason: str = Field(min_length=1, max_length=255)

    @field_validator("qty_delta")
    @classmethod
    def _qty_delta_nonzero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("qty_delta must be nonzero (a zero adjustment is not a move)")
        return value


class StockMoveRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    product_id: uuid.UUID
    qty_delta: Decimal
    move_type: StockMoveType
    source_type: str | None
    source_id: uuid.UUID | None
    reason: str | None
    created_at: datetime
    created_by: uuid.UUID | None


class StockSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_id: uuid.UUID
    product_id: uuid.UUID
    on_hand: Decimal
