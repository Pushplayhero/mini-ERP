"""ADR-005 R4 — period-close vs. in-flight posting race.

Simulation approach (documented per the brief's request): this drives two
independent `AsyncSession`s, each backed by its own real connection out of
the same Postgres connection pool, and calls the exact same service
functions (`service._resolve_and_lock_period`, `service.close_period`) a
real request would call — no mocking of locks or SQL. Concurrency is
achieved with two `asyncio.Task`s plus `asyncio.Event`s to control the
interleaving deterministically:

1. "posting" task takes the `FOR SHARE` lock on the period row (exactly
   what `create_journal_entry` does before it inserts anything), signals it
   has the lock, then *waits* before committing — standing in for "the rest
   of the posting transaction is still in flight".
2. "close" task starts only after the posting task confirms it holds the
   lock, and immediately attempts `service.close_period` (`FOR UPDATE` on
   the same row).
3. The test asserts `close` is still *not done* after a short grace period
   — proving `FOR UPDATE` is genuinely blocked by the held `FOR SHARE`, not
   racing past it — then releases the posting task, and asserts `close`
   only completes *after* posting commits.

Limitation: this is single-process, two-task concurrency within one Python
process (both sessions share the process's event loop), not two OS
processes. That's an accepted simplification — the locking guarantee being
tested is enforced entirely by Postgres itself (row-level `FOR SHARE`/`FOR
UPDATE`), which does not care whether the two blocking sessions live in the
same process; the test still exercises real, unmocked Postgres lock
contention across two genuinely independent connections/transactions, which
is the part of R4 that actually needed proving.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.exceptions import PeriodNotOpenError
from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from tests.ledger._helpers import create_company, create_period


@pytest.mark.asyncio
async def test_close_period_blocks_until_inflight_posting_commits(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    company_id = await create_company(client, "RACEC1")
    period_json = await create_period(client, company_id, 2026, 1)
    period_id = period_json["id"]
    entry_date = date(2026, 1, 15)

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    posting_locked = asyncio.Event()
    release_posting = asyncio.Event()
    order: list[str] = []

    async def posting_task() -> None:
        async with session_factory() as session:
            with company_context(company_id):
                # Same call `create_journal_entry` makes before inserting anything.
                await ledger_service._resolve_and_lock_period(session, company_id, entry_date)
                posting_locked.set()
                await release_posting.wait()
                await session.commit()  # releases the FOR SHARE lock
        order.append("posting_committed")

    async def close_task() -> None:
        await posting_locked.wait()
        async with session_factory() as session:
            with company_context(company_id):
                await ledger_service.close_period(session, period_id)
        order.append("close_completed")

    posting = asyncio.create_task(posting_task())
    await posting_locked.wait()
    closing = asyncio.create_task(close_task())

    # Give close_task every chance to (wrongly) race past the lock.
    await asyncio.sleep(0.3)
    assert not closing.done(), (
        "close_period's FOR UPDATE did not block on the posting transaction's "
        "FOR SHARE — R4's core guarantee is violated"
    )

    release_posting.set()
    await asyncio.gather(posting, closing)

    assert order == [
        "posting_committed",
        "close_completed",
    ], "close_period completed before the in-flight posting transaction committed"

    # Round out R4: now that the period is closed, a new posting attempt
    # against it must be rejected — no entry can land in a period that was
    # closed by the time its transaction committed.
    async with session_factory() as session:
        with company_context(company_id):
            with pytest.raises(PeriodNotOpenError):
                await ledger_service._resolve_and_lock_period(session, company_id, entry_date)
