"""masterdata SQLAlchemy models.

Two families of tables, deliberately split (see README "Design Decisions"):

- **Tenant-scoped** (`Customer`, `Product`, `Account`): inherit
  `TenantScopedMixin` + `TimestampAuditMixin` + `CustomDataMixin`. Every read
  is auto-filtered to the active company by `app.core.db`'s
  `do_orm_execute` hook; `company_id` is never settable by API clients.
- **Global reference data** (`Uom`, `UomConversion`, `Currency`,
  `ExchangeRate`): shared across all companies (a kilogram is a kilogram
  everywhere; ISO 4217 currencies are not tenant-specific). Not
  tenant-scoped, still carry `created_at`/`updated_at` for audit purposes.

`Company` is the tenant root itself and is therefore never tenant-scoped.

`OutboxEvent` schema follows master-plan §10.4 exactly (write-side only this
week; dispatcher is Phase 2).

All monetary/quantity columns are `NUMERIC(20, 6)` per master-plan §10.1;
`ExchangeRate.rate` is `NUMERIC(20, 10)` (also §10.1 — rates need more
precision than amounts).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean as SQLBoolean,
)
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, CustomDataMixin, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin

AMOUNT = Numeric(20, 6)
RATE = Numeric(20, 10)


class AccountType(str, enum.Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


# ---------------------------------------------------------------------------
# Tenant root
# ---------------------------------------------------------------------------


class Company(Base, TimestampAuditMixin, CustomDataMixin):
    """The multi-company tenant root. Never itself tenant-scoped."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    functional_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Tenant-scoped master data
# ---------------------------------------------------------------------------


class Customer(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_customers_company_code"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    credit_limit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)


class Product(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("company_id", "sku", name="uq_products_company_sku"),
        # Diff-review fix on ADR-007 Decision 3: DB-layer backstop for
        # `ProductCreate`/`ProductUpdate`'s `standard_cost: ... = Field(ge=0)`
        # — same belt-and-suspenders pattern as `ck_stock_summary_on_hand_nonneg`.
        # Added in migration 0005 alongside the column itself.
        CheckConstraint("standard_cost >= 0", name="ck_products_standard_cost_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    list_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    # ADR-007 Decision 3 / Consensus P3: the COGS cost basis `sales.service
    # .ship_order` snapshots into `sales.goods_shipped`'s payload at ship
    # time. Additive column (default 0, existing rows/tests unaffected) —
    # Phase 3's moving-average/FIFO costing supersedes this; standard
    # costing is Phase 1's deliberately simple, honest stand-in (not a
    # hack), per ADR-007's trade-off analysis.
    standard_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)

    uom: Mapped[Uom] = relationship(lazy="joined")


class Account(Base, TenantScopedMixin, TimestampAuditMixin, CustomDataMixin):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_accounts_company_code"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type", native_enum=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Global reference data (shared across companies — not tenant-scoped)
# ---------------------------------------------------------------------------


class Uom(Base):
    __tablename__ = "uom"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UomConversion(Base):
    """`qty_in(from_uom) * factor = qty_in(to_uom)`."""

    __tablename__ = "uom_conversions"
    __table_args__ = (
        UniqueConstraint("from_uom_id", "to_uom_id", name="uq_uom_conversion_pair"),
        CheckConstraint("from_uom_id <> to_uom_id", name="ck_uom_conversion_distinct"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    to_uom_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("uom.id"), nullable=False
    )
    factor: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Currency(Base):
    """ISO 4217-style currency master. Code is the primary key (e.g. `TWD`)."""

    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    is_active: Mapped[bool] = mapped_column(SQLBoolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExchangeRate(Base):
    """Daily rate snapshots. Transactions will copy the rate at post time

    (master-plan §10.1: historical amounts must be reproducible without
    re-querying this table) — that usage is Phase 2+ (ledger/receivables);
    Phase 1 only establishes the schema.
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint(
            "from_currency_code", "to_currency_code", "rate_date", name="uq_exchange_rate_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    to_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Outbox (master-plan §10.4 — write-side only this week)
# ---------------------------------------------------------------------------


class OutboxEvent(Base):
    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
