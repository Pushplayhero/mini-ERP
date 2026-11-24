"""mrp SQLAlchemy models — ADR-017 (multi-level BOM, work orders, WIP subledger).

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`, `app.modules.sales`, `app.modules.purchase`,
`app.modules.inventory`, `app.modules.costing`, `app.modules.receivables`,
`app.modules.payables`, or `app.modules.ledger`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean as SQLBoolean,
)
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, CustomDataMixin, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)


class WorkOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkOrderTxType(str, enum.Enum):
    MATERIAL_ISSUE = "material_issue"
    MATERIAL_RETURN = "material_return"
    PRODUCTION_RECEIPT = "production_receipt"


class MrpSequence(Base):
    """`(company_id, year) -> next_no` counter (`WO-{YYYY}-{NNNNNN}`).

    Deliberately omits `TenantScopedMixin` because `company_id` forms part of
    the composite PK.
    """

    __tablename__ = "mrp_sequences"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Bom(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    """Bill of Materials header defining components required to build `output_qty` of a product."""

    __tablename__ = "boms"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_boms_company_code"),
        Index(
            "uq_boms_company_product_active",
            "company_id",
            "product_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        CheckConstraint("output_qty > 0", name="ck_boms_output_qty_positive"),
        CheckConstraint("length(btrim(code)) > 0", name="ck_boms_code_nonempty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    output_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        SQLBoolean, nullable=False, default=True, server_default="true"
    )
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    lines: Mapped[list[BomLine]] = relationship(
        back_populates="bom", order_by="BomLine.line_no", cascade="all, delete-orphan"
    )


class BomLine(Base, TenantScopedMixin):
    """Single component line inside a Bill of Materials."""

    __tablename__ = "bom_lines"
    __table_args__ = (
        UniqueConstraint("bom_id", "line_no", name="uq_bom_lines_bom_line_no"),
        UniqueConstraint("bom_id", "component_product_id", name="uq_bom_lines_bom_component"),
        CheckConstraint("line_no >= 1", name="ck_bom_lines_line_no_positive"),
        CheckConstraint("qty_per > 0", name="ck_bom_lines_qty_per_positive"),
        CheckConstraint(
            "scrap_rate >= 0 AND scrap_rate < 1", name="ck_bom_lines_scrap_rate_bounds"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("boms.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    component_product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    qty_per: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    scrap_rate: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    bom: Mapped[Bom] = relationship(back_populates="lines")


class WorkOrder(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    """Manufacturing work order header tracking status, quantities, and WIP balance."""

    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("company_id", "wo_no", name="uq_work_orders_company_wo_no"),
        CheckConstraint("planned_qty > 0", name="ck_work_orders_planned_qty_positive"),
        CheckConstraint(
            "completed_qty >= 0 AND completed_qty <= planned_qty",
            name="ck_work_orders_completed_qty_bounds",
        ),
        CheckConstraint("wip_balance >= 0", name="ck_work_orders_wip_balance_nonneg"),
        CheckConstraint(
            "status::text != 'COMPLETED' OR (wip_balance = 0 AND completed_at IS NOT NULL)",
            name="ck_work_orders_completed_zero_wip",
        ),
        CheckConstraint(
            "status::text != 'CANCELLED' "
            "OR (wip_balance = 0 AND completed_qty = 0 AND cancelled_at IS NOT NULL)",
            name="ck_work_orders_cancelled_clean",
        ),
        CheckConstraint(
            "status::text NOT IN ('CONFIRMED', 'IN_PROGRESS', 'COMPLETED') "
            "OR (snapshot_sku IS NOT NULL AND snapshot_product_name IS NOT NULL "
            "AND confirmed_at IS NOT NULL)",
            name="ck_work_orders_confirmed_has_snapshot",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wo_no: Mapped[str] = mapped_column(String(32), nullable=False)
    bom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("boms.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    status: Mapped[WorkOrderStatus] = mapped_column(
        Enum(WorkOrderStatus, name="work_order_status", native_enum=True),
        nullable=False,
        server_default=WorkOrderStatus.DRAFT.name,
    )
    planned_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    completed_qty: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    wip_balance: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    snapshot_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    lines: Mapped[list[WorkOrderLine]] = relationship(
        back_populates="work_order", order_by="WorkOrderLine.line_no", cascade="all, delete-orphan"
    )


class WorkOrderLine(Base, TenantScopedMixin):
    """Required component material allocation and cumulative issue/return tracking for a WO."""

    __tablename__ = "work_order_lines"
    __table_args__ = (
        UniqueConstraint("work_order_id", "line_no", name="uq_work_order_lines_wo_line_no"),
        UniqueConstraint(
            "work_order_id", "component_product_id", name="uq_work_order_lines_wo_component"
        ),
        CheckConstraint("line_no >= 1", name="ck_work_order_lines_line_no_positive"),
        CheckConstraint("required_qty > 0", name="ck_work_order_lines_required_qty_positive"),
        CheckConstraint(
            "issued_qty >= 0 AND returned_qty >= 0 AND returned_qty <= issued_qty",
            name="ck_work_order_lines_issued_return_bounds",
        ),
        CheckConstraint(
            "issued_cost >= 0 AND returned_cost >= 0 AND returned_cost <= issued_cost "
            "AND estimated_unit_cost >= 0",
            name="ck_work_order_lines_cost_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    component_product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    required_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    issued_qty: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    returned_qty: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    issued_cost: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    returned_cost: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    estimated_unit_cost: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal("0"), server_default="0"
    )
    snapshot_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    work_order: Mapped[WorkOrder] = relationship(back_populates="lines")


class WorkOrderTransaction(Base, TenantScopedMixin):
    """Append-only idempotent shop-floor transaction fact log (issue, return, receipt)."""

    __tablename__ = "work_order_transactions"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "work_order_id", "request_ref", name="uq_work_order_tx_wo_request_ref"
        ),
        CheckConstraint("total_cost >= 0", name="ck_work_order_tx_total_cost_nonneg"),
        CheckConstraint(
            "length(btrim(request_ref)) > 0", name="ck_work_order_tx_request_ref_nonempty"
        ),
        CheckConstraint(
            "received_qty IS NULL OR received_qty > 0",
            name="ck_work_order_tx_received_qty_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT"), nullable=False
    )
    tx_type: Mapped[WorkOrderTxType] = mapped_column(
        Enum(WorkOrderTxType, name="work_order_tx_type", native_enum=True), nullable=False
    )
    request_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    received_qty: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    lines_payload: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
