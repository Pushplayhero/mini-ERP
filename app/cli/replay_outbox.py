"""Replay CLI for the outbox (ADR-004 R1/R3, master-plan §10.4).

Reads every `outbox` row with `dispatched_at IS NULL`, oldest (`occurred_at`)
first, and re-dispatches each one directly to its registered handler(s) —
`app.core.events.redispatch`, **never** `app.core.events.publish`, which
would re-insert an `outbox` row and make a replay run duplicate the very
queue it is draining (ADR-004 R1).

Each row gets its own session/transaction and its own bound tenancy context
(`app.core.tenancy.company_context`, keyed off the row's own
`payload["company_id"]` — ADR-004 R2/R3). This is precisely what lets replay
run correctly with no HTTP request anywhere in sight: nothing here depends
on `TenancyMiddleware` or any other request-scoped state. A row that fails
is logged, rolled back, and skipped — it does not block the rows after it
(ADR-004 R3, "one poison row aborts only itself").

Idempotency of the *effect* (not just "ran without error") comes from
ADR-003's posting engine: re-processing a row whose event already posted
hits `uq_journal_entries_source` and no-ops inside its own `SAVEPOINT`, so
replaying an already-successfully-replayed row (or two outbox rows that
represent the same at-least-once duplicate delivery) is always safe to
re-run.

Entry point: `uv run python -m app.cli.replay_outbox` (see README "Replay
the outbox").
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import column as sa_column
from sqlalchemy import func, select, update
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

# Side effect only: importing `app.main` runs its module-level
# `register_event`/`subscribe` calls, so the replay process ends up with the
# exact same event_type -> schema/handler wiring as the live API process,
# with zero duplicated registration logic. Python caches the import, so this
# is a no-op if `app.main` was already imported in-process (e.g. under the
# test suite).
from app import main as _app_main  # noqa: F401
from app.core import events
from app.core.db import dispose_engine, get_session_factory
from app.core.tenancy import company_context

logger = logging.getLogger("app.cli.replay_outbox")

# Lightweight Core references (same pattern as `ledger.service._ACCOUNTS`/
# `ledger.posting._ACCOUNTS`) used only to tell "this replay did real work"
# apart from "skipped, already fully processed" for the summary line below.
#
# Week 3 simplification, revisited (diff-review fix): the original version
# of this counted `journal_entries` alone, on the documented assumption that
# there was exactly one bus consumer (posting) to check. Week 5 made that
# assumption false — `sales.goods_shipped` now has a second subscriber
# (inventory's stock deduction), and ADR-007's zero-cost skip means a
# genuinely successful dispatch can move stock *without* ever posting a
# journal entry (all products on the shipment have `standard_cost=0`).
# Counting `journal_entries` alone would misclassify that replay — and the
# mirror case, where the journal entry already exists but a stock move was
# missing and just got repaired — as "skipped" even though real,
# once-only work happened. Summing across every side-effect table a
# replayed event's known consumers can write to, keyed by the same
# `(company_id, source_type=event_type, source_id)` triple
# `uq_journal_entries_source`/`uq_stock_moves_source` both already use as
# their own idempotency key, fixes both cases without needing to know which
# specific consumer did the work.
#
# Still not fully generic — a future third consumer with its own
# source-keyed side-effect table needs adding to `_SOURCE_KEYED_TABLES`
# below, same as this one did. Deliberately additive, not a redesign.
_JOURNAL_ENTRIES = sa_table(
    "journal_entries",
    sa_column("company_id", PGUUID(as_uuid=True)),
    sa_column("source_type"),
    sa_column("source_id", PGUUID(as_uuid=True)),
)
_STOCK_MOVES = sa_table(
    "stock_moves",
    sa_column("company_id", PGUUID(as_uuid=True)),
    sa_column("source_type"),
    sa_column("source_id", PGUUID(as_uuid=True)),
)
_SOURCE_KEYED_TABLES = (_JOURNAL_ENTRIES, _STOCK_MOVES)


@dataclass
class ReplaySummary:
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + self.skipped + self.failed


async def _count_effects(
    session: AsyncSession, company_id: uuid.UUID, event_type: str, source_id: object
) -> int | None:
    """Count source-keyed rows already recorded across every known
    side-effect table (`_SOURCE_KEYED_TABLES`) for this `(company, event,
    source_id)`. See that constant's comment for why this sums across
    tables rather than checking `journal_entries` alone.

    Returns `None` ("cannot classify") when the payload has no `source_id`
    to key off — the caller then defaults the row to "succeeded" rather
    than guessing.
    """
    if source_id is None:
        return None
    try:
        source_uuid = uuid.UUID(str(source_id))
    except ValueError:
        return None
    total = 0
    for table in _SOURCE_KEYED_TABLES:
        result = await session.execute(
            select(func.count())
            .select_from(table)
            .where(
                table.c.company_id == company_id,
                table.c.source_type == event_type,
                table.c.source_id == source_uuid,
            )
        )
        total += int(result.scalar_one())
    return total


async def replay_outbox() -> ReplaySummary:
    """Drain every un-dispatched `outbox` row once. Returns a succeeded/skipped/failed summary."""
    summary = ReplaySummary()
    session_factory = get_session_factory()

    async with session_factory() as read_session:
        result = await read_session.execute(
            select(
                events.OUTBOX_TABLE.c.id,
                events.OUTBOX_TABLE.c.event_type,
                events.OUTBOX_TABLE.c.payload,
            )
            .where(events.OUTBOX_TABLE.c.dispatched_at.is_(None))
            .order_by(events.OUTBOX_TABLE.c.occurred_at)
        )
        rows = result.all()

    for row in rows:
        # Diff-review fix (ADR-004 R3, poison-row isolation): `dict(row.payload)`
        # used to run *before* this try/except, so a NULL, scalar, or
        # otherwise malformed JSONB payload (`dict(None)` -> TypeError,
        # `dict("x")` -> ValueError, ...) raised outside any per-row
        # handling and aborted the whole `replay_outbox()` call, taking
        # every row after the poisoned one down with it — exactly the
        # failure mode R3 exists to prevent. Both the `dict()` conversion
        # and the `company_id` lookup are now inside the same guarded block.
        try:
            payload = dict(row.payload)
            company_id = uuid.UUID(str(payload["company_id"]))
        except (TypeError, KeyError, ValueError) as exc:
            logger.error("outbox row %s: payload missing/invalid company_id: %s", row.id, exc)
            summary.failed += 1
            continue

        async with session_factory() as session:
            try:
                source_id = payload.get("source_id")
                with company_context(company_id):
                    before = await _count_effects(session, company_id, row.event_type, source_id)
                    await events.redispatch(session, row.event_type, payload)
                    after = await _count_effects(session, company_id, row.event_type, source_id)
                await session.execute(
                    update(events.OUTBOX_TABLE)
                    .where(events.OUTBOX_TABLE.c.id == row.id)
                    .values(dispatched_at=func.now())
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "outbox row %s (event_type=%s) failed to replay", row.id, row.event_type
                )
                summary.failed += 1
                continue

        if before is None or after is None or after > before:
            summary.succeeded += 1
        else:
            summary.skipped += 1

    return summary


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    summary = await replay_outbox()
    print(
        f"replay summary: succeeded={summary.succeeded} skipped={summary.skipped} "
        f"failed={summary.failed} total={summary.total}"
    )
    await dispose_engine()
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
