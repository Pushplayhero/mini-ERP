"""Rebuild `vendor_bills.settled_amount` / `vendor_payments.allocated_amount` from
`vendor_payment_allocations` (ADR-015 Decision 3).

Mirrors `app.cli.rebuild_ar_balances`: for each company in turn inside ONE
transaction, locks every `VendorPayment` then every `VendorBill` ordered by `id`
(`SELECT ... FOR UPDATE`), recomputes balances from `vendor_payment_allocations`,
and re-derives non-voided `VendorBill.status` (`open <-> partial <-> paid`).

Entry point: `uv run python -m app.cli.rebuild_ap_balances`.
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
from app.modules.payables.models import (
    VendorBill,
    VendorBillStatus,
    VendorPayment,
    VendorPaymentAllocation,
)

logger = logging.getLogger("app.cli.rebuild_ap_balances")

_COMPANIES = sa_table("companies", sa_column("id", PGUUID(as_uuid=True)), sa_column("code"))


@dataclass
class RebuildSummary:
    companies: int = 0
    bills_updated: int = 0
    payments_updated: int = 0


async def _rebuild_company(
    session_factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> tuple[int, int]:
    """Rebuild one company's vendor bill/payment balances inside a single transaction."""
    async with session_factory() as session:
        with company_context(company_id):
            locked_payments = await session.execute(
                select(VendorPayment)
                .where(VendorPayment.company_id == company_id)
                .order_by(VendorPayment.id)
                .with_for_update()
            )
            payments = {p.id: p for p in locked_payments.scalars().all()}

            locked_bills = await session.execute(
                select(VendorBill)
                .where(VendorBill.company_id == company_id)
                .order_by(VendorBill.id)
                .with_for_update()
            )
            bills = {b.id: b for b in locked_bills.scalars().all()}

            bill_totals_result = await session.execute(
                select(VendorPaymentAllocation.bill_id, func.sum(VendorPaymentAllocation.amount))
                .where(VendorPaymentAllocation.company_id == company_id)
                .group_by(VendorPaymentAllocation.bill_id)
            )
            bill_totals: dict[uuid.UUID, Decimal] = {
                row[0]: row[1] for row in bill_totals_result.all()
            }

            payment_totals_result = await session.execute(
                select(VendorPaymentAllocation.payment_id, func.sum(VendorPaymentAllocation.amount))
                .where(VendorPaymentAllocation.company_id == company_id)
                .group_by(VendorPaymentAllocation.payment_id)
            )
            payment_totals: dict[uuid.UUID, Decimal] = {
                row[0]: row[1] for row in payment_totals_result.all()
            }

            bills_updated = 0
            for bill_id, bill in bills.items():
                settled = bill_totals.get(bill_id, Decimal("0"))
                if bill.settled_amount != settled:
                    bill.settled_amount = settled
                    bills_updated += 1
                if bill.status != VendorBillStatus.VOIDED:
                    if settled >= bill.total:
                        bill.status = VendorBillStatus.PAID
                    elif settled > 0:
                        bill.status = VendorBillStatus.PARTIAL
                    else:
                        bill.status = VendorBillStatus.OPEN

            payments_updated = 0
            for payment_id, payment in payments.items():
                allocated = payment_totals.get(payment_id, Decimal("0"))
                if payment.allocated_amount != allocated:
                    payment.allocated_amount = allocated
                    payments_updated += 1

            await session.commit()
    return bills_updated, payments_updated


async def rebuild_ap_balances() -> RebuildSummary:
    """Rebuild every company's vendor bill/payment balances, one company at a time."""
    summary = RebuildSummary()
    session_factory = get_session_factory()

    async with session_factory() as session:
        result = await session.execute(select(_COMPANIES.c.id).order_by(_COMPANIES.c.code))
        company_ids = [row[0] for row in result.all()]

    for company_id in company_ids:
        bills_updated, payments_updated = await _rebuild_company(session_factory, company_id)
        summary.companies += 1
        summary.bills_updated += bills_updated
        summary.payments_updated += payments_updated

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.warning(
        "rebuild_ap_balances: takes SELECT ... FOR UPDATE locks on all vendor payments and "
        "bills per company in turn. Run during an offline maintenance window."
    )
    result = await rebuild_ap_balances()
    print(
        f"rebuild summary: companies={result.companies} "
        f"bills_updated={result.bills_updated} payments_updated={result.payments_updated}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
