"""masterdata FastAPI routes — thin: parse request, call service, serialize.

`company_id` is never accepted as a path/body parameter for tenant-scoped
resources: it is always resolved server-side from the tenancy context set by
`TenancyMiddleware` (see `app.main`). Cross-company GET/PATCH/DELETE by id
resolve to 404 because the row is invisible to the requester's company
context (enforced by `app.core.db`'s ORM filter) — see
`tests/masterdata/test_cross_company_isolation.py`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_session
from app.modules.masterdata import service
from app.modules.masterdata.schemas import (
    AccountCreate,
    AccountRead,
    AccountUpdate,
    CompanyCreate,
    CompanyRead,
    CurrencyCreate,
    CurrencyRead,
    CustomerCreate,
    CustomerRead,
    CustomerUpdate,
    ExchangeRateCreate,
    ExchangeRateRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    UomConversionCreate,
    UomConversionRead,
    UomCreate,
    UomRead,
)

router = APIRouter(tags=["masterdata"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# ---------------------------------------------------------------------------
# Companies (tenant root — bootstrap endpoints, not tenant-filtered)
# ---------------------------------------------------------------------------


@router.post("/companies", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
async def create_company(payload: CompanyCreate, session: DbSession) -> CompanyRead:
    company = await service.create_company(session, payload)
    return CompanyRead.model_validate(company)


@router.get("/companies", response_model=list[CompanyRead])
async def list_companies(session: DbSession) -> list[CompanyRead]:
    companies = await service.list_companies(session)
    return [CompanyRead.model_validate(c) for c in companies]


@router.get("/companies/{company_id}", response_model=CompanyRead)
async def get_company(company_id: uuid.UUID, session: DbSession) -> CompanyRead:
    company = await service.get_company(session, company_id)
    return CompanyRead.model_validate(company)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@router.post("/customers", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
async def create_customer(payload: CustomerCreate, session: DbSession) -> CustomerRead:
    customer = await service.create_customer(session, payload)
    return CustomerRead.model_validate(customer)


@router.get("/customers", response_model=list[CustomerRead])
async def list_customers(session: DbSession) -> list[CustomerRead]:
    customers = await service.list_customers(session)
    return [CustomerRead.model_validate(c) for c in customers]


@router.get("/customers/{customer_id}", response_model=CustomerRead)
async def get_customer(customer_id: uuid.UUID, session: DbSession) -> CustomerRead:
    customer = await service.get_customer(session, customer_id)
    return CustomerRead.model_validate(customer)


@router.patch("/customers/{customer_id}", response_model=CustomerRead)
async def update_customer(
    customer_id: uuid.UUID, payload: CustomerUpdate, session: DbSession
) -> CustomerRead:
    customer = await service.update_customer(session, customer_id, payload)
    return CustomerRead.model_validate(customer)


@router.delete(
    "/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_customer(customer_id: uuid.UUID, session: DbSession) -> None:
    await service.delete_customer(session, customer_id)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, session: DbSession) -> ProductRead:
    product = await service.create_product(session, payload)
    return ProductRead.model_validate(product)


@router.get("/products", response_model=list[ProductRead])
async def list_products(session: DbSession) -> list[ProductRead]:
    products = await service.list_products(session)
    return [ProductRead.model_validate(p) for p in products]


@router.get("/products/{product_id}", response_model=ProductRead)
async def get_product(product_id: uuid.UUID, session: DbSession) -> ProductRead:
    product = await service.get_product(session, product_id)
    return ProductRead.model_validate(product)


@router.patch("/products/{product_id}", response_model=ProductRead)
async def update_product(
    product_id: uuid.UUID, payload: ProductUpdate, session: DbSession
) -> ProductRead:
    product = await service.update_product(session, product_id, payload)
    return ProductRead.model_validate(product)


@router.delete(
    "/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_product(product_id: uuid.UUID, session: DbSession) -> None:
    await service.delete_product(session, product_id)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@router.post("/accounts", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(payload: AccountCreate, session: DbSession) -> AccountRead:
    account = await service.create_account(session, payload)
    return AccountRead.model_validate(account)


@router.get("/accounts", response_model=list[AccountRead])
async def list_accounts(session: DbSession) -> list[AccountRead]:
    accounts = await service.list_accounts(session)
    return [AccountRead.model_validate(a) for a in accounts]


@router.get("/accounts/{account_id}", response_model=AccountRead)
async def get_account(account_id: uuid.UUID, session: DbSession) -> AccountRead:
    account = await service.get_account(session, account_id)
    return AccountRead.model_validate(account)


@router.patch("/accounts/{account_id}", response_model=AccountRead)
async def update_account(
    account_id: uuid.UUID, payload: AccountUpdate, session: DbSession
) -> AccountRead:
    account = await service.update_account(session, account_id, payload)
    return AccountRead.model_validate(account)


@router.delete(
    "/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_account(account_id: uuid.UUID, session: DbSession) -> None:
    await service.delete_account(session, account_id)


# ---------------------------------------------------------------------------
# UoM / UoM conversion (global reference data)
# ---------------------------------------------------------------------------


@router.post("/uom", response_model=UomRead, status_code=status.HTTP_201_CREATED)
async def create_uom(payload: UomCreate, session: DbSession) -> UomRead:
    uom = await service.create_uom(session, payload)
    return UomRead.model_validate(uom)


@router.get("/uom", response_model=list[UomRead])
async def list_uoms(session: DbSession) -> list[UomRead]:
    uoms = await service.list_uoms(session)
    return [UomRead.model_validate(u) for u in uoms]


@router.post(
    "/uom-conversions", response_model=UomConversionRead, status_code=status.HTTP_201_CREATED
)
async def create_uom_conversion(
    payload: UomConversionCreate, session: DbSession
) -> UomConversionRead:
    conversion = await service.create_uom_conversion(session, payload)
    return UomConversionRead.model_validate(conversion)


@router.get("/uom-conversions", response_model=list[UomConversionRead])
async def list_uom_conversions(session: DbSession) -> list[UomConversionRead]:
    conversions = await service.list_uom_conversions(session)
    return [UomConversionRead.model_validate(c) for c in conversions]


# ---------------------------------------------------------------------------
# Currencies / exchange rates (global reference data)
# ---------------------------------------------------------------------------


@router.post("/currencies", response_model=CurrencyRead, status_code=status.HTTP_201_CREATED)
async def create_currency(payload: CurrencyCreate, session: DbSession) -> CurrencyRead:
    currency = await service.create_currency(session, payload)
    return CurrencyRead.model_validate(currency)


@router.get("/currencies", response_model=list[CurrencyRead])
async def list_currencies(session: DbSession) -> list[CurrencyRead]:
    currencies = await service.list_currencies(session)
    return [CurrencyRead.model_validate(c) for c in currencies]


@router.post(
    "/exchange-rates", response_model=ExchangeRateRead, status_code=status.HTTP_201_CREATED
)
async def create_exchange_rate(payload: ExchangeRateCreate, session: DbSession) -> ExchangeRateRead:
    rate = await service.create_exchange_rate(session, payload)
    return ExchangeRateRead.model_validate(rate)


@router.get("/exchange-rates", response_model=list[ExchangeRateRead])
async def list_exchange_rates(session: DbSession) -> list[ExchangeRateRead]:
    rates = await service.list_exchange_rates(session)
    return [ExchangeRateRead.model_validate(r) for r in rates]
