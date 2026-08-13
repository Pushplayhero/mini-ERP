"""ledger Pydantic v2 DTOs.

Same convention as masterdata: `*Create` schemas never accept `company_id`,
and — specific to ledger — never accept `entry_no`, `reversal_of_id`,
`posted_at`, or `period_id` either. All of those are server-decided per
ADR-005 (R2 gapless numbering, R3 reversal linkage, R4 period resolution):
a client says *when* (`entry_date`) and *what* (`lines`), never *what
voucher number it gets* or *which period it lands in*.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.ledger.models import PeriodStatus

# ---------------------------------------------------------------------------
# Accounting periods
# ---------------------------------------------------------------------------


class AccountingPeriodCreate(BaseModel):
    year: int = Field(ge=1900, le=9999)
    month: int = Field(ge=1, le=12)
    custom_data: dict[str, Any] = Field(default_factory=dict)


class AccountingPeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    year: int
    month: int
    status: PeriodStatus
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Journal lines
# ---------------------------------------------------------------------------


class JournalLineCreate(BaseModel):
    account_id: uuid.UUID
    currency_code: str = Field(min_length=3, max_length=3)
    txn_debit: Decimal = Decimal("0")
    txn_credit: Decimal = Decimal("0")
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    # Phase 1 is TWD-only in practice (ADR-005 R1); the service layer
    # rejects any line whose currency_code is not the company's functional
    # currency rather than performing real conversion, so exchange_rate
    # defaults to 1 and is validated to equal 1 for any accepted line.
    exchange_rate: Decimal = Decimal("1")
    rate_date: date | None = None


class JournalLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entry_id: uuid.UUID
    line_no: int
    account_id: uuid.UUID
    currency_code: str
    txn_debit: Decimal
    txn_credit: Decimal
    debit: Decimal
    credit: Decimal
    exchange_rate: Decimal
    rate_date: date


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------


class JournalEntryCreate(BaseModel):
    entry_date: date
    source_type: str | None = Field(default=None, max_length=64)
    source_id: uuid.UUID | None = None
    # A balanced entry needs at least one debit line and one credit line —
    # a single line alone cannot balance unless it is degenerate (all
    # zeros), which service-layer validation also rejects.
    lines: list[JournalLineCreate] = Field(min_length=2)
    custom_data: dict[str, Any] = Field(default_factory=dict)


class JournalEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    entry_no: str
    entry_date: date
    period_id: uuid.UUID
    source_type: str | None
    source_id: uuid.UUID | None
    reversal_of_id: uuid.UUID | None
    posted_at: datetime
    created_at: datetime
    custom_data: dict[str, Any]
    lines: list[JournalLineRead]


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


class TrialBalanceLine(BaseModel):
    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    total_debit: Decimal
    total_credit: Decimal
