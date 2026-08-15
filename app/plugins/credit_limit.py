"""credit_limit — the Week 4 demonstration plugin (ADR-006), exposure

formula revised for receivables (ADR-008 Decision 6).

Registers on `app.modules.sales.service.SALES_ORDER_VALIDATE_CONFIRM` (see
`app.main` for the actual `hooks.register(...)` call): before a sales order
is allowed to move `draft -> confirmed`, checks that this customer's
current exposure plus the order now confirming stays within
`customer.credit_limit`.

**Exposure formula (ADR-008 Decision 6, revising ADR-006's Week 4
version)**:

```
exposure = Σ total       over orders  status IN (CONFIRMED, SHIPPED)
                                      AND NOT EXISTS (non-voided invoice for order)
         + Σ (total - settled_amount) over non-voided invoices
         + total of the order now confirming
```

This closes a real Week 5 gap the old formula had: it summed only
`status == CONFIRMED` orders, so a `SHIPPED` order — goods gone, nothing
paid — silently *dropped out* of exposure the moment it shipped. Under the
new formula an order counts exactly once through its life: as an
uninvoiced order until an invoice exists for it, then as an open invoice
balance until fully settled, and only then drops out. Unapplied credits
(a customer's un-allocated payments) deliberately do NOT offset exposure
here — conservative, since cash a customer could still reclaim on account
is not collateral (ADR-008 Decision 6, revisit in Phase 2 alongside
receipt-time-vs-allocation-time posting).

**`credit_limit == 0` means "do not check"** — an explicit, documented
sentinel (a real zero-credit customer needs a small positive limit, or a
Phase 2 customer-hold flag, instead).

**TOCTOU**: `SELECT ... FOR UPDATE` on the customer row, taken before
summing, serializes concurrent confirms for the same customer — two orders
racing to confirm against the same near-limit customer cannot both read the
same "current exposure" and both pass; the second blocks until the first's
transaction ends, then re-sums including whatever the first just committed
(or doesn't, if the first's confirm outright failed and rolled back). Same
doctrine as ADR-005 R4 / master-plan §10.5. This lock already covers the
new invoice-balance term too — nothing about an invoice's `settled_amount`
changes without a lock ADR-008 already serializes on (`ledger.service`'s
period lock and `receivables.service.allocate_payment`'s row locks are
orthogonal to this one; the credit check re-reads whatever committed state
exists at the moment it runs, which is all the TOCTOU guarantee ever
promised).

**Import note** (ADR-006 Decision 2): plugins are explicitly NOT bound by
the `app.modules.*` independence contract that `sales`/`ledger`/`receivables`/
etc. are bound by — only `app.core`/`app.modules` are forbidden from
importing `app.plugins`, never the reverse (see `pyproject.toml`'s
import-linter contracts). This plugin imports
`app.modules.masterdata.models.Customer`, `app.modules.sales.models
.SalesOrder`, and (new, ADR-008) `app.modules.receivables.models.Invoice`
directly (the real ORM models, not `sqlalchemy.table()` shadows the way
`ledger`/`sales`/`receivables` services must, since *they* are inside the
independence contract) — using the mapped classes here keeps
`with_for_update()`/column access idiomatic instead of re-declaring a
bespoke Core column list for a single query. This is the "public surface
only" trade-off ADR-006 documents and defers enforcing to Phase 2's plugin
loader — see `README.md` in this directory for the guidance given to
future plugin authors.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CreditLimitExceededError, DomainValidationError
from app.core.hooks import HookContext
from app.modules.masterdata.models import Customer
from app.modules.receivables.models import Invoice, InvoiceStatus
from app.modules.sales.models import SalesOrder, SalesOrderStatus


async def check_credit_limit(session: AsyncSession, context: HookContext) -> None:
    """Hook handler for `sales.order.validate_confirm` (ADR-006, ADR-008 Decision 6).

    Raises `CreditLimitExceededError` (a `ConflictError` subclass -> HTTP
    409) if confirming this order would push the customer's exposure past
    its `credit_limit`. Returns (no-op) if the limit is the "unchecked"
    sentinel (`0`) or the order is within limit.
    """
    result = await session.execute(
        select(Customer).where(Customer.id == context.customer_id).with_for_update()
    )
    customer = result.scalar_one_or_none()
    if customer is None:
        raise DomainValidationError(
            f"customer {context.customer_id} not found or does not belong to this company"
        )

    if customer.credit_limit == 0:
        return

    zero = Decimal("0")

    # Codex diff review (2026-08-15, finding 2): the customer row lock above
    # serializes concurrent *confirms* for this customer, but does not
    # serialize against `receivables.void_invoice`/`create_invoice`, which
    # touch only the invoice row. Reading the two terms via separate
    # `session.execute()` calls left a window where an invoice void
    # committing between them drops an order out of BOTH sums at once: the
    # uninvoiced-orders query (run first) already excluded it because a
    # live invoice existed at that instant, and the open-invoice query (run
    # second) now excludes it too because that invoice is voided — silently
    # understating exposure and potentially letting a concurrent confirm
    # slip past the limit. Folding both aggregates into one SELECT (two
    # scalar subqueries) makes them share a single READ COMMITTED statement
    # snapshot, closing the window; the customer lock is retained for its
    # original purpose (serializing concurrent confirms against each other).
    uninvoiced_subquery = (
        select(func.coalesce(func.sum(SalesOrder.total), zero))
        .where(
            SalesOrder.customer_id == context.customer_id,
            SalesOrder.status.in_([SalesOrderStatus.CONFIRMED, SalesOrderStatus.SHIPPED]),
            ~SalesOrder.id.in_(
                select(Invoice.order_id).where(Invoice.status != InvoiceStatus.VOIDED)
            ),
        )
        .scalar_subquery()
    )
    open_invoice_subquery = (
        select(func.coalesce(func.sum(Invoice.total - Invoice.settled_amount), zero))
        .where(Invoice.customer_id == context.customer_id, Invoice.status != InvoiceStatus.VOIDED)
        .scalar_subquery()
    )
    exposure_result = await session.execute(
        select(
            uninvoiced_subquery.label("uninvoiced_total"),
            open_invoice_subquery.label("open_invoice_total"),
        )
    )
    exposure_row = exposure_result.one()
    uninvoiced_total: Decimal = exposure_row.uninvoiced_total
    open_invoice_total: Decimal = exposure_row.open_invoice_total

    exposure = uninvoiced_total + open_invoice_total + context.total

    if exposure > customer.credit_limit:
        raise CreditLimitExceededError(
            f"customer {context.customer_id} would exceed its credit limit "
            f"({customer.credit_limit}): uninvoiced orders {uninvoiced_total} + open invoices "
            f"{open_invoice_total} + this order's total {context.total} = {exposure}"
        )
