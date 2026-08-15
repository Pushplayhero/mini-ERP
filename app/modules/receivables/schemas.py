"""receivables Pydantic v2 DTOs.

Same convention as every other module: `*Create` schemas never accept
`company_id`, `invoice_no`/`payment_no`, `status`, `total`, or any
`snapshot_*`/maintained-balance field — all server-decided (ADR-008
Decision 1/2/3). Money fields the client actually supplies
(`PaymentCreate.amount`, `PaymentAllocationIn.amount`) go through the same
round-half-even-to-6dp quantization `ledger.schemas.JournalLineCreate` uses
— duplicated here rather than imported (this module must never import
`app.modules.ledger`, per the independence contract), matching this
project's established "the money convention is duplicated, not shared
Python code, across an independence boundary" precedent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.receivables.models import InvoiceStatus, PaymentStatus

_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class InvoiceCreate(BaseModel):
    order_id: uuid.UUID
    # Both optional — omit to default to today / `invoice_date +
    # customer.payment_terms_days` respectively (ADR-008 Decision 1).
    invoice_date: date | None = None
    due_date: date | None = None


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    invoice_no: str
    order_id: uuid.UUID
    customer_id: uuid.UUID
    status: InvoiceStatus
    currency_code: str
    order_shipped_at: datetime
    invoice_date: date
    due_date: date
    total: Decimal
    settled_amount: Decimal
    snapshot_customer_code: str
    snapshot_customer_name: str
    voided_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Payments + allocations
# ---------------------------------------------------------------------------


class PaymentAllocationIn(BaseModel):
    invoice_id: uuid.UUID
    amount: Decimal = Field(gt=0)

    @field_validator("amount")
    @classmethod
    def _round_and_reject_zero(cls, value: Decimal) -> Decimal:
        rounded = _round_half_even_6dp(value)
        if rounded <= 0:
            raise ValueError("amount must be > 0 after rounding to 6 decimal places")
        return rounded


class PaymentCreate(BaseModel):
    customer_id: uuid.UUID
    # Required client idempotency key (ADR-008 R2) — a retried POST with
    # the same external_ref hits `uq_payments_company_external_ref` and
    # 409s instead of double-posting Cash/AR.
    external_ref: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0)
    # Omit to default to now() (ADR-008 Decision 2).
    received_at: datetime | None = None
    # Optional inline allocation at creation time — same underlying
    # `service.allocate_payment` core as the later /allocations endpoint;
    # inherits this payment's `external_ref` as its own request_ref.
    allocations: list[PaymentAllocationIn] = Field(default_factory=list)

    @field_validator("amount")
    @classmethod
    def _round_and_reject_zero(cls, value: Decimal) -> Decimal:
        rounded = _round_half_even_6dp(value)
        if rounded <= 0:
            raise ValueError("amount must be > 0 after rounding to 6 decimal places")
        return rounded


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    payment_no: str
    customer_id: uuid.UUID
    status: PaymentStatus
    external_ref: str
    currency_code: str
    amount: Decimal
    allocated_amount: Decimal
    received_at: datetime
    voided_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AllocationRequest(BaseModel):
    """Body for `POST /payments/{id}/allocations` (ADR-008 R7/R14).

    `request_ref` is the allocation-command idempotency key — required
    here (unlike `PaymentCreate.allocations`' inline batch, which inherits
    the payment's own `external_ref`), since a late allocation has no
    other natural command identity to reuse.
    """

    request_ref: str = Field(min_length=1, max_length=128)
    allocations: list[PaymentAllocationIn] = Field(min_length=1)


class PaymentAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    command_id: uuid.UUID
    amount: Decimal
    created_at: datetime


# ---------------------------------------------------------------------------
# AR aging (ADR-008 Decision 5)
# ---------------------------------------------------------------------------


class ARAgingRow(BaseModel):
    """One customer's row in the aging report — a CURRENT-STATE report

    (`bucket_date` only moves the days-past-due boundary, never a
    historical as-of cutoff; see `service.get_ar_aging`). `net_total` may
    be negative for a customer who is all unapplied credit with no open
    invoice (ADR-008 R15) — that is a correct value, not an error state.
    """

    customer_id: uuid.UUID
    customer_code: str
    customer_name: str
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_90_plus: Decimal
    unapplied_credits: Decimal
    net_total: Decimal
