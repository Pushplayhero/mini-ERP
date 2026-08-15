"""Rebuild `invoices.settled_amount`/`payments.allocated_amount` from
`payment_allocations` (ADR-008 Decision 3 / R6).

`payment_allocations` is the append-only source of truth; the two
maintained columns on `invoices`/`payments` are rebuildable projections of
it (`SUM(amount)` grouped by `invoice_id`/`payment_id` respectively). This
CLI recomputes both projections from scratch, one company at a time,
mirroring `app.cli.rebuild_stock_summary`'s lock discipline (see that
module's docstring for the full correctness argument, replayed here for
receivables' shape).

**Locking discipline**: for each company, this runs in ONE transaction
that locks every one of that company's `payments` and `invoices` rows
(`SELECT ... FOR UPDATE`, ordered by id, payments before invoices —
matching `service.allocate_payment`'s own lock order) before computing any
SUM from `payment_allocations`. Unlike `stock_summary`, receivables has no
"phantom row" gap to guard with an advisory lock: an `Invoice`/`Payment`
row always exists before any allocation can reference it (created by
`create_invoice`/`create_payment` respectively, both of which flush before
any allocation touches them), so locking every *existing* row is already
exhaustive — there is no row this rebuild could miss by it not existing
yet.

Also re-derives each non-voided invoice's `status` from the recomputed
`settled_amount` (`open <-> partial <-> paid`), matching
`service.allocate_payment`'s own derivation exactly — a status drift
(e.g. from a bug fixed after the fact) is corrected by the same rebuild.
`VOIDED` invoices/payments are read (for their locked row) but never
re-derived past that status.

Because the locks are held for the whole rebuild of one company, this
command blocks concurrent invoice/payment/allocation writes for that
company for as long as the rebuild takes — the entry point prints a
warning about this; run it during an offline maintenance window, not
against a live, high-write production database.

Entry point: `uv run python -m app.cli.rebuild_ar_balances`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import column as sa_column
from sqlalchemy import func, select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import dispose_engine, get_session_factory
from app.core.tenancy import company_context
from app.modules.receivables.models import Invoice, InvoiceStatus, Payment, PaymentAllocation

logger = logging.getLogger("app.cli.rebuild_ar_balances")

# Lightweight Core reference to `companies` — same pattern
# `rebuild_stock_summary` uses to iterate companies without pulling in the
# full `masterdata.models.Company` ORM surface.
_COMPANIES = sa_table("companies", sa_column("id", PGUUID(as_uuid=True)), sa_column("code"))


@dataclass
class RebuildSummary:
    companies: int = 0
    invoices_updated: int = 0
    payments_updated: int = 0


async def _rebuild_company(
    session_factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> tuple[int, int]:
    """Rebuild one company's invoice/payment balances inside a single transaction.

    See module docstring for the full lock-before-sum correctness argument.
    """
    async with session_factory() as session:
        with company_context(company_id):
            locked_payments = await session.execute(
                select(Payment)
                .where(Payment.company_id == company_id)
                .order_by(Payment.id)
                .with_for_update()
            )
            payments = {p.id: p for p in locked_payments.scalars().all()}

            locked_invoices = await session.execute(
                select(Invoice)
                .where(Invoice.company_id == company_id)
                .order_by(Invoice.id)
                .with_for_update()
            )
            invoices = {i.id: i for i in locked_invoices.scalars().all()}

            # Only now — with every row locked — compute true totals from
            # the append-only source of truth.
            invoice_totals_result = await session.execute(
                select(PaymentAllocation.invoice_id, func.sum(PaymentAllocation.amount))
                .where(PaymentAllocation.company_id == company_id)
                .group_by(PaymentAllocation.invoice_id)
            )
            invoice_totals: dict[uuid.UUID, Decimal] = {
                row[0]: row[1] for row in invoice_totals_result.all()
            }

            payment_totals_result = await session.execute(
                select(PaymentAllocation.payment_id, func.sum(PaymentAllocation.amount))
                .where(PaymentAllocation.company_id == company_id)
                .group_by(PaymentAllocation.payment_id)
            )
            payment_totals: dict[uuid.UUID, Decimal] = {
                row[0]: row[1] for row in payment_totals_result.all()
            }

            invoices_updated = 0
            for invoice_id, invoice in invoices.items():
                settled = invoice_totals.get(invoice_id, Decimal("0"))
                if invoice.settled_amount != settled:
                    invoice.settled_amount = settled
                    invoices_updated += 1
                if invoice.status != InvoiceStatus.VOIDED:
                    if settled >= invoice.total:
                        invoice.status = InvoiceStatus.PAID
                    elif settled > 0:
                        invoice.status = InvoiceStatus.PARTIAL
                    else:
                        invoice.status = InvoiceStatus.OPEN

            payments_updated = 0
            for payment_id, payment in payments.items():
                allocated = payment_totals.get(payment_id, Decimal("0"))
                if payment.allocated_amount != allocated:
                    payment.allocated_amount = allocated
                    payments_updated += 1

            await session.commit()
    return invoices_updated, payments_updated


async def rebuild_ar_balances() -> RebuildSummary:
    """Rebuild every company's invoice/payment balances, one company-transaction at a time."""
    summary = RebuildSummary()
    session_factory = get_session_factory()

    async with session_factory() as session:
        result = await session.execute(select(_COMPANIES.c.id).order_by(_COMPANIES.c.code))
        company_ids = [row[0] for row in result.all()]

    for company_id in company_ids:
        invoices_updated, payments_updated = await _rebuild_company(session_factory, company_id)
        summary.companies += 1
        summary.invoices_updated += invoices_updated
        summary.payments_updated += payments_updated

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.warning(
        "rebuild_ar_balances: this command takes `SELECT ... FOR UPDATE` locks on every "
        "payment and invoice row for each company in turn, one company-transaction at a "
        "time. Concurrent invoice/payment/allocation writes for a company currently being "
        "rebuilt will BLOCK until that company's rebuild transaction commits. Run this "
        "during an offline maintenance window, not against a live, high-write production "
        "database."
    )
    result = await rebuild_ar_balances()
    print(
        f"rebuild summary: companies={result.companies} "
        f"invoices_updated={result.invoices_updated} payments_updated={result.payments_updated}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
