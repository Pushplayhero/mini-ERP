"""purchase Pydantic v2 DTOs — ADR-014.

All server-computed fields (`company_id`, `order_no`, `status`, `total`, `snapshot_*`)
are omitted from `*Create` / `*Update` schemas.

`unit_cost` on `PurchaseOrderLineCreate` defaults from `product.standard_cost` when
omitted (`unit_cost_is_override=False`), and re-resolves against current `standard_cost`
at `confirm_order` time. Manual `unit_cost` overrides are preserved across confirmation
(`unit_cost_is_override=True`). Both `qty` and `unit_cost` use `ROUND_HALF_EVEN` to 6dp.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.purchase.models import PurchaseOrderStatus

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


class PurchaseOrderLineCreate(BaseModel):
    product_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)
    uom_id: uuid.UUID | None = None

    @field_validator("qty")
    @classmethod
    def _round_qty(cls, value: Decimal) -> Decimal:
        rounded = _round_half_even_6dp(value)
        if rounded <= 0:
            raise ValueError("qty must be greater than 0 after rounding to 6 decimal places")
        return rounded

    @field_validator("unit_cost")
    @classmethod
    def _round_unit_cost(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _round_half_even_6dp(value)


class PurchaseOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    line_no: int
    product_id: uuid.UUID
    qty: Decimal
    uom_id: uuid.UUID
    unit_cost: Decimal
    unit_cost_is_override: bool
    snapshot_sku: str | None
    snapshot_product_name: str | None
    amount: Decimal
    created_at: datetime


class PurchaseOrderCreate(BaseModel):
    supplier_id: uuid.UUID
    lines: list[PurchaseOrderLineCreate] = Field(default_factory=list)
    custom_data: dict[str, Any] = Field(default_factory=dict)


class PurchaseOrderUpdate(BaseModel):
    lines: list[PurchaseOrderLineCreate] | None = None
    custom_data: dict[str, Any] | None = None


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    order_no: str
    supplier_id: uuid.UUID
    status: PurchaseOrderStatus
    currency_code: str
    total: Decimal
    snapshot_supplier_code: str | None
    snapshot_supplier_name: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    received_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    lines: list[PurchaseOrderLineRead]
