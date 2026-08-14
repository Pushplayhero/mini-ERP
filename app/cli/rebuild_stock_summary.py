"""Rebuild `stock_summary` from `stock_moves` (ADR-007 Decision 2 / R2).

`stock_moves` is the append-only source of truth; `stock_summary` is a
maintained-but-rebuildable projection of it (`SUM(qty_delta)` per
`(company_id, product_id)`). This CLI recomputes that projection from
scratch, one company at a time, and is the documented recovery tool if the
two are ever suspected to have drifted apart.

**R2's locking discipline (read before touching this file)**: for each
company, this runs in ONE transaction that first takes `SELECT ... FOR
UPDATE` on every one of that company's existing `stock_summary` rows, and
only THEN computes `SUM(qty_delta) ... GROUP BY product_id` from
`stock_moves`. This order is not incidental — it is the entire correctness
argument:

- `service.get_or_lock_summary` (the function every shipment/adjustment
  goes through) takes the exact same row-level `FOR UPDATE` lock on a
  `stock_summary` row before it inserts a move and updates `on_hand`. Two
  transactions that both try to touch the same summary row therefore always
  serialize on that row's lock, regardless of which one is this rebuild and
  which one is a live shipment.
- Locking *first* means a concurrent shipment for this company either (a)
  already committed before this rebuild's lock was granted — in which case
  its `stock_moves` row is already there, and the subsequent `SUM` correctly
  includes it — or (b) is still in flight and blocks on the row lock until
  this rebuild's transaction commits or rolls back, and then applies
  cleanly on top of whatever this rebuild just wrote.
- Reversing the order (summing first, locking second) would open a window
  between the `SUM` and the lock acquisition where a concurrent shipment
  could commit a new move that the `SUM` never saw, and then this rebuild
  would overwrite `on_hand` with an already-stale snapshot, silently
  reintroducing the exact drift this tool exists to fix — and, because the
  `stock_summary.on_hand >= 0` CHECK constraint only protects against
  *negative* totals, a stale overwrite that happens to still be
  non-negative would pass every DB-level check while being wrong.

Because the lock is held for the whole rebuild of one company, this command
blocks concurrent shipments/adjustments for that company for as long as the
rebuild takes — the entry point prints a loud warning about this; run it
during an offline maintenance window, not against a live, high-write
production database (mirrors `app.cli.replay_outbox`'s "each row/company
gets its own session/transaction" structure, but this CLI cannot shrink its
lock scope the way replay shrinks to one outbox row at a time, because the
whole point is a consistent snapshot of one company's entire stock
position).

**Diff-review fix on top of R2 — the phantom-row gap.** "Lock every
*existing* row" has a hole: a product with moves but no summary row yet
(deleted, or never created) has nothing for `FOR UPDATE` to lock, so a
concurrent writer creating exactly that row could still race this rebuild's
own insert of it. Before locking anything, this function now takes the
EXCLUSIVE per-company advisory lock from `app.core.locking`; every normal
writer (`inventory.service.get_or_lock_summary`) takes the SHARED form of
the same lock before it touches any row for that company. That closes the
gap: no writer can be "in the middle of" creating a row for this company
while a rebuild holds the exclusive lock, so by the time this function's
own `FOR UPDATE` step runs, every row that could possibly need locking
already exists (or belongs to a writer still queued behind the exclusive
lock, which is exactly the serialization already documented above).

Existing rows are also now locked in a fixed order (`ORDER BY product_id`,
ascending) — the same order `inventory.service.handle_goods_shipped`
acquires per-product locks in — so a rebuild racing a shipment can never
deadlock through the row locks themselves, on top of the advisory lock
already serializing them at a coarser grain.

Entry point: `uv run python -m app.cli.rebuild_stock_summary`.
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
from app.core.locking import acquire_company_advisory_lock
from app.core.tenancy import company_context
from app.modules.inventory.models import StockMove, StockSummary

logger = logging.getLogger("app.cli.rebuild_stock_summary")

# Lightweight Core reference to `companies` (same pattern used throughout
# `ledger.service`/`ledger.posting`/`sales.service` for shared, non-owned
# tables) — this CLI only needs the id/code list to iterate companies, not
# the full `masterdata.models.Company` ORM surface.
_COMPANIES = sa_table("companies", sa_column("id", PGUUID(as_uuid=True)), sa_column("code"))


@dataclass
class RebuildSummary:
    companies: int = 0
    products_updated: int = 0


async def _rebuild_company(
    session_factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> int:
    """Rebuild one company's `stock_summary` inside a single transaction.

    See module docstring for the full lock-before-sum correctness argument.
    """
    async with session_factory() as session:
        with company_context(company_id):
            # EXCLUSIVE per-company advisory lock first — see module
            # docstring's "phantom-row gap" note. Waits for every in-flight
            # writer (holding the SHARED form) to finish, then blocks any
            # new writer until this whole rebuild transaction ends.
            await acquire_company_advisory_lock(session, company_id, exclusive=True)

            # Then lock every existing summary row for this company, all at
            # once and in a fixed order, before any SUM is computed.
            locked = await session.execute(
                select(StockSummary)
                .where(StockSummary.company_id == company_id)
                .order_by(StockSummary.product_id)
                .with_for_update()
            )
            existing = {row.product_id: row for row in locked.scalars().all()}

            # Only now — with the locks already held — compute the true
            # per-product totals from the append-only source of truth.
            summed = await session.execute(
                select(StockMove.product_id, func.sum(StockMove.qty_delta))
                .where(StockMove.company_id == company_id)
                .group_by(StockMove.product_id)
            )
            totals: dict[uuid.UUID, Decimal] = {row[0]: row[1] for row in summed.all()}

            updated = 0
            for product_id in set(existing) | set(totals):
                on_hand = totals.get(product_id, Decimal("0"))
                summary_row = existing.get(product_id)
                if summary_row is None:
                    session.add(
                        StockSummary(company_id=company_id, product_id=product_id, on_hand=on_hand)
                    )
                else:
                    summary_row.on_hand = on_hand
                updated += 1

            await session.commit()
    return updated


async def rebuild_stock_summary() -> RebuildSummary:
    """Rebuild every company's `stock_summary`, one company-transaction at a time."""
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
        "rebuild_stock_summary: this command takes `SELECT ... FOR UPDATE` locks on "
        "every stock_summary row for each company in turn, one company-transaction at "
        "a time. Concurrent shipments/adjustments for a company currently being "
        "rebuilt will BLOCK until that company's rebuild transaction commits. Run this "
        "during an offline maintenance window, not against a live, high-write "
        "production database."
    )
    result = await rebuild_stock_summary()
    print(
        f"rebuild summary: companies={result.companies} products_updated={result.products_updated}"
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
