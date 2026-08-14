"""masterdata Pydantic v2 DTOs.

Convention enforced throughout: `*Create` / `*Update` schemas never accept
`company_id` — it is always injected server-side from the tenancy context
(see `app.core.tenancy` + `router.py`), so a client can never write into
another company by putting a different id in the request body.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.masterdata.models import AccountType

# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------

# Diff-review fix (ADR-005 R1 / master-plan §10.1): `ledger.service` hardcodes
# `FUNCTIONAL_CURRENCY = "TWD"` rather than reading each company's own
# `functional_currency_code` — a deliberate Phase 1 simplification, per
# ADR-005 R1's own words: "Phase 1 is TWD-only in practice". That statement
# was previously true only by convention (nothing stopped a client from
# creating a company with any registered currency), so a company created
# with, say, `functional_currency_code="USD"` would silently get every
# journal line rejected (ledger only accepts TWD) or, worse, have genuinely
# non-functional-currency amounts treated as if they were functional —
# undermining the "balance enforced in functional currency" invariant for
# that company's entire ledger. Enforcing the assumption here, at company
# creation, makes masterdata and ledger agree on what Phase 1 actually
# supports instead of ledger silently assuming it.
_PHASE_1_FUNCTIONAL_CURRENCY = "TWD"


class CompanyCreate(BaseModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=255)
    functional_currency_code: str = Field(min_length=3, max_length=3)
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("functional_currency_code")
    @classmethod
    def _functional_currency_is_phase_1_supported(cls, value: str) -> str:
        if value != _PHASE_1_FUNCTIONAL_CURRENCY:
            raise ValueError(
                f"functional_currency_code must be {_PHASE_1_FUNCTIONAL_CURRENCY!r} in Phase 1 "
                f"(got {value!r}) — real multi-currency posting is out of scope until Phase 3 "
                "(see ADR-005 R1); the ledger module hardcodes this same assumption and would "
                "silently mishandle a company with a different functional currency"
            )
        return value


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    custom_data: dict[str, Any] | None = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    functional_currency_code: str
    is_active: bool
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerCreate(BaseModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=255)
    credit_limit: Decimal = Decimal("0")
    currency_code: str = Field(min_length=3, max_length=3)
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    credit_limit: Decimal | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    is_active: bool | None = None
    custom_data: dict[str, Any] | None = None


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    code: str
    name: str
    credit_limit: Decimal
    currency_code: str
    is_active: bool
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class ProductCreate(BaseModel):
    sku: str = Field(max_length=64)
    name: str = Field(max_length=255)
    uom_id: uuid.UUID
    # `ge=0` (diff-review fix): a negative list_price is not a real price,
    # and — since Week 4's diff-review fix — `sales.service.confirm_order`
    # writes `product.list_price` straight into a non-override line's
    # `unit_price`/`amount` at confirm time. Without this, a negative price
    # would violate `ck_sales_order_lines_unit_price_nonneg`/
    # `ck_sales_order_lines_amount_nonneg` at `session.flush()` inside
    # `confirm_order`'s uncommitted transaction, and that flush is not
    # wrapped by the router's `IntegrityError` -> 409 translation (see
    # `sales/router.py`'s diff-review fix), so it would otherwise surface
    # as an unhandled 500 instead of a clean 422 rejected up front here.
    list_price: Decimal = Field(default=Decimal("0"), ge=0)
    # ADR-007 Decision 3 / Consensus P3: COGS cost basis, snapshotted into
    # `sales.goods_shipped` at ship time. Additive, defaults to 0.
    # `ge=0` (diff-review fix): a negative cost isn't a real standard cost —
    # left unvalidated, a positive-cost line and a negative-cost line in the
    # same shipment could net `total_cost` to zero and trip the zero-cost
    # skip (`ledger.posting`) even though valued goods genuinely moved.
    # Mirrored by `ck_products_standard_cost_nonneg` at the DB layer
    # (migration 0005) as defense in depth, same belt-and-suspenders pattern
    # `stock_summary.on_hand >= 0` uses.
    standard_cost: Decimal = Field(default=Decimal("0"), ge=0)
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    uom_id: uuid.UUID | None = None
    list_price: Decimal | None = Field(default=None, ge=0)
    standard_cost: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None
    custom_data: dict[str, Any] | None = None


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    sku: str
    name: str
    uom_id: uuid.UUID
    list_price: Decimal
    standard_cost: Decimal
    is_active: bool
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountCreate(BaseModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=255)
    type: AccountType
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    type: AccountType | None = None
    is_active: bool | None = None
    custom_data: dict[str, Any] | None = None


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    code: str
    name: str
    type: AccountType
    is_active: bool
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# UoM / UoM conversion (global reference data)
# ---------------------------------------------------------------------------


class UomCreate(BaseModel):
    code: str = Field(max_length=16)
    name: str = Field(max_length=64)
    is_active: bool = True


class UomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    is_active: bool


class UomConversionCreate(BaseModel):
    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    factor: Decimal


class UomConversionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    factor: Decimal


# ---------------------------------------------------------------------------
# Currency / exchange rate (global reference data)
# ---------------------------------------------------------------------------


class CurrencyCreate(BaseModel):
    code: str = Field(min_length=3, max_length=3)
    name: str = Field(max_length=64)
    decimal_places: int = 2
    is_active: bool = True


class CurrencyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    decimal_places: int
    is_active: bool


class ExchangeRateCreate(BaseModel):
    from_currency_code: str = Field(min_length=3, max_length=3)
    to_currency_code: str = Field(min_length=3, max_length=3)
    rate: Decimal
    rate_date: date


class ExchangeRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_currency_code: str
    to_currency_code: str
    rate: Decimal
    rate_date: date
