"""credit_limit — the Week 4 demonstration plugin (ADR-006).

Registers on `app.modules.sales.service.SALES_ORDER_VALIDATE_CONFIRM` (see
`app.main` for the actual `hooks.register(...)` call): before a sales order
is allowed to move `draft -> confirmed`, checks that this customer's
already-`CONFIRMED` order totals plus the order now confirming stay within
`customer.credit_limit`.

**Exposure formula (Phase 1, ADR-006 "Credit-limit plugin" section)**:
`SUM(total)` of this customer's `CONFIRMED` orders + the order being
confirmed `<= customer.credit_limit`. Cancelled orders drop out of the SUM
automatically (the query filters on `status == CONFIRMED`); AR balance
joins the formula in Week 6 (documented revisit).

**`credit_limit == 0` means "do not check"** — an explicit, documented
sentinel (a real zero-credit customer needs a small positive limit, or a
Phase 2 customer-hold flag, instead).

**TOCTOU**: `SELECT ... FOR UPDATE` on the customer row, taken before
summing, serializes concurrent confirms for the same customer — two orders
racing to confirm against the same near-limit customer cannot both read the
same "current exposure" and both pass; the second blocks until the first's
transaction ends, then re-sums including whatever the first just committed
(or doesn't, if the first's confirm outright failed and rolled back). Same
doctrine as ADR-005 R4 / master-plan §10.5.

**Import note** (ADR-006 Decision 2): plugins are explicitly NOT bound by
the `app.modules.*` independence contract that `sales`/`ledger`/etc. are
bound by — only `app.core`/`app.modules` are forbidden from importing
`app.plugins`, never the reverse (see `pyproject.toml`'s two new
`import-linter` contracts). This plugin imports
`app.modules.masterdata.models.Customer` directly (the real ORM model, not
a `sqlalchemy.table()` shadow of `customers` the way `ledger`/`sales`
services must, since *they* are inside the independence contract) — using
the mapped class here keeps `with_for_update()`/`customer.credit_limit`
access idiomatic instead of re-declaring a bespoke Core column list for a
single query. `SalesOrder` is imported from `app.modules.sales.models` the
same way, for the same reason. This is the "public surface only" trade-off
ADR-006 documents and defers enforcing to Phase 2's plugin loader — see
`README.md` in this directory for the guidance given to future plugin
authors.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CreditLimitExceededError, DomainValidationError
from app.core.hooks import HookContext
from app.modules.masterdata.models import Customer
from app.modules.sales.models import SalesOrder, SalesOrderStatus


async def check_credit_limit(session: AsyncSession, context: HookContext) -> None:
    """Hook handler for `sales.order.validate_confirm` (ADR-006).

    Raises `CreditLimitExceededError` (a `ConflictError` subclass -> HTTP
    409) if confirming this order would push the customer's confirmed-order
    exposure past its `credit_limit`. Returns (no-op) if the limit is the
    "unchecked" sentinel (`0`) or the order is within limit.
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

    exposure_result = await session.execute(
        select(func.coalesce(func.sum(SalesOrder.total), Decimal("0"))).where(
            SalesOrder.customer_id == context.customer_id,
            SalesOrder.status == SalesOrderStatus.CONFIRMED,
        )
    )
    confirmed_total: Decimal = exposure_result.scalar_one()
    exposure = confirmed_total + context.total

    if exposure > customer.credit_limit:
        raise CreditLimitExceededError(
            f"customer {context.customer_id} would exceed its credit limit "
            f"({customer.credit_limit}): existing confirmed exposure {confirmed_total} + "
            f"this order's total {context.total} = {exposure}"
        )
