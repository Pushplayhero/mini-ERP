"""sales SQLAlchemy models — ADR-006 (order lifecycle, hook registry demo).

Table summary (normative per ADR-006 Decision 3, R1, R2):

- `SalesSequence`: one row per `(company_id, year)`, `next_no` is the
  counter `order_no` allocation reads/increments (see
  `service._allocate_order_no`). Same get-or-create + `FOR UPDATE` pattern
  as `ledger.models.LedgerSequence` — and, like that table, does *not*
  inherit `TenantScopedMixin` for the same reason (its own `company_id`
  already forms half of the composite primary key; adding the mixin's
  column would collide). Unlike `ledger_sequences`, gaplessness is
  explicitly NOT required for `order_no` (ADR-006 Decision 3) — a
  cancelled/rolled-back allocation simply leaves a gap, which is
  documented as acceptable.
- `SalesOrder`: tenant-scoped, `UNIQUE(company_id, order_no)`. `status` is
  a server-enforced state machine (`draft -> confirmed`,
  `draft|confirmed -> cancelled`); `confirm`/`cancel` both take
  `SELECT ... FOR UPDATE` on this row before re-checking `status` (R1) —
  see `service.confirm_order`/`service.cancel_order`. `total` is always
  server-computed from `lines`, never client-supplied. `snapshot_customer_*`
  are filled at create/update time (so a draft has a readable label without
  a join) and unconditionally re-copied from current masterdata at confirm
  time, which is the point ADR-006 actually calls "frozen" — once
  `status=confirmed`, nothing mutates the row again (Week 4 has no
  order-edit-after-confirm path), so whatever the snapshot says at the
  moment `confirm` commits is permanent.
- `SalesOrderLine`: tenant-scoped even though it always lives under one
  `SalesOrder` — same reasoning as `ledger.models.JournalLine` denormalizing
  `company_id` from its parent: without it, a bare `select(SalesOrderLine)`
  (as opposed to one joined through `SalesOrder`) would not be recognized as
  tenant-scoped by `app.core.db`'s `do_orm_execute` hook and would run
  unfiltered instead of failing closed. `qty > 0` is enforced by a CHECK
  constraint at the DB layer (see migration 0004) in addition to the
  pydantic schema, per this project's established "invariants belong at the
  DB layer too" doctrine (ADR-005).

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`. `service.py` resolves customer/product data via
lightweight `sqlalchemy.table()` Core references instead — same pattern
`ledger.service`/`ledger.posting` already use for `accounts`; see those
modules' docstrings for the full rationale.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

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


class SalesOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Order numbering (not required to be gapless — ADR-006 Decision 3)
# ---------------------------------------------------------------------------


class SalesSequence(Base):
    """`(company_id, year) -> next_no` counter. See module docstring for why

    this does not inherit `TenantScopedMixin`.
    """

    __tablename__ = "sales_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


# ---------------------------------------------------------------------------
# Sales orders / lines (ADR-006 Decision 3, R1, R2)
# ---------------------------------------------------------------------------


class SalesOrder(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("company_id", "order_no", name="uq_sales_orders_company_order_no"),
        CheckConstraint("total >= 0", name="ck_sales_orders_total_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[SalesOrderStatus] = mapped_column(
        Enum(SalesOrderStatus, name="sales_order_status", native_enum=True),
        nullable=False,
        server_default=SalesOrderStatus.DRAFT.name,
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    total: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    snapshot_customer_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[SalesOrderLine]] = relationship(
        back_populates="order", order_by="SalesOrderLine.line_no", cascade="all, delete-orphan"
    )


class SalesOrderLine(Base, TenantScopedMixin):
    __tablename__ = "sales_order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_sales_order_lines_order_line_no"),
        CheckConstraint("qty > 0", name="ck_sales_order_lines_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_sales_order_lines_unit_price_nonneg"),
        CheckConstraint("amount >= 0", name="ck_sales_order_lines_amount_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    unit_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    snapshot_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[SalesOrder] = relationship(back_populates="lines")
