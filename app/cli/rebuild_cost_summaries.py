"""Rebuild `product_cost_summaries` from append-only `cost_moves` (ADR-016).

Mirrors `app.cli.rebuild_stock_summary`: for each company in turn inside ONE
transaction, acquires the EXCLUSIVE per-company advisory lock
(`app.core.locking`), locks every existing `ProductCostSummary` row ordered by
`product_id` (`SELECT ... FOR UPDATE`), then replays all `CostMove` rows in
`(created_at, id)` ascending order to recompute `tracked_qty`,
`total_cost_value`, and `moving_avg_cost` (`ROUND_HALF_EVEN` to 6dp).

Entry point: `uv run python -m app.cli.rebuild_cost_summaries`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import column as sa_column
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import dispose_engine, get_session_factory
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import company_context
from app.modules.costing.models import CostMove, ProductCostSummary

logger = logging.getLogger("app.cli.rebuild_cost_summaries")

_COMPANIES = sa_table("companies", sa_column("id", PGUUID(as_uuid=True)), sa_column("code"))
_MONEY_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0.000000")


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass
class RebuildSummary:
    companies: int = 0
    products_updated: int = 0


async def _rebuild_company(
    session_factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> int:
    """Rebuild one company's `product_cost_summaries` inside a single transaction."""
    async with session_factory() as session:
        with company_context(company_id):
            await acquire_company_advisory_lock(session, company_id, exclusive=True)

            locked = await session.execute(
                select(ProductCostSummary)
                .where(ProductCostSummary.company_id == company_id)
                .order_by(ProductCostSummary.product_id)
                .with_for_update()
            )
            existing = {row.product_id: row for row in locked.scalars().all()}

            moves_result = await session.execute(
                select(CostMove)
                .where(CostMove.company_id == company_id)
                .order_by(CostMove.created_at, CostMove.id)
            )
            recomputed: dict[uuid.UUID, tuple[Decimal, Decimal, Decimal]] = {}
            for move in moves_result.scalars().all():
                prev_qty, prev_val, prev_avg = recomputed.get(
                    move.product_id, (_ZERO, _ZERO, _ZERO)
                )
                new_qty = _round_half_even_6dp(prev_qty + move.qty_delta)
                new_val = _round_half_even_6dp(prev_val + move.value_delta)
                new_avg = _round_half_even_6dp(new_val / new_qty) if new_qty > 0 else prev_avg
                recomputed[move.product_id] = (new_qty, new_val, new_avg)

            updated = 0
            for product_id in set(existing) | set(recomputed):
                qty, val, avg = recomputed.get(product_id, (_ZERO, _ZERO, _ZERO))
                summary_row = existing.get(product_id)
                if summary_row is None:
                    session.add(
                        ProductCostSummary(
                            company_id=company_id,
                            product_id=product_id,
                            tracked_qty=qty,
                            total_cost_value=val,
                            moving_avg_cost=avg,
                        )
                    )
                else:
                    summary_row.tracked_qty = qty
                    summary_row.total_cost_value = val
                    summary_row.moving_avg_cost = avg
                updated += 1

            await session.commit()
    return updated


async def rebuild_cost_summaries() -> RebuildSummary:
    """Rebuild every company's `product_cost_summaries`, one company at a time."""
    summary = RebuildSummary()
    session_factory = get_session_factory()

    async with session_factory() as session:
        result = await session.execute(select(_COMPANIES.c.id).order_by(_COMPANIES.c.code))
        company_ids = [row[0] for row in result.all()]

    for company_id in company_ids:
        updated = await _rebuild_company(session_factory, company_id)
        summary.companies += 1
        summary.products_updated += updated

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.warning(
        "rebuild_cost_summaries: takes exclusive advisory + SELECT ... FOR UPDATE locks on "
        "all product cost summaries per company in turn. Run during an offline maintenance window."
    )
    result = await rebuild_cost_summaries()
    print(
        f"rebuild summary: companies={result.companies} products_updated={result.products_updated}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
