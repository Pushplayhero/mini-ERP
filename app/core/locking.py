"""Per-company Postgres advisory locks — a coordination primitive for the
narrow case where row-level `SELECT ... FOR UPDATE` isn't enough because the
row in question might not exist yet.

Background (diff-review fix on top of ADR-007 R2): `stock_summary`'s
"lock every existing row, then recompute" rebuild discipline only protects
rows that already exist at lock time. A row that has moves but no summary
row yet (deleted by an operator, or never created — see
`app.cli.rebuild_stock_summary`'s reconciliation tests) has nothing for
`SELECT ... FOR UPDATE` to lock, so a normal writer creating that exact row
concurrently with a rebuild can still race it. Advisory locks close that
gap because they're keyed by an application-chosen id (here, `company_id`),
not by a row that has to already exist.

Usage: every normal writer (`inventory.service.get_or_lock_summary`) takes
the SHARED lock before touching any `stock_summary` row for its company;
`rebuild_stock_summary` takes the EXCLUSIVE lock for the whole rebuild.
Postgres serializes shared-vs-exclusive the way a read/write lock would:
any number of writers can hold the shared lock concurrently (they still
serialize on each other via the normal row-level `FOR UPDATE`), but the
exclusive holder waits for all of them to finish, and — while it holds the
lock — blocks every new writer from proceeding past its own shared-lock
acquisition. That is exactly the ordering rebuild's own correctness
argument already assumes; this just makes row *creation* (not only row
*contents*) subject to it too.

Transaction-scoped (`pg_advisory_xact_lock*`): released automatically at
commit or rollback, so it composes with the flush-only-core /
committing-wrapper doctrine (ADR-003 R1) without callers needing to release
anything themselves.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def acquire_company_advisory_lock(
    session: AsyncSession, company_id: uuid.UUID, *, exclusive: bool
) -> None:
    """Acquire a transaction-scoped advisory lock keyed by `company_id`.

    `hashtextextended(text, seed)` (Postgres >= 11) maps the company id to a
    stable `bigint` lock key; the `0` seed is arbitrary but must stay fixed
    forever (changing it would silently stop new callers from contending
    with locks taken under the old seed). `fn` is chosen from a fixed
    two-value set, never from caller input, so building the statement string
    from it is not a SQL-injection concern.
    """
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {fn}(hashtextextended(:company_id, 0))"),
        {"company_id": str(company_id)},
    )
