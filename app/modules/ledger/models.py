"""ledger SQLAlchemy models — ADR-005 (journal entries, periods, trial balance).

Everything here is subordinate to the invariants ADR-005 exists to
guarantee; see the migration (`alembic/versions/0002_ledger_initial.py`) for
the DB-level half of that guarantee (CHECK constraints, the deferred balance
constraint trigger, and the unconditional immutability triggers) — the ORM
models below only describe the *shape*, not the enforcement, of the tables.

Table summary (all normative per ADR-005 R1-R4):

- `AccountingPeriod`: tenant-scoped, `UNIQUE(company_id, year, month)`.
  Created open; closing requires `SELECT ... FOR UPDATE` (R4) — see
  `service.close_period`.
- `LedgerSequence`: one row per `(company_id, year)`, `next_no` is the
  gapless counter for `entry_no` allocation (R2). Composite primary key
  instead of a surrogate id — this table has no independent identity beyond
  the counter itself and is never exposed via any read API, so it
  deliberately does *not* inherit `TenantScopedMixin` (that mixin adds its
  own `company_id` column, which would collide with the one already forming
  half of this table's primary key). Every access goes through
  `service._allocate_entry_no`, which always scopes by the caller's
  `company_id` explicitly — see that function's docstring for the isolation
  argument.
- `JournalEntry`: tenant-scoped, immutable (Decision 3 — DB trigger blocks
  UPDATE/DELETE unconditionally). `reversal_of_id` is nullable + `UNIQUE`
  (R3: an entry can be reversed at most once). `source_type`/`source_id`
  are reserved for Week 3's posting engine; nullable this week.
- `JournalLine`: dual-currency per R1 — `txn_debit`/`txn_credit` (line's
  transaction currency) alongside `debit`/`credit` (company's functional
  currency, the pair the balance trigger sums). Also tenant-scoped
  (`company_id` denormalized from the parent entry) even though ADR-005's
  table sketch doesn't spell this column out explicitly: without it, a bare
  `select(JournalLine)` (as opposed to one joined through `JournalEntry`)
  would not be recognized as tenant-scoped by `app.core.db`'s
  `do_orm_execute` hook and would run unfiltered instead of failing closed —
  exactly the silent-leak failure mode master-plan §10.2 is designed to
  prevent. See the Week 2 completion report for the full trade-off note.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, CustomDataMixin
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)
RATE = Numeric(20, 10)


class PeriodStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# Accounting periods
# ---------------------------------------------------------------------------


class AccountingPeriod(Base, TenantScopedMixin, CustomDataMixin):
    __tablename__ = "accounting_periods"
    __table_args__ = (
        UniqueConstraint("company_id", "year", "month", name="uq_accounting_periods_company_ym"),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_accounting_periods_month_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PeriodStatus] = mapped_column(
        Enum(PeriodStatus, name="period_status", native_enum=True),
        nullable=False,
        server_default=PeriodStatus.OPEN.name,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


# ---------------------------------------------------------------------------
# Gapless entry numbering (ADR-005 R2)
# ---------------------------------------------------------------------------


class LedgerSequence(Base):
    """`(company_id, year) -> next_no` counter. See module docstring for why

    this does not inherit `TenantScopedMixin`.
    """

    __tablename__ = "ledger_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


# ---------------------------------------------------------------------------
# Journal entries / lines (ADR-005 Decisions 1-3, R1-R3)
# ---------------------------------------------------------------------------


class JournalEntry(Base, TenantScopedMixin, CustomDataMixin):
    """Immutable once created — see the `BEFORE UPDATE OR DELETE` trigger in

    the migration. No `updated_at`/`updated_by` columns: a row that
    structurally can never be updated has no meaningful "last updated"
    timestamp, and carrying dead columns that would silently never change
    seemed more confusing than omitting them (documented in the Week 2
    completion report as a deliberate deviation from `TimestampAuditMixin`).
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("company_id", "entry_no", name="uq_journal_entries_company_entry_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_no: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Reserved for Week 3's posting engine (business event -> journal entry
    # traceability). Columns exist this week per the brief; not populated or
    # validated by anything in Week 2.
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    lines: Mapped[list[JournalLine]] = relationship(
        back_populates="entry", order_by="JournalLine.line_no", cascade="all, delete-orphan"
    )


class JournalLine(Base, TenantScopedMixin):
    """Dual-currency per ADR-005 R1. See migration for the CHECK constraints

    ("one side only" per pair, and the two pairs' nonzero sides must agree)
    and the deferred balance constraint trigger that sums `debit`/`credit`
    (the functional-currency pair) per entry at commit.
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        UniqueConstraint("entry_id", "line_no", name="uq_journal_lines_entry_line_no"),
        CheckConstraint("txn_debit >= 0 AND txn_credit >= 0", name="ck_journal_lines_txn_nonneg"),
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_journal_lines_fn_nonneg"),
        CheckConstraint(
            "txn_debit = 0 OR txn_credit = 0", name="ck_journal_lines_txn_one_side_only"
        ),
        CheckConstraint("debit = 0 OR credit = 0", name="ck_journal_lines_fn_one_side_only"),
        CheckConstraint(
            "(txn_debit > 0) = (debit > 0) AND (txn_credit > 0) = (credit > 0)",
            name="ck_journal_lines_sides_agree",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    txn_debit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    txn_credit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    debit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    credit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    exchange_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False, server_default="1")
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
