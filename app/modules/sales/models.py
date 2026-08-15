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


class SalesOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    # ADR-007 "Order lifecycle update": `confirmed -> shipped` via
    # `service.ship_order`. `cancel` stops being legal once an order is
    # `shipped` (the goods left; Phase 2+ returns/RMA is the undo
    # mechanism, not cancel) — see `service.cancel_order`'s updated
    # docstring. Added to the native Postgres enum by migration 0005's
    # `ALTER TYPE ... ADD VALUE` (R3: see that migration for why nothing
    # else in it may reference this value).
    SHIPPED = "shipped"


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
        # Diff-review fix (migration 0006): DB-layer backstop for ADR-006
        # Decision 3's "snapshots frozen at confirm" promise — a bypass
        # writer could otherwise persist a CONFIRMED (or SHIPPED, which can
        # only be reached via CONFIRMED) order with a blank customer
        # snapshot, which `sales.service.confirm_order`'s own write path
        # never allows. `status::text` (not `'SHIPPED'::sales_order_status`)
        # — see migration 0006's docstring for why: this project's
        # `alembic upgrade head` runs every pending migration in one
        # transaction, and 'SHIPPED' is a new enum value added by migration
        # 0005, so a typed comparison against it here is "unsafe" (per
        # Postgres) in that same transaction; text comparison sidesteps it.
        CheckConstraint(
            "status::text NOT IN ('CONFIRMED', 'SHIPPED') "
            "OR (snapshot_customer_code IS NOT NULL AND snapshot_customer_name IS NOT NULL)",
            name="ck_sales_orders_confirmed_has_snapshot",
        ),
        # ADR-008 R13/R17: `shipped_at` must be set on every SHIPPED order —
        # this is what makes receivables' `invoice_date >= order_shipped_at`
        # rule (Decision 1) enforceable at all. `status::text` (not a typed
        # comparison), same reason as the constraint above: migration 0008
        # runs in the same one-transaction `upgrade head` as migration
        # 0005's `ALTER TYPE ... ADD VALUE 'SHIPPED'`, so a typed comparison
        # against that literal here would be "unsafe" per Postgres in that
        # same transaction.
        CheckConstraint(
            "status::text != 'SHIPPED' OR shipped_at IS NOT NULL",
            name="ck_sales_orders_shipped_has_shipped_at",
        ),
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
    # ADR-008 R13/R17: nullable — only `SHIPPED` orders ever have a shipment
    # moment; draft/confirmed/cancelled orders never do. Set by
    # `service.ship_order` alongside the `confirmed -> shipped` transition.
    # Migration 0008 backfills this for any pre-existing SHIPPED row from
    # that order's own `sales.goods_shipped` outbox payload before adding
    # the CHECK constraint above.
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    # Diff-review fix (migration 0007): distinguishes "client explicitly set
    # unit_price" (a manual quote override, ADR-006 P3) from "service
    # defaulted it from product.list_price" — without this flag,
    # `confirm_order` cannot tell the two apart and so cannot honor ADR-006
    # Decision 3's "confirms at current prices unless lines carried manual
    # overrides" for the only case that matters (a defaulted line whose
    # product's price changed since the line was written).
    unit_price_is_override: Mapped[bool] = mapped_column(
        SQLBoolean, nullable=False, default=False, server_default="false"
    )
    snapshot_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[SalesOrder] = relationship(back_populates="lines")
