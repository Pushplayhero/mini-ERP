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

from pydantic import BaseModel, ConfigDict, Field

from app.modules.masterdata.models import AccountType

# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------


class CompanyCreate(BaseModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=255)
    functional_currency_code: str = Field(min_length=3, max_length=3)
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)


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
    list_price: Decimal = Decimal("0")
    is_active: bool = True
    custom_data: dict[str, Any] = Field(default_factory=dict)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    uom_id: uuid.UUID | None = None
    list_price: Decimal | None = None
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
