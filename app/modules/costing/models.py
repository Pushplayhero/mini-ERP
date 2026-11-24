"""costing SQLAlchemy models — ADR-016 (moving weighted average, landed cost, PPV, revaluation).

Table summary:
- `CostingSequence`: `(company_id, year) -> next_no` counter (`COST-{YYYY}-{NNNNNN}`).
- `ProductCostSummary`: composite PK `(company_id, product_id)`. Like `StockSummary`
  and `LedgerSequence`, deliberately omits `TenantScopedMixin` because `company_id`
  already forms half of its primary key; every service and CLI query MUST filter
  explicitly on `ProductCostSummary.company_id == company_id`.
- `CostMove`: append-only cost valuation audit ledger (`TenantScopedMixin`).
- `CostAdjustment`: tenant-scoped independent cost adjustment document (`LANDED_COST`,
  `PPV`, `REVALUATION`) with `POSTED`/`VOIDED` lifecycle, custom fields, and workflow void gate.

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`, `app.modules.purchase`, `app.modules.inventory`,
`app.modules.sales`, `app.modules.receivables`, `app.modules.payables`, or
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


class CostMoveType(str, enum.Enum):
    RECEIPT = "receipt"
    SHIPMENT = "shipment"
    LANDED_COST = "landed_cost"
    PPV = "ppv"
    REVALUATION = "revaluation"
    PRODUCTION_ISSUE = "production_issue"
    PRODUCTION_RETURN = "production_return"
    PRODUCTION_RECEIPT = "production_receipt"


class CostAdjustmentType(str, enum.Enum):
    LANDED_COST = "landed_cost"
    PPV = "ppv"
    REVALUATION = "revaluation"


class CostAdjustmentStatus(str, enum.Enum):
    POSTED = "posted"
    VOIDED = "voided"


class CostingSequence(Base):
    """`(company_id, year) -> next_no` counter for COST-{YYYY}-{NNNNNN} numbers."""

    __tablename__ = "costing_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ProductCostSummary(Base):
    """Rebuildable moving weighted average valuation projection per `(company_id, product_id)`."""

    __tablename__ = "product_cost_summaries"
    __table_args__ = (
        CheckConstraint("tracked_qty >= 0", name="ck_product_cost_summaries_qty_nonneg"),
        CheckConstraint("total_cost_value >= 0", name="ck_product_cost_summaries_value_nonneg"),
        CheckConstraint("moving_avg_cost >= 0", name="ck_product_cost_summaries_avg_cost_nonneg"),
        CheckConstraint(
            "tracked_qty != 0 OR total_cost_value = 0",
            name="ck_product_cost_summaries_zero_qty_zero_value",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    tracked_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    total_cost_value: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    moving_avg_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")


class CostMove(Base, TenantScopedMixin):
    """Append-only ledger of inventory valuation moves."""

    __tablename__ = "cost_moves"
    __table_args__ = (
        CheckConstraint("unit_cost >= 0", name="ck_cost_moves_unit_cost_nonneg"),
        CheckConstraint("resulting_qty >= 0", name="ck_cost_moves_resulting_qty_nonneg"),
        CheckConstraint("resulting_value >= 0", name="ck_cost_moves_resulting_value_nonneg"),
        CheckConstraint("resulting_avg_cost >= 0", name="ck_cost_moves_resulting_avg_cost_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    move_type: Mapped[CostMoveType] = mapped_column(
        Enum(CostMoveType, name="cost_move_type", native_enum=True), nullable=False
    )
    qty_delta: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    value_delta: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    resulting_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    resulting_value: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    resulting_avg_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class CostAdjustment(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    """Posted and voidable cost adjustment documents (Landed Cost, PPV, Revaluation)."""

    __tablename__ = "cost_adjustments"
    __table_args__ = (
        UniqueConstraint("company_id", "adjustment_no", name="uq_cost_adjustments_company_no"),
        CheckConstraint("amount <> 0", name="ck_cost_adjustments_amount_nonzero"),
        CheckConstraint(
            "adjustment_type != 'LANDED_COST' OR amount > 0",
            name="ck_cost_adjustments_landed_cost_positive",
        ),
        CheckConstraint(
            "adjustment_type NOT IN ('LANDED_COST', 'PPV') OR order_id IS NOT NULL",
            name="ck_cost_adjustments_po_required",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_cost_adjustments_reason_nonempty"),
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_cost_adjustments_voided_at_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adjustment_no: Mapped[str] = mapped_column(String(32), nullable=False)
    adjustment_type: Mapped[CostAdjustmentType] = mapped_column(
        Enum(CostAdjustmentType, name="cost_adjustment_type", native_enum=True), nullable=False
    )
    status: Mapped[CostAdjustmentStatus] = mapped_column(
        Enum(CostAdjustmentStatus, name="cost_adjustment_status", native_enum=True), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    adjustment_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
