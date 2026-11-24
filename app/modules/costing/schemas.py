"""costing Pydantic v2 DTOs (ADR-016)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.costing.models import CostAdjustmentStatus, CostAdjustmentType, CostMoveType

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


class ProductCostSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_id: uuid.UUID
    product_id: uuid.UUID
    tracked_qty: Decimal
    total_cost_value: Decimal
    moving_avg_cost: Decimal


class CostMoveRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    product_id: uuid.UUID
    move_type: CostMoveType
    qty_delta: Decimal
    value_delta: Decimal
    unit_cost: Decimal
    resulting_qty: Decimal
    resulting_value: Decimal
    resulting_avg_cost: Decimal
    source_type: str | None
    source_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class CostAdjustmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustment_type: CostAdjustmentType
    product_id: uuid.UUID
    order_id: uuid.UUID | None = None
    amount: Decimal
    adjustment_date: date | None = None
    reason: str = Field(min_length=1, max_length=255)
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount")
    @classmethod
    def _round_and_reject_zero(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be a finite Decimal")
        rounded = _round_half_even_6dp(value)
        if rounded == 0:
            raise ValueError("amount must be non-zero after rounding to 6 decimal places")
        return rounded

    @field_validator("reason")
    @classmethod
    def _strip_and_require_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason cannot be blank")
        return stripped

    @model_validator(mode="after")
    def _validate_adjustment_constraints(self) -> CostAdjustmentCreate:
        if (
            self.adjustment_type in (CostAdjustmentType.LANDED_COST, CostAdjustmentType.PPV)
            and self.order_id is None
        ):
            raise ValueError(
                f"order_id is required for {self.adjustment_type.value} cost adjustments"
            )
        if self.adjustment_type is CostAdjustmentType.REVALUATION and self.order_id is not None:
            raise ValueError("order_id must not be provided for revaluation cost adjustments")
        if self.adjustment_type is CostAdjustmentType.LANDED_COST and self.amount <= 0:
            raise ValueError("amount must be positive for landed_cost adjustments")
        return self


class CostAdjustmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    adjustment_no: str
    adjustment_type: CostAdjustmentType
    status: CostAdjustmentStatus
    product_id: uuid.UUID
    order_id: uuid.UUID | None
    amount: Decimal
    adjustment_date: date
    reason: str
    voided_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ValuationVarianceRow(BaseModel):
    """Per-product comparison of moving-average subledger valuation vs standard cost."""

    product_id: uuid.UUID
    sku: str
    product_name: str
    stock_on_hand: Decimal
    tracked_qty: Decimal
    moving_avg_cost: Decimal
    total_cost_value: Decimal
    standard_cost: Decimal
    standard_valuation: Decimal
    valuation_variance: Decimal
    unit_cost_variance: Decimal
