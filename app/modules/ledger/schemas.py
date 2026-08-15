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
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.ledger.models import PeriodStatus

# Diff-review fix (master-plan §10.1's rounding policy: line-level
# round-half-even). A client-supplied amount with more than 6 fractional
# digits used to fall
# through to Postgres's `NUMERIC(20,6)` column coercion at INSERT time,
# which rounds ties away from zero — a different tie-breaking rule than the
# banker's rounding (ties-to-even) this project commits to. Quantizing here,
# at schema-validation time, means every money field is already rounded the
# required way before `_validate_lines`/the balance trigger ever sees it —
# "header 金額 = 已捨入 lines 之和" (header totals are sums of *already-
# rounded* lines) then falls out for free, since `_validate_lines` sums
# these already-quantized values.
_MONEY_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)

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

    @field_validator("txn_debit", "txn_credit", "debit", "credit")
    @classmethod
    def _round_half_even_to_6dp(cls, value: Decimal) -> Decimal:
        return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


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
    """Internal transport type — accepted by `service.post_journal_entry`.

    Carries `source_type`/`source_id` because internal callers
    (`ledger.posting`, and transitively `receivables`/`sales` via events)
    need to set them to real values. **Never** bind this model directly to
    a public request body — see `JournalEntryCreateRequest` below, which is
    what `POST /journal-entries` actually accepts (ADR-008 R11).
    """

    entry_date: date
    source_type: str | None = Field(default=None, max_length=64)
    source_id: uuid.UUID | None = None
    # A balanced entry needs at least one debit line and one credit line —
    # a single line alone cannot balance unless it is degenerate (all
    # zeros), which service-layer validation also rejects.
    lines: list[JournalLineCreate] = Field(min_length=2)
    custom_data: dict[str, Any] = Field(default_factory=dict)


class JournalEntryCreateRequest(BaseModel):
    """Public, HTTP-facing DTO for `POST /journal-entries` (ADR-008 R11).

    Deliberately omits `source_type`/`source_id` — those are now
    server-only, set exclusively by internal callers that build
    `JournalEntryCreate` directly. Before this split, a client could set an
    arbitrary `source_type` on a manually-created entry to dodge the R5
    control-account check (which only applies when `source_type is None`)
    and, separately, could collide with a real event's own
    `(company_id, source_type, source_id)` idempotency key and silently
    block it from ever posting — this closes both at once.
    """

    entry_date: date
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
