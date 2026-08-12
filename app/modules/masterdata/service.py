"""masterdata service layer — business logic + transaction boundary.

Routers stay thin (parse request, call service, serialize response). All
writes to tenant-scoped tables stamp `company_id` from the ambient tenancy
context (`app.core.tenancy.require_current_company_id`) — it is never taken
from the DTO. All reads of tenant-scoped tables go through `select(...)`,
which `app.core.db`'s `do_orm_execute` hook filters to the active company
automatically; a row belonging to another company is therefore
indistinguishable from a row that doesn't exist, which is what turns
cross-company access attempts into 404s "for free" at the router layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import require_current_company_id
from app.modules.masterdata.models import (
    Account,
    Company,
    Currency,
    Customer,
    ExchangeRate,
    Product,
    Uom,
    UomConversion,
)
from app.modules.masterdata.schemas import (
    AccountCreate,
    AccountUpdate,
    CompanyCreate,
    CurrencyCreate,
    CustomerCreate,
    CustomerUpdate,
    ExchangeRateCreate,
    ProductCreate,
    ProductUpdate,
    UomConversionCreate,
    UomCreate,
)


async def _commit_or_conflict(session: AsyncSession, *, entity: str) -> None:
    """Commit, translating DB constraint violations into `ConflictError` (409).

    Fixes the Important finding from /CODEX REVIEW DIFF (2026-08-13): every
    `create_*` below writes to a table with a unique constraint and/or a
    foreign key (e.g. `company_id` sourced from the `X-Company-Id` header,
    which Phase 1 does not yet validate against a real `companies` row —
    see `app.core.tenancy`). Before this fix, a duplicate code/SKU or a
    well-formed-but-nonexistent company id surfaced as an unhandled
    `sqlalchemy.exc.IntegrityError` -> generic 500, even though
    `ConflictError` already existed and was wired to a 409 handler in
    `app.main`. This is the single choke point every create path funnels
    through instead of calling `session.commit()` directly.
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{entity} violates a uniqueness or reference constraint") from exc


# ---------------------------------------------------------------------------
# Company (tenant root — not tenant-scoped, no company_id stamping)
# ---------------------------------------------------------------------------


async def create_company(session: AsyncSession, data: CompanyCreate) -> Company:
    company = Company(**data.model_dump())
    session.add(company)
    await _commit_or_conflict(session, entity="Company")
    await session.refresh(company)
    return company


async def list_companies(session: AsyncSession) -> Sequence[Company]:
    result = await session.execute(select(Company).order_by(Company.code))
    return result.scalars().all()


async def get_company(session: AsyncSession, company_id: uuid.UUID) -> Company:
    result = await session.execute(select(Company).where(Company.id == company_id))
    company = result.scalars().first()
    if company is None:
        raise NotFoundError("Company", company_id)
    return company


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


async def create_customer(session: AsyncSession, data: CustomerCreate) -> Customer:
    customer = Customer(company_id=require_current_company_id(), **data.model_dump())
    session.add(customer)
    await _commit_or_conflict(session, entity="Customer")
    await session.refresh(customer)
    return customer


async def list_customers(session: AsyncSession) -> Sequence[Customer]:
    result = await session.execute(select(Customer).order_by(Customer.code))
    return result.scalars().all()


async def get_customer(session: AsyncSession, customer_id: uuid.UUID) -> Customer:
    result = await session.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalars().first()
    if customer is None:
        raise NotFoundError("Customer", customer_id)
    return customer


async def update_customer(
    session: AsyncSession, customer_id: uuid.UUID, data: CustomerUpdate
) -> Customer:
    customer = await get_customer(session, customer_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    await session.commit()
    await session.refresh(customer)
    return customer


async def delete_customer(session: AsyncSession, customer_id: uuid.UUID) -> None:
    customer = await get_customer(session, customer_id)
    await session.delete(customer)
    await session.commit()


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


async def create_product(session: AsyncSession, data: ProductCreate) -> Product:
    product = Product(company_id=require_current_company_id(), **data.model_dump())
    session.add(product)
    await _commit_or_conflict(session, entity="Product")
    await session.refresh(product, attribute_names=["uom"])
    return product


async def list_products(session: AsyncSession) -> Sequence[Product]:
    result = await session.execute(select(Product).order_by(Product.sku))
    return result.scalars().all()


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product:
    result = await session.execute(select(Product).where(Product.id == product_id))
    product = result.scalars().first()
    if product is None:
        raise NotFoundError("Product", product_id)
    return product


async def update_product(
    session: AsyncSession, product_id: uuid.UUID, data: ProductUpdate
) -> Product:
    product = await get_product(session, product_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    await session.commit()
    await session.refresh(product)
    return product


async def delete_product(session: AsyncSession, product_id: uuid.UUID) -> None:
    product = await get_product(session, product_id)
    await session.delete(product)
    await session.commit()


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def create_account(session: AsyncSession, data: AccountCreate) -> Account:
    account = Account(company_id=require_current_company_id(), **data.model_dump())
    session.add(account)
    await _commit_or_conflict(session, entity="Account")
    await session.refresh(account)
    return account


async def list_accounts(session: AsyncSession) -> Sequence[Account]:
    result = await session.execute(select(Account).order_by(Account.code))
    return result.scalars().all()


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account:
    result = await session.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if account is None:
        raise NotFoundError("Account", account_id)
    return account


async def update_account(
    session: AsyncSession, account_id: uuid.UUID, data: AccountUpdate
) -> Account:
    account = await get_account(session, account_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await session.commit()
    await session.refresh(account)
    return account


async def delete_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    account = await get_account(session, account_id)
    await session.delete(account)
    await session.commit()


# ---------------------------------------------------------------------------
# UoM / UoM conversion (global reference data — no tenant stamping/filtering)
# ---------------------------------------------------------------------------


async def create_uom(session: AsyncSession, data: UomCreate) -> Uom:
    uom = Uom(**data.model_dump())
    session.add(uom)
    await _commit_or_conflict(session, entity="Uom")
    await session.refresh(uom)
    return uom


async def list_uoms(session: AsyncSession) -> Sequence[Uom]:
    result = await session.execute(select(Uom).order_by(Uom.code))
    return result.scalars().all()


async def create_uom_conversion(session: AsyncSession, data: UomConversionCreate) -> UomConversion:
    conversion = UomConversion(**data.model_dump())
    session.add(conversion)
    await _commit_or_conflict(session, entity="UomConversion")
    await session.refresh(conversion)
    return conversion


async def list_uom_conversions(session: AsyncSession) -> Sequence[UomConversion]:
    result = await session.execute(select(UomConversion))
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Currency / exchange rate (global reference data)
# ---------------------------------------------------------------------------


async def create_currency(session: AsyncSession, data: CurrencyCreate) -> Currency:
    currency = Currency(**data.model_dump())
    session.add(currency)
    await _commit_or_conflict(session, entity="Currency")
    await session.refresh(currency)
    return currency


async def list_currencies(session: AsyncSession) -> Sequence[Currency]:
    result = await session.execute(select(Currency).order_by(Currency.code))
    return result.scalars().all()


async def create_exchange_rate(session: AsyncSession, data: ExchangeRateCreate) -> ExchangeRate:
    rate = ExchangeRate(**data.model_dump())
    session.add(rate)
    await _commit_or_conflict(session, entity="ExchangeRate")
    await session.refresh(rate)
    return rate


async def list_exchange_rates(session: AsyncSession) -> Sequence[ExchangeRate]:
    result = await session.execute(select(ExchangeRate).order_by(ExchangeRate.rate_date.desc()))
    return result.scalars().all()
