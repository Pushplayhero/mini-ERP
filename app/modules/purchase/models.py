"""purchase SQLAlchemy models — ADR-014 (purchase order lifecycle).

Table summary:
- `PurchaseSequence`: one row per `(company_id, year)` -> `next_no`, allocating
  `PO-{YYYY}-{NNNNNN}` under `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE`.
- `PurchaseOrder`: tenant-scoped, `UNIQUE(company_id, order_no)`. State machine:
  `draft -> confirmed -> received`, and `draft|confirmed -> cancelled`.
- `PurchaseOrderLine`: tenant-scoped line items referencing `products`, carrying
  `qty > 0`, `unit_cost >= 0`, `amount >= 0`, `unit_cost_is_override`, and SKU/name snapshots.

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`. `service.py` resolves supplier/product data via
lightweight `sqlalchemy.table()` Core references with explicit `company_id` filters.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean as SQLBoolean,
)
from sqlalchemy import (
    CheckConstraint,
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

from app.core.db import Base, CustomDataMixin, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)


class PurchaseOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    RECEIVED = "received"


class PurchaseSequence(Base):
    """`(company_id, year) -> next_no` counter. Does not inherit `TenantScopedMixin`
    because `company_id` is part of the composite primary key.
    """

    __tablename__ = "purchase_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class PurchaseOrder(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("company_id", "order_no", name="uq_purchase_orders_company_order_no"),
        CheckConstraint("total >= 0", name="ck_purchase_orders_total_nonneg"),
        CheckConstraint(
            "status::text NOT IN ('CONFIRMED', 'RECEIVED') "
            "OR (snapshot_supplier_code IS NOT NULL AND snapshot_supplier_name IS NOT NULL)",
            name="ck_purchase_orders_confirmed_has_snapshot",
        ),
        CheckConstraint(
            "status::text != 'RECEIVED' OR received_at IS NOT NULL",
            name="ck_purchase_orders_received_has_received_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, name="purchase_order_status", native_enum=True),
        nullable=False,
        server_default=PurchaseOrderStatus.DRAFT.name,
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    total: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    snapshot_supplier_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_supplier_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="order", order_by="PurchaseOrderLine.line_no", cascade="all, delete-orphan"
    )


class PurchaseOrderLine(Base, TenantScopedMixin):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_purchase_order_lines_order_line_no"),
        CheckConstraint("qty > 0", name="ck_purchase_order_lines_qty_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_purchase_order_lines_unit_cost_nonneg"),
        CheckConstraint("amount >= 0", name="ck_purchase_order_lines_amount_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    unit_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unit_cost_is_override: Mapped[bool] = mapped_column(
        SQLBoolean, nullable=False, default=False, server_default="false"
    )
    snapshot_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
