"""payables SQLAlchemy models — ADR-015 (vendor bills, outgoing payments, AP aging).

Table summary (normative per ADR-015 Decisions 1-4):
- `PayablesSequence`: `(company_id, year, doc_type) -> next_no` counter (`bill` / `payment`),
  mirroring `ReceivablesSequence` (composite key already starts with `company_id`).
- `VendorBill`: tenant-scoped, `UNIQUE(company_id, bill_no)`, plus partial unique indexes
  `uq_vendor_bills_order_live` and `uq_vendor_bills_supplier_invoice_no_live` created in
  migration `0016_payables`. Issued 1:1 from a `RECEIVED` purchase order.
- `VendorPayment`: tenant-scoped, `UNIQUE(company_id, payment_no)` and
  `UNIQUE(company_id, external_ref)` retry-idempotency key.
- `VendorPaymentAllocationCommand`: one row per distinct allocation command (`request_ref` +
  `request_fingerprint`).
- `VendorPaymentAllocation`: tenant-scoped append-only subledger allocation fact table.

Independence-boundary note (import-linter): this module must never import
`app.modules.purchase`, `app.modules.masterdata`, `app.modules.receivables`, or
`app.modules.ledger`.
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


class VendorBillStatus(str, enum.Enum):
    OPEN = "open"
    PARTIAL = "partial"
    PAID = "paid"
    VOIDED = "voided"


class VendorPaymentStatus(str, enum.Enum):
    ISSUED = "issued"
    VOIDED = "voided"


class PayablesSequence(Base):
    """`(company_id, year, doc_type) -> next_no` counter for BILL/APAY numbers."""

    __tablename__ = "payables_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class VendorBill(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "vendor_bills"
    __table_args__ = (
        UniqueConstraint("company_id", "bill_no", name="uq_vendor_bills_company_bill_no"),
        CheckConstraint("total > 0", name="ck_vendor_bills_total_positive"),
        CheckConstraint(
            "settled_amount >= 0 AND settled_amount <= total",
            name="ck_vendor_bills_settled_amount_bounds",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND settled_amount = 0) "
            "OR (status = 'PARTIAL' AND settled_amount > 0 AND settled_amount < total) "
            "OR (status = 'PAID' AND settled_amount = total) "
            "OR (status = 'VOIDED' AND settled_amount = 0)",
            name="ck_vendor_bills_status_settled_amount_consistency",
        ),
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_vendor_bills_voided_at_consistency",
        ),
        CheckConstraint("due_date >= bill_date", name="ck_vendor_bills_due_date_after_bill_date"),
        CheckConstraint(
            "bill_date >= (order_received_at AT TIME ZONE 'UTC')::date",
            name="ck_vendor_bills_bill_date_after_receipt",
        ),
        CheckConstraint(
            "supplier_invoice_no IS NULL OR length(btrim(supplier_invoice_no)) > 0",
            name="ck_vendor_bills_invoice_no_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bill_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_invoice_no: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[VendorBillStatus] = mapped_column(
        Enum(VendorBillStatus, name="vendor_bill_status", native_enum=True), nullable=False
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    order_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bill_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    total: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    settled_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    snapshot_supplier_code: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_supplier_name: Mapped[str] = mapped_column(String(255), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorPayment(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "vendor_payments"
    __table_args__ = (
        UniqueConstraint("company_id", "payment_no", name="uq_vendor_payments_company_payment_no"),
        UniqueConstraint(
            "company_id", "external_ref", name="uq_vendor_payments_company_external_ref"
        ),
        CheckConstraint("amount > 0", name="ck_vendor_payments_amount_positive"),
        CheckConstraint(
            "allocated_amount >= 0 AND allocated_amount <= amount",
            name="ck_vendor_payments_allocated_amount_bounds",
        ),
        CheckConstraint(
            "status != 'VOIDED' OR allocated_amount = 0",
            name="ck_vendor_payments_voided_zero_allocated",
        ),
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_vendor_payments_voided_at_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_no: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[VendorPaymentStatus] = mapped_column(
        Enum(VendorPaymentStatus, name="vendor_payment_status", native_enum=True), nullable=False
    )
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorPaymentAllocationCommand(Base, TenantScopedMixin):
    """One row per distinct `service.allocate_payment` call (ADR-015 / ADR-008 R14)."""

    __tablename__ = "vendor_payment_allocation_commands"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "payment_id",
            "request_ref",
            name="uq_vendor_payment_alloc_cmds_payment_request_ref",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("vendor_payments.id", ondelete="RESTRICT"), nullable=False
    )
    request_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VendorPaymentAllocation(Base, TenantScopedMixin):
    """Append-only vendor payment allocation fact table."""

    __tablename__ = "vendor_payment_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_vendor_payment_allocations_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("vendor_payments.id", ondelete="RESTRICT"), nullable=False
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("vendor_bills.id", ondelete="RESTRICT"), nullable=False
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("vendor_payment_allocation_commands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
