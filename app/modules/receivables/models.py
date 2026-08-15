"""receivables SQLAlchemy models — ADR-008 (invoicing, payment application, AR aging).

Table summary (normative per ADR-008 Decisions 1-3, R7, R13-R17):

- `ReceivablesSequence`: `(company_id, year, doc_type) -> next_no` counter,
  `doc_type` discriminating `"invoice"`/`"payment"` — same get-or-create +
  `FOR UPDATE` pattern as `sales.models.SalesSequence` /
  `ledger.models.LedgerSequence`, and like those two does not inherit
  `TenantScopedMixin` for the same reason (its own `company_id` already
  forms part of the composite primary key). Gaplessness is NOT required
  (internal document numbers; Taiwan's legally-numbered 發票字軌 is the
  Phase 4 `tw.einvoice` plugin's problem, not Phase 1's).

- `Invoice`: tenant-scoped, `UNIQUE(company_id, invoice_no)`. Issued 1:1
  from a `SHIPPED` sales order — `total`, `currency_code`, and the customer
  snapshot are copied at issue, never recomputed; `order_shipped_at` is a
  snapshot of the order's own `shipped_at` (copied because a Postgres
  `CHECK` cannot reach across tables — see `ck_invoices_invoice_date_after_
  shipment` in migration 0008). `settled_amount` is a maintained projection
  of `PaymentAllocation` rows (Decision 3), always updated under
  `SELECT ... FOR UPDATE` in the same transaction as the allocation rows
  that justify it — see `service.allocate_payment`. `status` derives from
  `settled_amount` (`open -> partial -> paid`); an exhaustive DB CHECK
  (migration 0008) backstops the status<->settled_amount relationship for
  every enum value, not just the ones the service layer happens to reach.
  `voided_at` is the only correction path (Decision 4) — void requires
  `settled_amount = 0`, publishes `receivables.invoice_voided`, and permits
  re-issuing the order (the partial unique index only covers non-voided
  invoices).

- `Payment`: tenant-scoped, `UNIQUE(company_id, payment_no)` +
  `UNIQUE(company_id, external_ref)`. `external_ref` is a required,
  client-supplied idempotency key (R2) — a retried `POST /payments` hits
  that constraint and 409s instead of double-posting Cash/AR.
  `allocated_amount` is the payment-side mirror of `Invoice.settled_amount`,
  maintained the same way. Void (Decision 4) requires `allocated_amount =
  0` and publishes `receivables.payment_voided`.

- `PaymentAllocationCommand`: one row per distinct `allocate_payment` call
  (R14, replacing a flawed row-level-uniqueness design from an earlier
  review round — see ADR-008's Consensus Revisions for the full story).
  `UNIQUE(company_id, payment_id, request_ref)` — an exact retry (same
  `request_ref`, same body) is detected by comparing `request_fingerprint`
  at the service layer and replayed idempotently; a reused `request_ref`
  with a different body hits this constraint and 409s as the contract
  violation it always should have been.

- `PaymentAllocation`: tenant-scoped, append-only fact table (§10.5's stock
  pattern transplanted to receivables) — `Invoice.settled_amount` and
  `Payment.allocated_amount` are always rebuildable from `SUM(amount)`
  grouped by `invoice_id`/`payment_id` respectively (see
  `app.cli.rebuild_ar_balances`). Every row belongs to exactly one
  `PaymentAllocationCommand`.

Independence-boundary note (import-linter): this module must never import
`app.modules.sales`, `app.modules.masterdata`, or `app.modules.ledger`.
`service.py` resolves order/customer data via lightweight
`sqlalchemy.table()` Core references instead — same pattern
`sales.service`/`ledger.service`/`ledger.posting` already use; see those
modules' docstrings for the full rationale. The four posting-event payload
schemas this module's events drive live in `ledger.posting` (next to the
rules that consume them), not here — see that module's docstring for why,
following the `sales.goods_shipped`/`GoodsShippedPayload` precedent.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, CustomDataMixin, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)


class InvoiceStatus(str, enum.Enum):
    OPEN = "open"
    PARTIAL = "partial"
    PAID = "paid"
    VOIDED = "voided"


class PaymentStatus(str, enum.Enum):
    RECEIVED = "received"
    VOIDED = "voided"


# ---------------------------------------------------------------------------
# Document numbering (not required to be gapless — ADR-008 Decision 1)
# ---------------------------------------------------------------------------


class ReceivablesSequence(Base):
    """`(company_id, year, doc_type) -> next_no` counter. See module

    docstring for why this does not inherit `TenantScopedMixin`.
    """

    __tablename__ = "receivables_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


# ---------------------------------------------------------------------------
# Invoices (ADR-008 Decision 1, R13)
# ---------------------------------------------------------------------------


class Invoice(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("company_id", "invoice_no", name="uq_invoices_company_invoice_no"),
        CheckConstraint("total > 0", name="ck_invoices_total_positive"),
        CheckConstraint(
            "settled_amount >= 0 AND settled_amount <= total",
            name="ck_invoices_settled_amount_bounds",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND settled_amount = 0) "
            "OR (status = 'PARTIAL' AND settled_amount > 0 AND settled_amount < total) "
            "OR (status = 'PAID' AND settled_amount = total) "
            "OR (status = 'VOIDED' AND settled_amount = 0)",
            name="ck_invoices_status_settled_amount_consistency",
        ),
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_invoices_voided_at_consistency",
        ),
        CheckConstraint("due_date >= invoice_date", name="ck_invoices_due_date_after_invoice_date"),
        CheckConstraint(
            "invoice_date >= order_shipped_at::date",
            name="ck_invoices_invoice_date_after_shipment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status", native_enum=True), nullable=False
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    # Snapshot of the source order's `shipped_at` at issue time (R13) — see
    # module docstring for why this is copied rather than joined.
    order_shipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    total: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    settled_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    snapshot_customer_code: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Payments + allocations (ADR-008 Decision 2/3, R2, R14)
# ---------------------------------------------------------------------------


class Payment(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("company_id", "payment_no", name="uq_payments_company_payment_no"),
        UniqueConstraint("company_id", "external_ref", name="uq_payments_company_external_ref"),
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "allocated_amount >= 0 AND allocated_amount <= amount",
            name="ck_payments_allocated_amount_bounds",
        ),
        CheckConstraint(
            "status != 'VOIDED' OR allocated_amount = 0", name="ck_payments_voided_zero_allocated"
        ),
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_payments_voided_at_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_no: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", native_enum=True), nullable=False
    )
    # Client-supplied idempotency key (R2) — see module docstring.
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentAllocationCommand(Base, TenantScopedMixin):
    """One row per distinct `service.allocate_payment` call (R14). See

    module docstring for the idempotency contract this table implements.
    """

    __tablename__ = "payment_allocation_commands"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "payment_id",
            "request_ref",
            name="uq_payment_allocation_commands_payment_request_ref",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    )
    request_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentAllocation(Base, TenantScopedMixin):
    """Append-only fact table (§10.5's stock pattern). See module docstring."""

    __tablename__ = "payment_allocations"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_payment_allocations_amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payment_allocation_commands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
