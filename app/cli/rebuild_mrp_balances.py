"""Rebuild `work_orders` (`completed_qty`, `wip_balance`) & `work_order_lines` balances (ADR-017).

Replays append-only `work_order_transactions` in `(created_at, id)` ascending order under
`SELECT ... FOR UPDATE` (`populate_existing=True`) for each company.

Entry point: `uv run python -m app.cli.rebuild_mrp_balances`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import column as sa_column
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.db import dispose_engine, get_session_factory
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import company_context
from app.modules.mrp.models import WorkOrder, WorkOrderTransaction, WorkOrderTxType

logger = logging.getLogger("app.cli.rebuild_mrp_balances")

_COMPANIES = sa_table("companies", sa_column("id", PGUUID(as_uuid=True)), sa_column("code"))
_MONEY_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0.000000")


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass
class RebuildSummary:
    companies: int = 0
    work_orders_updated: int = 0


async def _rebuild_company(
    session_factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        with company_context(company_id):
            await acquire_company_advisory_lock(session, company_id, exclusive=True)
            wos = (
                (
                    await session.execute(
                        select(WorkOrder)
                        .where(WorkOrder.company_id == company_id)
                        .options(selectinload(WorkOrder.lines))
                        .order_by(WorkOrder.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            txs = (
                (
                    await session.execute(
                        select(WorkOrderTransaction)
                        .where(WorkOrderTransaction.company_id == company_id)
                        .order_by(WorkOrderTransaction.created_at, WorkOrderTransaction.id)
                    )
                )
                .scalars()
                .all()
            )

            wo_completed: dict[uuid.UUID, Decimal] = defaultdict(lambda: _ZERO)
            wo_wip: dict[uuid.UUID, Decimal] = defaultdict(lambda: _ZERO)
            line_issued_qty: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = defaultdict(lambda: _ZERO)
            line_returned_qty: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = defaultdict(
                lambda: _ZERO
            )
            line_issued_cost: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = defaultdict(
                lambda: _ZERO
            )
            line_returned_cost: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = defaultdict(
                lambda: _ZERO
            )

            for tx in txs:
                wid = tx.work_order_id
                if tx.tx_type is WorkOrderTxType.MATERIAL_ISSUE:
                    wo_wip[wid] = _round_half_even_6dp(wo_wip[wid] + tx.total_cost)
                    for item in tx.lines_payload:
                        pid = uuid.UUID(str(item["product_id"]))
                        key = (wid, pid)
                        line_issued_qty[key] = _round_half_even_6dp(
                            line_issued_qty[key] + Decimal(str(item["qty"]))
                        )
                        line_issued_cost[key] = _round_half_even_6dp(
                            line_issued_cost[key] + Decimal(str(item["line_cost"]))
                        )
                elif tx.tx_type is WorkOrderTxType.MATERIAL_RETURN:
                    wo_wip[wid] = _round_half_even_6dp(wo_wip[wid] - tx.total_cost)
                    for item in tx.lines_payload:
                        pid = uuid.UUID(str(item["product_id"]))
                        key = (wid, pid)
                        line_returned_qty[key] = _round_half_even_6dp(
                            line_returned_qty[key] + Decimal(str(item["qty"]))
                        )
                        line_returned_cost[key] = _round_half_even_6dp(
                            line_returned_cost[key] + Decimal(str(item["line_cost"]))
                        )
                elif tx.tx_type is WorkOrderTxType.PRODUCTION_RECEIPT:
                    if tx.received_qty is not None:
                        wo_completed[wid] = _round_half_even_6dp(
                            wo_completed[wid] + tx.received_qty
                        )
                    wo_wip[wid] = _round_half_even_6dp(wo_wip[wid] - tx.total_cost)

            updated = 0
            for wo in wos:
                wo.completed_qty = wo_completed[wo.id]
                wo.wip_balance = wo_wip[wo.id]
                for ln in wo.lines:
                    key = (wo.id, ln.component_product_id)
                    ln.issued_qty = line_issued_qty[key]
                    ln.returned_qty = line_returned_qty[key]
                    ln.issued_cost = line_issued_cost[key]
                    ln.returned_cost = line_returned_cost[key]
                updated += 1

            await session.commit()
    return updated


async def rebuild_mrp_balances() -> RebuildSummary:
    """Rebuild every company's MRP work order and line balances from `work_order_transactions`."""
    summary = RebuildSummary()
    session_factory = get_session_factory()

    async with session_factory() as session:
        result = await session.execute(select(_COMPANIES.c.id).order_by(_COMPANIES.c.code))
        company_ids = [row[0] for row in result.all()]

    for company_id in company_ids:
        updated = await _rebuild_company(session_factory, company_id)
        summary.companies += 1
        summary.work_orders_updated += updated

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.warning(
        "rebuild_mrp_balances: locks work orders per company in turn. Run during an offline "
        "maintenance window."
    )
    result = await rebuild_mrp_balances()
    print(
        f"rebuild summary: companies={result.companies} "
        f"work_orders_updated={result.work_orders_updated}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
