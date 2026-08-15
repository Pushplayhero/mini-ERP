"""tests/e2e/test_property_o2c_balances.py — Hypothesis property test over

domain-event sequences (Week 7 hardening brief, Decision 2). A **binding
DoD line**, not droppable — see `docs/adr/WEEK7-phase1-hardening-brief.md`
Decision 2 / O-1.

Proves, for a randomly generated but always-legal O2C operation plan
(order create → confirm → ship → invoice → pay → allocate, interleaved in
any legal order across multiple orders/payments):
- Trial balance balances (Σdebit == Σcredit).
- Σ(open invoice balances) - Σ(unapplied credits) == ledger 1100 balance
  (the ADR-008 Decision 5 control-account tie-out).
- `on_hand >= 0` for the product (Codex v1 P3 / master plan §6 — a
  near-free assertion on the same trace, not narrowed away).

**Harness design (Decision 2's mandatory shape — copying
`tests/ledger/test_property_trial_balance.py`'s pattern would be UNSAFE)**:
that test sets up ONE company once and accumulates DB state across every
Hypothesis example (a running `cumulative_expected` total) — fine for a
single flat invariant, but wrong here: a multi-module domain state machine
with shared state would make failures order-dependent across examples and
Hypothesis shrinking unreliable (a shrunk example would replay against
dirty state left by a *different*, earlier example). This harness instead:
- Draws a **pure operation plan first** (`_draw_plan`, no I/O at all) using
  `st.data()`'s interactive draw, where each action is only offered when
  its predecessor state makes it legal — so the plan is always fully
  executable, with no "illegal transition" noise to filter out later.
- Executes each Hypothesis example against a **fresh company, fresh chart,
  fresh period, fresh customer, fresh product, fresh stock** — no example
  ever sees another's state (`_run_example`, called once per example).
- Creates and disposes its own `AsyncEngine` **inside that example's own
  `asyncio.run()` call** — same loop-isolation reasoning as the ledger
  property test's module docstring (an engine created in one
  `asyncio.run()` cannot be reused from a different one).
- Imports `app.main` first (composition-root wiring: event schemas +
  posting-handler subscriptions) before any service call that publishes an
  event, same requirement `seed_demo.py` documents — otherwise the first
  `publish()` raises `UnknownEventTypeError`.
- Guarantees at least one non-zero posting event unconditionally: every
  drawn plan starts with a fixed, always-legal `NewOrder → Confirm → Ship`
  prefix (a real COGS/Inventory posting), rather than leaving this to
  chance the way a purely random draw might occasionally skip it.

**One deliberate deviation from the brief's illustrative op list**: the
brief sketches `Pay(i, amount)` as if indexed by order `i`, but a
`Payment` in this domain is never tied to an order (it's tied to a
customer, and later allocated to whichever invoice(s) the caller
chooses via a *separate* `Allocate` op) — `receivables.service.create_payment`
takes no order reference at all. `Pay` here is simply `Pay(amount)`
against the plan's single shared customer; `Allocate(payment_idx,
invoice_idx, amount)` is where an order-derived invoice and a payment
actually connect. This matches how the real API works, not a narrowing of
the brief's intent.

**Stock provisioning** (Codex diff review, Week 7 slice 5, finding 2): seeded
to *exactly* the sum of qty for orders that actually get a `Ship` op in the
drawn plan — NOT every `NewOrder`'s qty. An earlier version of this test
seeded every created order's qty regardless of whether it ever shipped,
which over-provisions whenever a plan creates an order it never
confirms/ships, silently absorbing a double-decrement bug in the
ship-time stock move instead of `on_hand >= 0` catching it. Since an
order ships at most once, summing by each `Ship` op's `order_idx` never
double-counts and is exactly tight.

**Credit limit**: the plan's customer is created with `credit_limit=0`,
the module's documented "do not check" sentinel (`app/plugins/credit_limit.py`)
— this keeps every `Confirm` in a drawn plan unconditionally legal
regardless of drawn quantities, which is what "always executable, no
illegal-transition noise" requires; the credit-limit hook itself already
has its own dedicated tests (`tests/sales/test_credit_limit_plugin.py`)
and proving it again here would just be plan-generation noise.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Side-effecting import (see module docstring): installs every registered
# event schema and posting-handler subscription before any service call
# below can publish one. Same requirement/placement as `app/cli/seed_demo.py`.
import app.main  # noqa: F401  (side effect: composition-root wiring)
from app.core.tenancy import company_context
from app.modules.inventory import service as inventory_service
from app.modules.inventory.schemas import StockAdjustmentCreate
from app.modules.ledger import service as ledger_service
from app.modules.ledger.schemas import AccountingPeriodCreate
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import AccountType
from app.modules.masterdata.schemas import (
    AccountCreate,
    CompanyCreate,
    CustomerCreate,
    ProductCreate,
)
from app.modules.receivables import service as receivables_service
from app.modules.receivables.schemas import InvoiceCreate, PaymentAllocationIn, PaymentCreate
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate

_ZERO = Decimal("0")
_UNIT_PRICE = Decimal("100")
_STANDARD_COST = Decimal("30")
_STANDARD_CHART = (
    ("1000", "Cash", AccountType.ASSET),
    ("1100", "Accounts Receivable", AccountType.ASSET),
    ("1300", "Inventory", AccountType.ASSET),
    ("4000", "Revenue", AccountType.REVENUE),
    ("5000", "COGS", AccountType.EXPENSE),
)

# Plan-shape bounds — small enough to keep each Hypothesis example's DB
# round-trip count (and therefore wall-clock time) bounded, large enough to
# exercise real interleaving across multiple orders/payments.
_MAX_ORDERS = 4
_MAX_PAYMENTS = 3
_MAX_EXTRA_STEPS = 9
_MAX_QTY = 5
_MAX_PAY_AMOUNT = 300

# ---------------------------------------------------------------------------
# Plan representation — pure data, no I/O. Executed later by `_execute_plan`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewOrder:
    qty: int


@dataclass(frozen=True)
class Confirm:
    order_idx: int


@dataclass(frozen=True)
class Ship:
    order_idx: int


@dataclass(frozen=True)
class IssueInvoice:
    order_idx: int


@dataclass(frozen=True)
class Pay:
    amount: int


@dataclass(frozen=True)
class Allocate:
    payment_idx: int
    invoice_idx: int
    amount: int


Op = NewOrder | Confirm | Ship | IssueInvoice | Pay | Allocate


@dataclass
class _OrderState:
    status: str  # "draft" | "confirmed" | "shipped"
    qty: int
    invoice_idx: int | None = None


@dataclass
class _InvoiceState:
    open_balance: int


@dataclass
class _PaymentState:
    unapplied: int


def _draw_plan(data: st.DataObject) -> list[Op]:
    """Interactively draw a plan (Decision 2's `st.data()` requirement):

    at every step, only actions legal from the CURRENT simulated state are
    offered, so the returned plan is always fully executable end to end —
    no branch here can ever produce an "illegal transition" the caller has
    to filter out.
    """
    orders: list[_OrderState] = []
    invoices: list[_InvoiceState] = []
    payments: list[_PaymentState] = []
    plan: list[Op] = []

    # Guaranteed always-legal prefix: at least one Ship (a real COGS/
    # Inventory posting) in every example, unconditionally (module
    # docstring: "guarantees at least one non-zero posting event").
    seed_qty = data.draw(st.integers(min_value=1, max_value=_MAX_QTY), label="seed_qty")
    plan.append(NewOrder(seed_qty))
    orders.append(_OrderState(status="draft", qty=seed_qty))
    plan.append(Confirm(0))
    orders[0].status = "confirmed"
    plan.append(Ship(0))
    orders[0].status = "shipped"

    extra_steps = data.draw(st.integers(min_value=0, max_value=_MAX_EXTRA_STEPS), label="steps")
    for _ in range(extra_steps):
        draft_idxs = [i for i, o in enumerate(orders) if o.status == "draft"]
        confirmed_idxs = [i for i, o in enumerate(orders) if o.status == "confirmed"]
        shippable_uninvoiced_idxs = [
            i for i, o in enumerate(orders) if o.status == "shipped" and o.invoice_idx is None
        ]
        payable_payment_idxs = [i for i, p in enumerate(payments) if p.unapplied > 0]
        open_invoice_idxs = [i for i, inv in enumerate(invoices) if inv.open_balance > 0]

        actions: list[str] = []
        if len(orders) < _MAX_ORDERS:
            actions.append("new_order")
        if draft_idxs:
            actions.append("confirm")
        if confirmed_idxs:
            actions.append("ship")
        if shippable_uninvoiced_idxs:
            actions.append("invoice")
        if len(payments) < _MAX_PAYMENTS:
            actions.append("pay")
        if payable_payment_idxs and open_invoice_idxs:
            actions.append("allocate")

        if not actions:
            break  # every dimension exhausted (all maxed out and settled)

        action = data.draw(st.sampled_from(actions), label="action")

        if action == "new_order":
            qty = data.draw(st.integers(min_value=1, max_value=_MAX_QTY), label="qty")
            plan.append(NewOrder(qty))
            orders.append(_OrderState(status="draft", qty=qty))
        elif action == "confirm":
            idx = data.draw(st.sampled_from(draft_idxs), label="confirm_idx")
            plan.append(Confirm(idx))
            orders[idx].status = "confirmed"
        elif action == "ship":
            idx = data.draw(st.sampled_from(confirmed_idxs), label="ship_idx")
            plan.append(Ship(idx))
            orders[idx].status = "shipped"
        elif action == "invoice":
            idx = data.draw(st.sampled_from(shippable_uninvoiced_idxs), label="invoice_idx")
            plan.append(IssueInvoice(idx))
            total = orders[idx].qty * int(_UNIT_PRICE)
            invoices.append(_InvoiceState(open_balance=total))
            orders[idx].invoice_idx = len(invoices) - 1
        elif action == "pay":
            amount = data.draw(
                st.integers(min_value=1, max_value=_MAX_PAY_AMOUNT), label="pay_amount"
            )
            plan.append(Pay(amount))
            payments.append(_PaymentState(unapplied=amount))
        elif action == "allocate":
            payment_idx = data.draw(st.sampled_from(payable_payment_idxs), label="alloc_payment")
            invoice_idx = data.draw(st.sampled_from(open_invoice_idxs), label="alloc_invoice")
            max_amount = min(payments[payment_idx].unapplied, invoices[invoice_idx].open_balance)
            amount = data.draw(st.integers(min_value=1, max_value=max_amount), label="alloc_amount")
            plan.append(Allocate(payment_idx, invoice_idx, amount))
            payments[payment_idx].unapplied -= amount
            invoices[invoice_idx].open_balance -= amount

    return plan


# ---------------------------------------------------------------------------
# Plan execution — real service-layer calls against a real company.
# ---------------------------------------------------------------------------


async def _execute_plan(
    session: AsyncSession, plan: list[Op], customer_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    order_ids: list[uuid.UUID] = []
    invoice_ids: list[uuid.UUID] = []
    payment_ids: list[uuid.UUID] = []
    # order_idx -> the order's actual `shipped_at` date (UTC — see Ship
    # branch below), used to pin `invoice_date` explicitly (Codex diff
    # review round 2, finding B).
    shipped_at_date_by_order: dict[int, date] = {}

    for step, op in enumerate(plan):
        if isinstance(op, NewOrder):
            # `create_order` self-commits (sales.service's "simple create"
            # convention) — no explicit commit needed here.
            order = await sales_service.create_order(
                session,
                SalesOrderCreate(
                    customer_id=customer_id,
                    lines=[SalesOrderLineCreate(product_id=product_id, qty=Decimal(op.qty))],
                ),
            )
            order_ids.append(order.id)
        elif isinstance(op, Confirm):
            # Flush-only core — this test owns the commit boundary, same as
            # `receivables.router`/`sales.router` do for the real API.
            await sales_service.confirm_order(session, order_ids[op.order_idx])
            await session.commit()
        elif isinstance(op, Ship):
            # `ship_order` has no date-override parameter — it always
            # stamps real `datetime.now(timezone.utc)` internally
            # (`sales.service.ship_order`) — so record what it actually
            # used rather than assuming any particular timezone basis.
            shipped_order = await sales_service.ship_order(session, order_ids[op.order_idx])
            await session.commit()
            # `SalesOrder.shipped_at` is nullable at the type level (unset
            # on a draft/confirmed order) but `ship_order` always sets it
            # on the order it just shipped — assert rather than silently
            # `# type: ignore`, so a real regression here still fails loudly.
            assert shipped_order.shipped_at is not None
            shipped_at_date_by_order[op.order_idx] = shipped_order.shipped_at.date()
        elif isinstance(op, IssueInvoice):
            # Codex diff review round 2, finding B: `create_invoice`
            # rejects `invoice_date < order.shipped_at.date()`
            # (ADR-008 R13) — `order.shipped_at` is UTC while
            # `InvoiceCreate.invoice_date`'s default is local
            # `date.today()`, so on any host WEST of UTC (negative
            # offset), local "today" can trail the UTC ship date and this
            # would spuriously reject a perfectly legal plan. Pin
            # `invoice_date` explicitly to the order's own recorded
            # `shipped_at` date so the check is satisfied by construction,
            # independent of the host's timezone.
            invoice = await receivables_service.create_invoice(
                session,
                InvoiceCreate(
                    order_id=order_ids[op.order_idx],
                    invoice_date=shipped_at_date_by_order[op.order_idx],
                ),
            )
            await session.commit()
            invoice_ids.append(invoice.id)
        elif isinstance(op, Pay):
            payment = await receivables_service.create_payment(
                session,
                PaymentCreate(
                    customer_id=customer_id,
                    external_ref=f"hyp-pay-{step}",
                    amount=Decimal(op.amount),
                ),
            )
            await session.commit()
            payment_ids.append(payment.id)
        elif isinstance(op, Allocate):
            await receivables_service.allocate_payment(
                session,
                payment_ids[op.payment_idx],
                f"hyp-alloc-{step}",
                [
                    PaymentAllocationIn(
                        invoice_id=invoice_ids[op.invoice_idx], amount=Decimal(op.amount)
                    )
                ],
            )
            await session.commit()


async def _assert_invariants(session: AsyncSession, product_id: uuid.UUID) -> None:
    trial_balance = await ledger_service.get_trial_balance(session)
    total_debit = sum((line.total_debit for line in trial_balance), _ZERO)
    total_credit = sum((line.total_credit for line in trial_balance), _ZERO)
    assert (
        total_debit == total_credit
    ), f"trial balance does not balance: debit={total_debit} credit={total_credit}"

    line_1100 = next((line for line in trial_balance if line.account_code == "1100"), None)
    ledger_1100_balance = (
        (line_1100.total_debit - line_1100.total_credit) if line_1100 is not None else _ZERO
    )
    aging_rows = await receivables_service.get_ar_aging(session)
    aging_net = sum((row.net_total for row in aging_rows), _ZERO)
    assert aging_net == ledger_1100_balance, (
        f"control-account tie-out violated: aging net {aging_net} != ledger 1100 balance "
        f"{ledger_1100_balance}"
    )

    # Codex diff review (Week 7 slice 5, finding 3): setup unconditionally
    # calls `create_adjustment` with a nonzero `qty_delta` (the guaranteed
    # Ship prefix means `seed_stock` is always >= 1), so a `StockSummary`
    # row for this product MUST exist by now — silently treating "no row"
    # as "on_hand == 0" would let a genuine bug (the adjustment failing to
    # create/upsert the row, or this query filtering wrongly) pass as a
    # vacuous zero instead of failing loudly.
    stock_rows = await inventory_service.list_stock_summary(session, product_id=product_id)
    assert (
        len(stock_rows) == 1
    ), f"expected exactly one stock summary row for the product, got {len(stock_rows)}"
    on_hand = stock_rows[0].on_hand
    assert on_hand >= 0, f"on_hand went negative: {on_hand}"


async def _run_example(dsn: str, plan: list[Op], seed_stock: int) -> None:
    """One Hypothesis example, fully self-contained: fresh engine, fresh

    company, fresh everything (see module docstring for why this must
    never share state with any other example).
    """
    engine = create_async_engine(dsn)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            company = await masterdata_service.create_company(
                session,
                CompanyCreate(
                    code=f"HYPO2C{uuid.uuid4().hex[:10].upper()}",
                    name="Hypothesis O2C Co",
                    functional_currency_code="TWD",
                ),
            )
            with company_context(company.id):
                for code, name, acct_type in _STANDARD_CHART:
                    await masterdata_service.create_account(
                        session, AccountCreate(code=code, name=name, type=acct_type)
                    )
                # Codex diff review (Week 7 slice 5, finding 1; broadened in
                # round 2, finding A): postings in this flow default their
                # own date inconsistently — `sales.service.ship_order`/
                # `receivables.service.create_payment` both stamp
                # `datetime.now(timezone.utc)`, while
                # `receivables.service.create_invoice` defaults to local
                # `date.today()`. Near a month boundary, local and UTC can
                # disagree on which month "now" is in (e.g. Taipei UTC+8
                # just after local midnight on the 1st is still the
                # previous month in UTC). Round 1 covered that by creating
                # a period for both bases; round 2 pointed out these two
                # snapshots are taken here, BEFORE the example's several
                # subsequent DB round trips actually execute — if the
                # wall clock crosses a month boundary mid-example, a
                # later `datetime.now(timezone.utc)` call inside a service
                # function could still land in a month neither snapshot
                # covers. Also including the month immediately following
                # each snapshot closes that gap too: a single Hypothesis
                # example's execution (a handful of DB round trips) cannot
                # advance the wall clock by more than one calendar month,
                # so {this month, next month} x {local, UTC} is a strict
                # superset of every month any date default in this example
                # could possibly resolve to. Creating a period for each
                # distinct (year, month) (deduplicated via the `set`, since
                # they usually collapse to one or two) is correct
                # regardless of which basis a given posting call resolves
                # to, and costs nothing extra when they coincide.
                local_today = date.today()
                utc_today = datetime.now(timezone.utc).date()  # noqa: UP017
                periods_needed: set[tuple[int, int]] = set()
                for basis in (local_today, utc_today):
                    periods_needed.add((basis.year, basis.month))
                    next_month = (basis.replace(day=28) + timedelta(days=4)).replace(day=1)
                    periods_needed.add((next_month.year, next_month.month))
                for year, month in periods_needed:
                    await ledger_service.create_period(
                        session, AccountingPeriodCreate(year=year, month=month)
                    )
                customer = await masterdata_service.create_customer(
                    session,
                    CustomerCreate(
                        code=f"HYPCUST{uuid.uuid4().hex[:8].upper()}",
                        name="Hypothesis Customer",
                        currency_code="TWD",
                        # "do not check" sentinel — see module docstring.
                        credit_limit=Decimal("0"),
                    ),
                )
                uoms = await masterdata_service.list_uoms(session)
                ea_id = next(u.id for u in uoms if u.code == "EA")
                product = await masterdata_service.create_product(
                    session,
                    ProductCreate(
                        sku=f"HYPSKU{uuid.uuid4().hex[:8].upper()}",
                        name="Hypothesis Widget",
                        uom_id=ea_id,
                        list_price=_UNIT_PRICE,
                        standard_cost=_STANDARD_COST,
                    ),
                )
                # Seeded to exactly the plan's total demand — see module
                # docstring "Stock provisioning".
                await inventory_service.create_adjustment(
                    session,
                    StockAdjustmentCreate(
                        product_id=product.id,
                        qty_delta=Decimal(seed_stock),
                        reason="hypothesis property test: seed exactly the plan's total demand",
                    ),
                )

                await _execute_plan(session, plan, customer.id, product.id)
                await _assert_invariants(session, product.id)
    finally:
        await engine.dispose()


def test_property_o2c_balances_hold_under_any_legal_operation_sequence(
    postgres_dsn: str,
) -> None:
    """Deliberately a plain (non-`async def`) test function — see

    `tests/ledger/test_property_trial_balance.py`'s module docstring for
    why: `asyncio.run()` cannot start a new event loop while one is already
    running, so this must stay fully synchronous for `_run_example`'s
    per-example `asyncio.run()` calls to each own a brand-new loop.
    pytest-asyncio still resolves this function's async fixture
    dependencies (`postgres_dsn` transitively pulls in `_run_migrations`,
    `db_engine`, `_seed_reference_data`) in their own loop before this body
    ever runs — standard, supported pytest-asyncio behaviour for sync tests
    with async fixtures.
    """

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(data=st.data())
    def run_example(data: st.DataObject) -> None:
        plan = _draw_plan(data)
        # Codex diff review (Week 7 slice 5, finding 2): seed exactly the
        # quantity of orders that actually get a `Ship` op — NOT every
        # `NewOrder`'s qty. A drawn plan can legally create an order and
        # never confirm/ship it; seeding that order's qty too would over-
        # provision stock, silently absorbing a double-decrement bug in the
        # ship-time stock move (the `on_hand >= 0` assertion would still
        # pass on the leftover slack instead of catching it). Each order
        # ships at most once (`_draw_plan` only offers `ship` from
        # `confirmed_idxs`, which excludes an already-shipped order), so
        # summing by `Ship.order_idx` never double-counts.
        order_qtys = [op.qty for op in plan if isinstance(op, NewOrder)]
        seed_stock = sum(order_qtys[op.order_idx] for op in plan if isinstance(op, Ship))
        asyncio.run(_run_example(postgres_dsn, plan, seed_stock))

    run_example()
