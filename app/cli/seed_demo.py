"""Seed a realistic, idempotent demo dataset (Week 7 hardening brief,

Decision 4). Populates one demo company with a full chart of accounts and
four O2C scenarios at varied stages — shipped-uninvoiced, invoiced-unpaid,
partially paid, fully paid — so the AR aging report, credit-limit exposure,
and trial balance all show non-trivial, realistic numbers out of the box.

**Bootstrap order is load-bearing** (Codex diff review 2026-08-15, v1 P1):
a freshly-migrated database has EMPTY `currencies`/`uom` tables — migration
0001 creates them but does not populate them; only the test-only
`tests/conftest.py::_seed_reference_data` fixture inserts `TWD`/`EA`. This
module must therefore:

1. Import `app.main` (below, as a side-effecting import) so the event bus's
   schema registrations and posting-handler subscriptions are installed —
   otherwise the first `events.publish(...)` a service call makes raises
   `UnknownEventTypeError`.
2. Create `TWD`/`EA` idempotently before anything that references them.
3. Get-or-create the demo company, then bind `company_context` explicitly
   for every subsequent write — this runs outside any HTTP request, so
   fail-closed tenancy (`app.core.tenancy`) would otherwise reject every
   write, same as `rebuild_ar_balances`/`rebuild_stock_summary`.
4. Create the chart of accounts AND a currently-open accounting period —
   every posting needs one (ADR-005 R4).
5. Only then: customer, product, stock, and the four O2C scenarios.

**Idempotency, per document type** (Codex diff review 2026-08-15, v1+v2
P2): the receivables/sales modules already carry their own idempotency
keys — this script's only job is to feed them stable values and recover by
lookup, never blind-create.

- Currencies/UoM/company/customer/product/accounts: get-or-create by their
  natural unique key (`_get_or_create` below); a code collision with
  *incompatible* attributes fails loudly rather than silently diverging.
- Orders: tagged via `SalesOrder.custom_data["seed_scenario"]` (a stable
  key per scenario) — on rerun, found by that tag and resumed through
  whatever legal states remain, never recreated.
- Invoices: recovered by ORDER — `list_invoices` is searched for a
  non-voided invoice against this order before ever calling
  `create_invoice`; blindly re-issuing would hit `uq_invoices_order_live`.
- Payments: a stable per-scenario `external_ref` recovered via
  `receivables.service.get_payment_by_external_ref` (the same lookup
  Week 6's diff-review fix added for the API's own 409 body) before ever
  calling `create_payment`.
- Allocations: a stable per-scenario `request_ref` with an identical
  payload, submitted through `allocate_payment` every run — the module's
  own allocation-command fingerprint (ADR-008 R14) already makes an exact
  retry replay idempotently; this script relies on that mechanism rather
  than inventing its own "already allocated?" check.
- Stock: **remaining-work-aware** reconciliation, not a fixed pre-scenario
  target (the over-correction trap Codex's v2 review caught) — pre-run
  on-hand is set to `desired_final_on_hand + sum(qty still awaiting
  shipment across not-yet-shipped scenario orders for that product)`,
  scenarios are then resumed (consuming exactly that remaining quantity),
  landing on exactly `desired_final_on_hand` regardless of whether this is
  the first run (everything still to ship) or a full rerun (nothing left
  to ship, so the reconciliation target is already the final one and
  nothing moves).

Entry point: `uv run python -m app.cli.seed_demo`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# Side-effecting import (see module docstring point 1): installs every
# registered event schema and posting-handler subscription before any
# service call below can publish one. Must happen before any other
# `app.modules.*`/`app.plugins` import that might indirectly trigger a
# publish — placing it first, ahead of even stdlib-adjacent imports below,
# keeps that ordering impossible to get wrong by accident.
import app.main  # noqa: F401  (side effect: composition-root wiring)
from app.core.db import dispose_engine, get_session_factory
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context, require_current_company_id
from app.modules.inventory import service as inventory_service
from app.modules.inventory.schemas import StockAdjustmentCreate
from app.modules.ledger import service as ledger_service
from app.modules.ledger.models import PeriodStatus
from app.modules.ledger.schemas import AccountingPeriodCreate
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import AccountType
from app.modules.masterdata.schemas import (
    AccountCreate,
    CompanyCreate,
    CurrencyCreate,
    CustomerCreate,
    ProductCreate,
    UomCreate,
)
from app.modules.receivables import service as receivables_service
from app.modules.receivables.models import Invoice, InvoiceStatus
from app.modules.receivables.schemas import InvoiceCreate, PaymentAllocationIn, PaymentCreate
from app.modules.sales import service as sales_service
from app.modules.sales.models import SalesOrder, SalesOrderStatus
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate

logger = logging.getLogger("app.cli.seed_demo")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Demo fixtures — stable codes, referenced nowhere outside this module.
# ---------------------------------------------------------------------------

COMPANY_CODE = "DEMOCO"
CUSTOMER_CODE = "DEMOCUST"
PRODUCT_SKU = "DEMO-SKU"
_STANDARD_CHART = (
    ("1000", "Cash", AccountType.ASSET),
    ("1100", "Accounts Receivable", AccountType.ASSET),
    ("1300", "Inventory", AccountType.ASSET),
    ("4000", "Revenue", AccountType.REVENUE),
    ("5000", "COGS", AccountType.EXPENSE),
)
_UNIT_PRICE = Decimal("100")
_STANDARD_COST = Decimal("30")
_DESIRED_FINAL_ON_HAND = Decimal("100")
# Generous enough that four scenario orders confirming/re-confirming across
# any number of reruns never trips the credit-limit plugin's hook.
_CUSTOMER_CREDIT_LIMIT = Decimal("100000")


@dataclass(frozen=True)
class Scenario:
    """One O2C scenario, identified by a stable `seed_scenario` tag.

    Every scenario ships (the earliest state the brief's example list
    names is "shipped-uninvoiced") — `invoice`/`pay_fraction` control how
    much further it goes. `pay_fraction` is the fraction of the invoice
    total received and allocated (`0` = no payment at all).
    """

    key: str
    qty: Decimal
    invoice: bool
    pay_fraction: Decimal


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(key="s1-fully-paid", qty=Decimal("2"), invoice=True, pay_fraction=Decimal("1")),
    Scenario(key="s2-partial-paid", qty=Decimal("3"), invoice=True, pay_fraction=Decimal("0.5")),
    Scenario(key="s3-invoiced-unpaid", qty=Decimal("1"), invoice=True, pay_fraction=Decimal("0")),
    Scenario(
        key="s4-shipped-uninvoiced", qty=Decimal("4"), invoice=False, pay_fraction=Decimal("0")
    ),
)


@dataclass
class SeedSummary:
    company_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))
    scenarios_resumed: int = 0
    invoices_issued: int = 0
    payments_created: int = 0
    allocations_submitted: int = 0


# ---------------------------------------------------------------------------
# Generic idempotent get-or-create over a masterdata-shaped service module
# (create_fn commits itself and raises ConflictError on a unique violation
# — see masterdata.service._commit_or_conflict / ledger.service's twin).
# ---------------------------------------------------------------------------


async def _get_or_create(
    session: AsyncSession,
    *,
    list_fn: Callable[[AsyncSession], Awaitable[Sequence[T]]],
    create_fn: Callable[[AsyncSession], Awaitable[T]],
    match: Callable[[T], bool],
    entity: str,
    compatible: Callable[[T], bool] | None = None,
) -> T:
    """`compatible`, if given, is checked whenever an existing row is found

    (by lookup OR by 409-recovery) and raises loudly on `False` — reusing a
    same-code row with *different* attributes than intended would silently
    post against something other than what this script means to (Codex
    diff review 2026-08-15, finding 3).
    """

    def _check(row: T) -> T:
        if compatible is not None and not compatible(row):
            raise RuntimeError(
                f"seed_demo: an existing {entity} (code-matched) has incompatible attributes "
                f"— {row!r} does not match what this script expects. Investigate before "
                "rerunning; this is not a race, it's a real data mismatch."
            )
        return row

    for existing in await list_fn(session):
        if match(existing):
            return _check(existing)
    try:
        return await create_fn(session)
    except ConflictError:
        # Lost a create-time race (or a prior run got this far and this one
        # didn't see it in the first list — same thing) — look up again
        # rather than treating the conflict as fatal.
        for existing in await list_fn(session):
            if match(existing):
                return _check(existing)
        raise RuntimeError(
            f"seed_demo: {entity} creation conflicted but no matching row was found on retry "
            "— a genuinely incompatible existing row, not a race. Investigate before rerunning."
        ) from None


# ---------------------------------------------------------------------------
# Bootstrap (Decision 4 §2, steps 1-4)
# ---------------------------------------------------------------------------


async def _ensure_reference_data(session: AsyncSession) -> None:
    await _get_or_create(
        session,
        list_fn=masterdata_service.list_currencies,
        create_fn=lambda s: masterdata_service.create_currency(
            s, CurrencyCreate(code="TWD", name="New Taiwan Dollar", decimal_places=0)
        ),
        match=lambda c: c.code == "TWD",
        compatible=lambda c: c.decimal_places == 0,
        entity="currency TWD",
    )
    await _get_or_create(
        session,
        list_fn=masterdata_service.list_uoms,
        create_fn=lambda s: masterdata_service.create_uom(s, UomCreate(code="EA", name="Each")),
        match=lambda u: u.code == "EA",
        entity="uom EA",
    )


async def _ensure_company(session: AsyncSession) -> uuid.UUID:
    company = await _get_or_create(
        session,
        list_fn=masterdata_service.list_companies,
        create_fn=lambda s: masterdata_service.create_company(
            s,
            CompanyCreate(
                code=COMPANY_CODE, name="Demo Company Ltd.", functional_currency_code="TWD"
            ),
        ),
        match=lambda c: c.code == COMPANY_CODE,
        compatible=lambda c: c.functional_currency_code == "TWD",
        entity="company",
    )
    return company.id


async def _ensure_account(
    session: AsyncSession, code: str, name: str, acct_type: AccountType
) -> None:
    # A dedicated helper (rather than an inline lambda inside the loop
    # below, with `code=code`-style default-arg capture to dodge the
    # classic late-binding-closure bug) — real function parameters give
    # mypy a concrete type to infer `_get_or_create`'s `T` from, which the
    # default-arg-lambda form otherwise leaves ambiguous.
    await _get_or_create(
        session,
        list_fn=masterdata_service.list_accounts,
        create_fn=lambda s: masterdata_service.create_account(
            s, AccountCreate(code=code, name=name, type=acct_type)
        ),
        match=lambda a: a.code == code,
        compatible=lambda a: a.type == acct_type,
        entity=f"account {code}",
    )


async def _ensure_chart_and_period(session: AsyncSession) -> None:
    for code, name, acct_type in _STANDARD_CHART:
        await _ensure_account(session, code, name, acct_type)

    today = date.today()
    await _get_or_create(
        session,
        list_fn=ledger_service.list_periods,
        create_fn=lambda s: ledger_service.create_period(
            s, AccountingPeriodCreate(year=today.year, month=today.month)
        ),
        match=lambda p: (p.year, p.month) == (today.year, today.month),
        compatible=lambda p: p.status == PeriodStatus.OPEN,
        entity=f"period {today.year}-{today.month:02d}",
    )


async def _ensure_customer_and_product(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    customer = await _get_or_create(
        session,
        list_fn=masterdata_service.list_customers,
        create_fn=lambda s: masterdata_service.create_customer(
            s,
            CustomerCreate(
                code=CUSTOMER_CODE,
                name="Demo Customer Co.",
                currency_code="TWD",
                credit_limit=_CUSTOMER_CREDIT_LIMIT,
            ),
        ),
        match=lambda c: c.code == CUSTOMER_CODE,
        compatible=lambda c: (
            c.currency_code == "TWD" and c.credit_limit == _CUSTOMER_CREDIT_LIMIT
        ),
        entity="customer",
    )

    uoms = await masterdata_service.list_uoms(session)
    ea_id = next(u.id for u in uoms if u.code == "EA")

    product = await _get_or_create(
        session,
        list_fn=masterdata_service.list_products,
        create_fn=lambda s: masterdata_service.create_product(
            s,
            ProductCreate(
                sku=PRODUCT_SKU,
                name="Demo Widget",
                uom_id=ea_id,
                list_price=_UNIT_PRICE,
                standard_cost=_STANDARD_COST,
            ),
        ),
        match=lambda p: p.sku == PRODUCT_SKU,
        compatible=lambda p: (
            p.uom_id == ea_id and p.list_price == _UNIT_PRICE and p.standard_cost == _STANDARD_COST
        ),
        entity="product",
    )
    return customer.id, product.id


# ---------------------------------------------------------------------------
# Stock: remaining-work-aware reconciliation (see module docstring)
# ---------------------------------------------------------------------------


async def _find_scenario_order(session: AsyncSession, key: str) -> SalesOrder | None:
    orders = await sales_service.list_orders(session)
    return next((o for o in orders if o.custom_data.get("seed_scenario") == key), None)


async def _reconcile_stock(session: AsyncSession, product_id: uuid.UUID) -> None:
    remaining_to_ship = Decimal("0")
    for scenario in SCENARIOS:
        order = await _find_scenario_order(session, scenario.key)
        if order is None or order.status in (
            SalesOrderStatus.DRAFT,
            SalesOrderStatus.CONFIRMED,
        ):
            remaining_to_ship += scenario.qty

    target = _DESIRED_FINAL_ON_HAND + remaining_to_ship
    company_id = require_current_company_id()
    current = await inventory_service.get_or_lock_summary(session, company_id, product_id)
    # `get_or_lock_summary` holds a row lock in this transaction; releasing
    # it before the adjustment call (which re-acquires the same lock
    # internally) needs a commit, not a mid-transaction release — simplest
    # correct fix: commit this lock-and-read first, then adjust separately.
    await session.commit()
    delta = target - current.on_hand
    if delta != 0:
        await inventory_service.create_adjustment(
            session,
            StockAdjustmentCreate(
                product_id=product_id,
                qty_delta=delta,
                reason=(
                    f"seed_demo: reconcile on-hand to desired final "
                    f"{_DESIRED_FINAL_ON_HAND} + {remaining_to_ship} still awaiting shipment"
                ),
            ),
        )


# ---------------------------------------------------------------------------
# Scenario resume — walk one order forward through whatever legal states
# remain, idempotently at every step (see module docstring).
# ---------------------------------------------------------------------------


async def _resume_scenario(
    session: AsyncSession,
    scenario: Scenario,
    customer_id: uuid.UUID,
    product_id: uuid.UUID,
    summary: SeedSummary,
) -> None:
    order = await _find_scenario_order(session, scenario.key)
    if order is None:
        order = await sales_service.create_order(
            session,
            SalesOrderCreate(
                customer_id=customer_id,
                lines=[
                    SalesOrderLineCreate(
                        product_id=product_id, qty=scenario.qty, unit_price=_UNIT_PRICE
                    )
                ],
                custom_data={"seed_scenario": scenario.key},
            ),
        )

    if order.status == SalesOrderStatus.DRAFT:
        try:
            await sales_service.confirm_order(session, order.id)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise RuntimeError(f"seed_demo: confirm failed for {scenario.key}") from exc
        order = await sales_service.get_order(session, order.id)

    if order.status == SalesOrderStatus.CONFIRMED:
        try:
            await sales_service.ship_order(session, order.id)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise RuntimeError(f"seed_demo: ship failed for {scenario.key}") from exc
        order = await sales_service.get_order(session, order.id)

    if not scenario.invoice:
        summary.scenarios_resumed += 1
        return

    invoice = await _find_or_issue_invoice(session, order.id, summary)

    if scenario.pay_fraction > 0:
        await _find_or_create_payment_and_allocate(session, scenario, customer_id, invoice, summary)

    summary.scenarios_resumed += 1


async def _find_or_issue_invoice(
    session: AsyncSession, order_id: uuid.UUID, summary: SeedSummary
) -> Invoice:
    """Recover by ORDER (Codex v2 P2) — never blindly re-issue, which would

    hit `uq_invoices_order_live`.
    """
    existing_invoices = await receivables_service.list_invoices(session)
    existing = next(
        (
            i
            for i in existing_invoices
            if i.order_id == order_id and i.status != InvoiceStatus.VOIDED
        ),
        None,
    )
    if existing is not None:
        return existing

    try:
        invoice = await receivables_service.create_invoice(
            session, InvoiceCreate(order_id=order_id)
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing_invoices = await receivables_service.list_invoices(session)
        recovered = next(
            (
                i
                for i in existing_invoices
                if i.order_id == order_id and i.status != InvoiceStatus.VOIDED
            ),
            None,
        )
        if recovered is None:
            raise RuntimeError(f"seed_demo: invoice issue conflicted for order {order_id}") from exc
        return recovered

    summary.invoices_issued += 1
    return await receivables_service.get_invoice(session, invoice.id)


async def _find_or_create_payment_and_allocate(
    session: AsyncSession,
    scenario: Scenario,
    customer_id: uuid.UUID,
    invoice: Invoice,
    summary: SeedSummary,
) -> None:
    external_ref = f"seed-{scenario.key}-pay1"
    amount = (invoice.total * scenario.pay_fraction).quantize(Decimal("0.000001"))

    payment = await receivables_service.get_payment_by_external_ref(session, external_ref)
    if payment is None:
        try:
            payment = await receivables_service.create_payment(
                session,
                PaymentCreate(customer_id=customer_id, external_ref=external_ref, amount=amount),
            )
            await session.commit()
            summary.payments_created += 1
        except IntegrityError as exc:
            await session.rollback()
            payment = await receivables_service.get_payment_by_external_ref(session, external_ref)
            if payment is None:
                raise RuntimeError(
                    f"seed_demo: payment creation conflicted for {external_ref}"
                ) from exc

    # Stable request_ref + identical payload every run — ADR-008 R14's
    # allocation-command fingerprint makes a repeat replay idempotently
    # (module docstring point on Allocations).
    request_ref = f"seed-{scenario.key}-alloc1"
    try:
        await receivables_service.allocate_payment(
            session,
            payment.id,
            request_ref,
            [PaymentAllocationIn(invoice_id=invoice.id, amount=amount)],
        )
        await session.commit()
        summary.allocations_submitted += 1
    except IntegrityError as exc:
        await session.rollback()
        raise RuntimeError(f"seed_demo: allocation failed for {request_ref}") from exc


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def seed_demo() -> SeedSummary:
    session_factory = get_session_factory()
    summary = SeedSummary()

    async with session_factory() as session:
        await _ensure_reference_data(session)
        company_id = await _ensure_company(session)
        summary.company_id = company_id

        with company_context(company_id):
            await _ensure_chart_and_period(session)
            customer_id, product_id = await _ensure_customer_and_product(session)
            await _reconcile_stock(session, product_id)
            for scenario in SCENARIOS:
                await _resume_scenario(session, scenario, customer_id, product_id, summary)

            # Decision 4's v2->v3 consensus record requires this explicitly
            # (Codex diff review 2026-08-15, finding 4): after resuming
            # every scenario, on-hand must land on exactly
            # `_DESIRED_FINAL_ON_HAND` — a self-check, not just a hope the
            # remaining-work-aware reconciliation above got the math right.
            final_summary = await inventory_service.get_or_lock_summary(
                session, company_id, product_id
            )
            await session.commit()
            if final_summary.on_hand != _DESIRED_FINAL_ON_HAND:
                raise RuntimeError(
                    f"seed_demo: expected on_hand == {_DESIRED_FINAL_ON_HAND} after resuming "
                    f"every scenario, got {final_summary.on_hand} — the remaining-work-aware "
                    "stock reconciliation invariant was violated; investigate before trusting "
                    "this run's data."
                )

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    result = await seed_demo()
    print(
        f"seed_demo summary: company_id={result.company_id} "
        f"scenarios_resumed={result.scenarios_resumed} "
        f"invoices_issued={result.invoices_issued} "
        f"payments_created={result.payments_created} "
        f"allocations_submitted={result.allocations_submitted}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
