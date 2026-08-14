"""inventory SQLAlchemy models — ADR-007 Decision 2 (append-only moves +
rebuildable summary).

Table summary:

- `StockMove`: append-only, immutable by application convention (nothing in
  this codebase ever UPDATEs or DELETEs a row — see `service.apply_stock_move`,
  the only writer). Inherits `TenantScopedMixin`, the same choice
  `ledger.models.JournalLine` and `sales.models.SalesOrderLine` make for the
  identical reason: this table is queried directly (`service.list_moves`),
  not only ever reached by joining through a parent, so without an explicit
  `company_id` column `app.core.db`'s automatic tenant filter (which only
  recognizes `TenantScopedMixin` subclasses) would never see a bare
  `select(StockMove)` as tenant-scoped and would run it unfiltered instead
  of failing closed — exactly the silent-leak failure mode master-plan
  §10.2 exists to prevent. `qty_delta` is signed (positive = stock in,
  negative = stock out); the running balance lives in `StockSummary` below,
  always maintained in the same transaction as the insert
  (`service.apply_stock_move`), and is fully rebuildable at any time from
  `SUM(qty_delta)` (`app.cli.rebuild_stock_summary`).

- `StockSummary`: composite primary key `(company_id, product_id)`, the
  same shape `ledger.models.LedgerSequence` / `sales.models.SalesSequence`
  use — and, like those two, deliberately does **not** inherit
  `TenantScopedMixin`: the mixin's own `company_id` column would collide
  with the one already forming half of this table's primary key, and this
  table (again like those two) has no surrogate identity anything ever
  references by id, only by its natural key. The one place this table's
  trade-off differs from `LedgerSequence`/`SalesSequence`: THIS table IS
  exposed read-only via `GET /inventory/summary`, whereas the two sequence
  tables are never exposed via any read API and are always accessed already
  scoped by their one caller. That means every service-layer query against
  `StockSummary` must explicitly filter `company_id ==
  require_current_company_id()` itself — the same belt-and-suspenders
  explicit predicate `ledger.service.get_trial_balance` already applies to
  its Core-table `accounts` join, for the same reason (no automatic ORM
  hook covers this table). This is a documented trade-off, not an oversight
  — see `service.py` for where the explicit filter is applied on every read.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)


class StockMoveType(str, enum.Enum):
    SHIPMENT = "shipment"
    ADJUSTMENT = "adjustment"


class StockMove(Base, TenantScopedMixin):
    """Append-only ledger of stock quantity changes. See module docstring."""

    __tablename__ = "stock_moves"
    __table_args__ = (CheckConstraint("qty_delta <> 0", name="ck_stock_moves_qty_delta_nonzero"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    qty_delta: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    move_type: Mapped[StockMoveType] = mapped_column(
        Enum(StockMoveType, name="stock_move_type", native_enum=True), nullable=False
    )
    # Reserved for replay idempotency (ADR-007 Decision 4): NULL for manual
    # adjustments (no replay path, no dedup needed — excluded from
    # `uq_stock_moves_source`'s partial index by the same `WHERE source_type
    # IS NOT NULL` clause `uq_journal_entries_source` uses), set to the
    # publishing `event_type` (e.g. `"sales.goods_shipped"`) for
    # bus-originated moves.
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class StockSummary(Base):
    """Rebuildable `on_hand` projection per `(company_id, product_id)`. See
    module docstring for the `TenantScopedMixin` trade-off discussion.
    """

    __tablename__ = "stock_summary"
    __table_args__ = (CheckConstraint("on_hand >= 0", name="ck_stock_summary_on_hand_nonneg"),)

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    on_hand: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
