"""payables Pydantic v2 DTOs (ADR-015)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.payables.models import VendorBillStatus, VendorPaymentStatus

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Vendor Bills
# ---------------------------------------------------------------------------


class VendorBillCreate(BaseModel):
    order_id: uuid.UUID
    supplier_invoice_no: str | None = Field(default=None, max_length=128)
    bill_date: date | None = None
    due_date: date | None = None
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("supplier_invoice_no")
    @classmethod
    def _normalize_supplier_invoice_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped if stripped else None


class VendorBillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    bill_no: str
    order_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_invoice_no: str | None
    status: VendorBillStatus
    currency_code: str
    order_received_at: datetime
    bill_date: date
    due_date: date
    total: Decimal
    settled_amount: Decimal
    snapshot_supplier_code: str
    snapshot_supplier_name: str
    voided_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Vendor Payments + Allocations
# ---------------------------------------------------------------------------


class VendorPaymentAllocationIn(BaseModel):
    bill_id: uuid.UUID
    amount: Decimal = Field(gt=0)

    @field_validator("amount")
    @classmethod
    def _round_and_reject_zero(cls, value: Decimal) -> Decimal:
        rounded = _round_half_even_6dp(value)
        if rounded <= 0:
            raise ValueError("amount must be > 0 after rounding to 6 decimal places")
        return rounded


class VendorPaymentCreate(BaseModel):
    supplier_id: uuid.UUID
    external_ref: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0)
    paid_at: datetime | None = None
    allocations: list[VendorPaymentAllocationIn] = Field(default_factory=list)
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount")
    @classmethod
    def _round_and_reject_zero(cls, value: Decimal) -> Decimal:
        rounded = _round_half_even_6dp(value)
        if rounded <= 0:
            raise ValueError("amount must be > 0 after rounding to 6 decimal places")
        return rounded


class VendorPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    payment_no: str
    supplier_id: uuid.UUID
    status: VendorPaymentStatus
    external_ref: str
    currency_code: str
    amount: Decimal
    allocated_amount: Decimal
    paid_at: datetime
    voided_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class VendorAllocationRequest(BaseModel):
    request_ref: str = Field(min_length=1, max_length=128)
    allocations: list[VendorPaymentAllocationIn] = Field(min_length=1)


class VendorPaymentAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payment_id: uuid.UUID
    bill_id: uuid.UUID
    command_id: uuid.UUID
    amount: Decimal
    created_at: datetime


# ---------------------------------------------------------------------------
# AP Aging (ADR-015 Decision 5)
# ---------------------------------------------------------------------------


class APAgingRow(BaseModel):
    """One supplier's row in the current-state AP aging report."""

    supplier_id: uuid.UUID
    supplier_code: str
    supplier_name: str
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_90_plus: Decimal
    unapplied_payments: Decimal
    net_total: Decimal
