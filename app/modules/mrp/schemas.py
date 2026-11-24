"""mrp Pydantic v2 DTOs (ADR-017)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.mrp.models import WorkOrderStatus, WorkOrderTxType

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _require_positive_6dp(value: Decimal, *, field_name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    rounded = _round_half_even_6dp(value)
    if rounded <= 0:
        raise ValueError(f"{field_name} must be greater than 0 after rounding to 6 decimal places")
    return rounded


def _require_nonblank(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} cannot be blank")
    return stripped


# ---------------------------------------------------------------------------
# BOM Schemas
# ---------------------------------------------------------------------------


class BomLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_product_id: uuid.UUID
    qty_per: Decimal = Field(gt=0)
    uom_id: uuid.UUID | None = None
    scrap_rate: Decimal = Field(default=Decimal("0"), ge=0, lt=1)

    @field_validator("qty_per")
    @classmethod
    def _validate_qty_per(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="qty_per")

    @field_validator("scrap_rate")
    @classmethod
    def _validate_scrap_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("scrap_rate must be a finite Decimal")
        rounded = _round_half_even_6dp(value)
        if rounded < 0 or rounded >= Decimal("1"):
            raise ValueError("scrap_rate must satisfy 0 <= scrap_rate < 1")
        return rounded


class BomLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bom_id: uuid.UUID
    line_no: int
    component_product_id: uuid.UUID
    qty_per: Decimal
    uom_id: uuid.UUID
    scrap_rate: Decimal
    created_at: datetime


class BomCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)
    product_id: uuid.UUID
    uom_id: uuid.UUID | None = None
    output_qty: Decimal = Field(default=Decimal("1"), gt=0)
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=500)
    lines: list[BomLineCreate] = Field(min_length=1)
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return _require_nonblank(value, field_name="code")

    @field_validator("output_qty")
    @classmethod
    def _validate_output_qty(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="output_qty")


class BomUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_qty: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=500)
    lines: list[BomLineCreate] | None = Field(default=None, min_length=1)
    custom_data: dict[str, Any] | None = None

    @field_validator("output_qty")
    @classmethod
    def _validate_output_qty(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _require_positive_6dp(value, field_name="output_qty")


class BomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    code: str
    product_id: uuid.UUID
    uom_id: uuid.UUID
    output_qty: Decimal
    is_active: bool
    notes: str | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    lines: list[BomLineRead]


class BomExplosionNode(BaseModel):
    level: int
    bom_id: uuid.UUID
    parent_product_id: uuid.UUID
    component_product_id: uuid.UUID
    sku: str
    product_name: str
    uom_id: uuid.UUID
    qty_per: Decimal
    scrap_rate: Decimal
    effective_qty: Decimal
    is_subassembly: bool
    sub_bom_id: uuid.UUID | None
    standard_unit_cost: Decimal
    moving_avg_cost: Decimal
    rolled_up_unit_cost: Decimal
    extended_cost: Decimal


class BomExplosionRead(BaseModel):
    bom_id: uuid.UUID
    code: str
    product_id: uuid.UUID
    sku: str
    product_name: str
    requested_qty: Decimal
    total_standard_material_cost: Decimal
    total_rolled_up_cost: Decimal
    unit_rolled_up_cost: Decimal
    nodes: list[BomExplosionNode]


# ---------------------------------------------------------------------------
# Work Order Schemas
# ---------------------------------------------------------------------------


class WorkOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bom_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    planned_qty: Decimal = Field(gt=0)
    notes: str | None = Field(default=None, max_length=500)
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("planned_qty")
    @classmethod
    def _validate_planned_qty(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="planned_qty")

    @model_validator(mode="after")
    def _validate_target(self) -> WorkOrderCreate:
        if self.bom_id is None and self.product_id is None:
            raise ValueError("Either bom_id or product_id must be provided")
        return self


class WorkOrderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planned_qty: Decimal | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=500)
    custom_data: dict[str, Any] | None = None

    @field_validator("planned_qty")
    @classmethod
    def _validate_planned_qty(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _require_positive_6dp(value, field_name="planned_qty")


class WorkOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    work_order_id: uuid.UUID
    line_no: int
    component_product_id: uuid.UUID
    uom_id: uuid.UUID
    required_qty: Decimal
    issued_qty: Decimal
    returned_qty: Decimal
    issued_cost: Decimal
    returned_cost: Decimal
    estimated_unit_cost: Decimal
    snapshot_sku: str | None
    snapshot_product_name: str | None
    created_at: datetime


class WorkOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    wo_no: str
    bom_id: uuid.UUID
    product_id: uuid.UUID
    uom_id: uuid.UUID
    status: WorkOrderStatus
    planned_qty: Decimal
    completed_qty: Decimal
    wip_balance: Decimal
    snapshot_sku: str | None
    snapshot_product_name: str | None
    confirmed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    notes: str | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    lines: list[WorkOrderLineRead]


class MaterialMoveLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_product_id: uuid.UUID
    qty: Decimal = Field(gt=0)

    @field_validator("qty")
    @classmethod
    def _validate_qty(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="qty")


class MaterialIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ref: str = Field(min_length=1, max_length=128)
    lines: list[MaterialMoveLineInput] | None = Field(default=None, min_length=1)

    @field_validator("request_ref")
    @classmethod
    def _validate_request_ref(cls, value: str) -> str:
        return _require_nonblank(value, field_name="request_ref")


class MaterialReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ref: str = Field(min_length=1, max_length=128)
    lines: list[MaterialMoveLineInput] = Field(min_length=1)

    @field_validator("request_ref")
    @classmethod
    def _validate_request_ref(cls, value: str) -> str:
        return _require_nonblank(value, field_name="request_ref")


class ProductionReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ref: str = Field(min_length=1, max_length=128)
    qty: Decimal = Field(gt=0)
    mark_completed: bool = False

    @field_validator("request_ref")
    @classmethod
    def _validate_request_ref(cls, value: str) -> str:
        return _require_nonblank(value, field_name="request_ref")

    @field_validator("qty")
    @classmethod
    def _validate_qty(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="qty")


class WorkOrderTransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    work_order_id: uuid.UUID
    tx_type: WorkOrderTxType
    request_ref: str
    total_cost: Decimal
    received_qty: Decimal | None
    lines_payload: list[dict[str, Any]]
    created_at: datetime


# ---------------------------------------------------------------------------
# MRP Net-Requirements Planning Schemas
# ---------------------------------------------------------------------------


class MrpDemandItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    qty: Decimal = Field(gt=0)

    @field_validator("qty")
    @classmethod
    def _validate_qty(cls, value: Decimal) -> Decimal:
        return _require_positive_6dp(value, field_name="qty")


class MrpPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demands: list[MrpDemandItem] = Field(default_factory=list)
    include_open_sales_orders: bool = False
    include_open_purchase_orders: bool = True
    include_open_work_orders: bool = True


class MrpRequirementRow(BaseModel):
    product_id: uuid.UUID
    sku: str
    product_name: str
    uom_id: uuid.UUID
    level: int
    gross_requirement: Decimal
    on_hand_qty: Decimal
    scheduled_receipts_qty: Decimal
    net_requirement: Decimal
    action: Literal["manufacture", "purchase", "none"]
    bom_id: uuid.UUID | None


class MrpPlanResponse(BaseModel):
    requirements: list[MrpRequirementRow]
