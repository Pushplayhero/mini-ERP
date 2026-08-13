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

# Lightweight Core reference to `journal_entries` (same pattern as
# `ledger.service._ACCOUNTS`/`ledger.posting._ACCOUNTS`) used only to tell
# "posted a new entry" apart from "skipped, already posted" for the summary
# line below. Week 3 simplification, documented in the completion report:
# there is exactly one bus consumer (posting) this week, so this heuristic
# does not need to be generic yet — it will need revisiting if/when a
# non-posting consumer is ever replayed.
_JOURNAL_ENTRIES = sa_table(
    "journal_entries",
    sa_column("company_id", PGUUID(as_uuid=True)),
    sa_column("source_type"),
    sa_column("source_id", PGUUID(as_uuid=True)),
)


@dataclass
class ReplaySummary:
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + self.skipped + self.failed


async def _count_posted(
    session: AsyncSession, company_id: uuid.UUID, event_type: str, source_id: object
) -> int | None:
    """Count journal entries already posted for this `(company, event, source_id)`.

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
    result = await session.execute(
        select(func.count())
        .select_from(_JOURNAL_ENTRIES)
        .where(
            _JOURNAL_ENTRIES.c.company_id == company_id,
            _JOURNAL_ENTRIES.c.source_type == event_type,
            _JOURNAL_ENTRIES.c.source_id == source_uuid,
        )
    )
    return int(result.scalar_one())


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
        payload = dict(row.payload)
        try:
            company_id = uuid.UUID(str(payload["company_id"]))
        except (KeyError, ValueError) as exc:
            logger.error("outbox row %s: payload missing/invalid company_id: %s", row.id, exc)
            summary.failed += 1
            continue

        async with session_factory() as session:
            try:
                source_id = payload.get("source_id")
                with company_context(company_id):
                    before = await _count_posted(session, company_id, row.event_type, source_id)
                    await events.redispatch(session, row.event_type, payload)
                    after = await _count_posted(session, company_id, row.event_type, source_id)
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
