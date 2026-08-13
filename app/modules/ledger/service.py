"""ledger service layer — business logic + transaction boundary (ADR-005).

Routers stay thin, same as masterdata. Every write to a tenant-scoped table
stamps `company_id` from `app.core.tenancy.require_current_company_id()`,
never from the DTO — see `app.modules.ledger.schemas` module docstring.

Independence-boundary note (import-linter): this module must never import
`app.modules.masterdata`. Two places would ordinarily want to — validating
that a line's `account_id` belongs to the caller's company, and joining
`accounts` for the trial balance report's code/name/type columns — so both
use a lightweight `sqlalchemy.table()`/`column()` reference to the physical
`accounts` table instead of importing `masterdata.models.Account`. This is a
Core-level reference to shared schema (fine in a modular monolith with one
database), not a Python import across the module boundary, so it keeps the
independence contract green. See the Week 2 completion report point 4 for
the full trade-off discussion, including why the DB foreign key alone is
*not* sufficient here (it proves the account exists, not that it belongs to
the posting company).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import column as sa_column
from sqlalchemy import func, select
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    PeriodNotOpenError,
    ReversalNotAllowedError,
)
from app.core.tenancy import require_current_company_id
from app.modules.ledger.models import (
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    LedgerSequence,
    PeriodStatus,
)
from app.modules.ledger.schemas import (
    AccountingPeriodCreate,
    JournalEntryCreate,
    JournalLineCreate,
    TrialBalanceLine,
)

# Phase 1 hard constraint (ADR-005 R1 / master-plan §10.1): "Phase 1 is
# TWD-only in practice". Rather than looking up each company's
# `functional_currency_code` (which would require importing
# `masterdata.models.Company`, crossing the independence boundary for a
# lookup Week 2 does not otherwise need), the service simply hardcodes the
# one currency Phase 1 supports and rejects everything else with a clear
# 422. Revisit when real multi-currency posting lands (likely via a
# published company-currency snapshot rather than a live cross-module
# import) — see completion report point 4.
FUNCTIONAL_CURRENCY = "TWD"

_ACCOUNTS = sa_table(
    "accounts",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("code"),
    sa_column("name"),
    sa_column("type"),
)


async def _commit_or_conflict(session: AsyncSession, *, entity: str) -> None:
    """Same choke point as `masterdata.service._commit_or_conflict`.

    Also catches the deferred balance constraint trigger (ADR-005 Decision
    2 / R1): Postgres raises it with `ERRCODE 23514` (`check_violation`),
    which SQLAlchemy/asyncpg classify as `IntegrityError` — so an unbalanced
    entry that somehow slipped past `_validate_lines` (defense in depth)
    surfaces as a 409 here too, not a raw 500.
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{entity} violates a uniqueness or reference constraint") from exc


# ---------------------------------------------------------------------------
# Line validation (fast fail, before anything touches the DB)
# ---------------------------------------------------------------------------


def _validate_lines(lines: Sequence[JournalLineCreate]) -> None:
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for idx, line in enumerate(lines, start=1):
        if line.currency_code != FUNCTIONAL_CURRENCY:
            raise DomainValidationError(
                f"line {idx}: currency_code {line.currency_code!r} is not supported in Phase 1 "
                f"(only the functional currency {FUNCTIONAL_CURRENCY!r} is accepted — real "
                "multi-currency conversion is out of scope for Week 2, see ADR-005 R1)"
            )
        if line.txn_debit < 0 or line.txn_credit < 0 or line.debit < 0 or line.credit < 0:
            raise DomainValidationError(f"line {idx}: amounts must be >= 0")
        if line.txn_debit > 0 and line.txn_credit > 0:
            raise DomainValidationError(
                f"line {idx}: cannot have both txn_debit and txn_credit set"
            )
        if line.debit > 0 and line.credit > 0:
            raise DomainValidationError(f"line {idx}: cannot have both debit and credit set")
        if line.debit == 0 and line.credit == 0:
            raise DomainValidationError(f"line {idx}: must have a nonzero debit or credit")
        if (line.debit > 0) != (line.txn_debit > 0) or (line.credit > 0) != (line.txn_credit > 0):
            raise DomainValidationError(
                f"line {idx}: transaction-currency and functional-currency sides must agree"
            )
        if line.exchange_rate != Decimal("1"):
            raise DomainValidationError(
                f"line {idx}: exchange_rate must be 1 in Phase 1 (TWD-only, see ADR-005 R1)"
            )
        if line.debit != line.txn_debit or line.credit != line.txn_credit:
            raise DomainValidationError(
                f"line {idx}: txn amounts must equal functional amounts when currency_code is "
                "the functional currency (no conversion logic in Phase 1)"
            )
        total_debit += line.debit
        total_credit += line.credit

    if total_debit != total_credit:
        raise DomainValidationError(
            f"entry does not balance: total debit {total_debit} != total credit {total_credit}"
        )
    if total_debit == 0:
        raise DomainValidationError("entry has no nonzero lines")


async def _ensure_accounts_belong_to_company(
    session: AsyncSession, company_id: uuid.UUID, account_ids: set[uuid.UUID]
) -> None:
    if not account_ids:
        return
    result = await session.execute(
        select(_ACCOUNTS.c.id).where(
            _ACCOUNTS.c.id.in_(account_ids), _ACCOUNTS.c.company_id == company_id
        )
    )
    found = {row[0] for row in result.all()}
    missing = account_ids - found
    if missing:
        missing_str = ", ".join(str(a) for a in sorted(missing, key=str))
        raise DomainValidationError(
            f"account_id(s) not found or do not belong to this company: {missing_str}"
        )


# ---------------------------------------------------------------------------
# Period resolution (ADR-005 R4) + gapless numbering (ADR-005 R2)
# ---------------------------------------------------------------------------


async def _resolve_and_lock_period(
    session: AsyncSession, company_id: uuid.UUID, entry_date: date
) -> AccountingPeriod:
    """`SELECT ... FOR SHARE` on the target period row (ADR-005 R4).

    Holding a `FOR SHARE` lock here means a concurrent `close_period`
    (which needs `FOR UPDATE` on the same row) cannot commit while this
    transaction is still in flight — it blocks until this transaction
    commits or rolls back. Either the close wins the race (commits before
    this call even runs, so this lookup already observes `status=closed`
    and this function raises), or this posting wins (locks first, close
    blocks until this transaction ends) — either way, no entry can land in
    a period that is closed by the time its transaction commits.
    """
    year, month = entry_date.year, entry_date.month
    result = await session.execute(
        select(AccountingPeriod)
        .where(
            AccountingPeriod.company_id == company_id,
            AccountingPeriod.year == year,
            AccountingPeriod.month == month,
        )
        .with_for_update(read=True)
    )
    period = result.scalar_one_or_none()
    if period is None:
        raise PeriodNotOpenError(year, month, reason="no accounting period exists for this month")
    if period.status is PeriodStatus.CLOSED:
        raise PeriodNotOpenError(year, month, reason="period is closed")
    return period


async def _allocate_entry_no(session: AsyncSession, company_id: uuid.UUID, year: int) -> str:
    """Gapless `entry_no` allocation (ADR-005 R2).

    `INSERT ... ON CONFLICT DO NOTHING` followed by `SELECT ... FOR UPDATE`
    is the standard race-free "get-or-create, then lock" pattern: two
    transactions racing to allocate the first number of a company's first
    entry in a given year cannot both `INSERT` successfully (the second
    hits the `(company_id, year)` primary key and no-ops instead of
    raising), and whichever one performs the following `SELECT ... FOR
    UPDATE` first blocks the other until it commits — so the counter
    increment is always serialized per `(company_id, year)`, and a rollback
    of the caller's transaction rolls the increment back too (no gaps).
    """
    await session.execute(
        pg_insert(LedgerSequence)
        .values(company_id=company_id, year=year, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )
    result = await session.execute(
        select(LedgerSequence)
        .where(LedgerSequence.company_id == company_id, LedgerSequence.year == year)
        .with_for_update()
    )
    seq = result.scalar_one()
    allocated_no = seq.next_no
    seq.next_no = allocated_no + 1
    return f"JE-{year:04d}-{allocated_no:06d}"


def _build_lines(
    company_id: uuid.UUID,
    lines: Sequence[JournalLineCreate],
    *,
    entry_date: date,
    swap: bool = False,
) -> list[JournalLine]:
    built: list[JournalLine] = []
    for idx, line in enumerate(lines, start=1):
        debit, credit = (line.credit, line.debit) if swap else (line.debit, line.credit)
        txn_debit, txn_credit = (
            (line.txn_credit, line.txn_debit) if swap else (line.txn_debit, line.txn_credit)
        )
        built.append(
            JournalLine(
                company_id=company_id,
                account_id=line.account_id,
                line_no=idx,
                currency_code=line.currency_code,
                txn_debit=txn_debit,
                txn_credit=txn_credit,
                debit=debit,
                credit=credit,
                exchange_rate=line.exchange_rate,
                rate_date=line.rate_date or entry_date,
            )
        )
    return built


# ---------------------------------------------------------------------------
# Accounting periods
# ---------------------------------------------------------------------------


async def create_period(session: AsyncSession, data: AccountingPeriodCreate) -> AccountingPeriod:
    period = AccountingPeriod(
        company_id=require_current_company_id(),
        year=data.year,
        month=data.month,
        status=PeriodStatus.OPEN,
        custom_data=data.custom_data,
    )
    session.add(period)
    await _commit_or_conflict(session, entity="AccountingPeriod")
    await session.refresh(period)
    return period


async def list_periods(session: AsyncSession) -> Sequence[AccountingPeriod]:
    result = await session.execute(
        select(AccountingPeriod).order_by(AccountingPeriod.year, AccountingPeriod.month)
    )
    return result.scalars().all()


async def get_period(session: AsyncSession, period_id: uuid.UUID) -> AccountingPeriod:
    result = await session.execute(select(AccountingPeriod).where(AccountingPeriod.id == period_id))
    period = result.scalar_one_or_none()
    if period is None:
        raise NotFoundError("AccountingPeriod", period_id)
    return period


async def close_period(session: AsyncSession, period_id: uuid.UUID) -> AccountingPeriod:
    """Close a period. `SELECT ... FOR UPDATE` is the R4 lock — see

    `_resolve_and_lock_period`'s docstring for the race analysis from the
    posting side.

    Idempotent: closing an already-closed period is a no-op returning the
    period unchanged rather than a 409. Week 2 has no reopen workflow (out
    of scope per the ADR), so there is no conflicting state transition to
    reject here — a repeat close request is simply satisfied twice.
    """
    result = await session.execute(
        select(AccountingPeriod).where(AccountingPeriod.id == period_id).with_for_update()
    )
    period = result.scalar_one_or_none()
    if period is None:
        raise NotFoundError("AccountingPeriod", period_id)
    if period.status is PeriodStatus.OPEN:
        period.status = PeriodStatus.CLOSED
        await session.commit()
        await session.refresh(period)
    return period


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------


async def create_journal_entry(session: AsyncSession, data: JournalEntryCreate) -> JournalEntry:
    company_id = require_current_company_id()

    _validate_lines(data.lines)
    await _ensure_accounts_belong_to_company(
        session, company_id, {line.account_id for line in data.lines}
    )

    period = await _resolve_and_lock_period(session, company_id, data.entry_date)
    entry_no = await _allocate_entry_no(session, company_id, data.entry_date.year)

    entry = JournalEntry(
        company_id=company_id,
        entry_no=entry_no,
        entry_date=data.entry_date,
        period_id=period.id,
        source_type=data.source_type,
        source_id=data.source_id,
        reversal_of_id=None,
        custom_data=data.custom_data,
    )
    entry.lines = _build_lines(company_id, data.lines, entry_date=data.entry_date)
    session.add(entry)
    await _commit_or_conflict(session, entity="JournalEntry")
    await session.refresh(entry, attribute_names=["lines"])
    return entry


async def get_journal_entry(session: AsyncSession, entry_id: uuid.UUID) -> JournalEntry:
    result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id)
        .options(selectinload(JournalEntry.lines))
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise NotFoundError("JournalEntry", entry_id)
    return entry


async def list_journal_entries(session: AsyncSession) -> Sequence[JournalEntry]:
    result = await session.execute(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .order_by(JournalEntry.entry_no)
    )
    return result.scalars().all()


async def reverse_journal_entry(session: AsyncSession, entry_id: uuid.UUID) -> JournalEntry:
    """ADR-005 R3: create a new entry with debit/credit swapped, linked via

    `reversal_of_id`.

    `entry_date` choice: the reversal is dated *today* (the day the
    correction is made), not backdated into the original entry's period —
    the original period may well be closed by the time someone notices the
    mistake (that is, in fact, the common real-world trigger for wanting a
    reversal in the first place), and ADR-005's period model has no reopen
    workflow. Dating the reversal "as of discovery" also matches standard
    accounting practice for correcting entries. This means reversing an
    entry requires an *open* period for today's year/month to already
    exist, same as any other posting — documented as a judgment call in the
    Week 2 completion report (the brief left this an open choice).
    """
    company_id = require_current_company_id()
    original = await get_journal_entry(session, entry_id)

    if original.reversal_of_id is not None:
        raise ReversalNotAllowedError(
            f"JournalEntry {entry_id} is itself a reversal entry and cannot be reversed"
        )

    already_reversed = await session.execute(
        select(JournalEntry.id).where(JournalEntry.reversal_of_id == original.id)
    )
    if already_reversed.scalar_one_or_none() is not None:
        raise ReversalNotAllowedError(f"JournalEntry {entry_id} has already been reversed")

    reversal_date = date.today()
    period = await _resolve_and_lock_period(session, company_id, reversal_date)
    entry_no = await _allocate_entry_no(session, company_id, reversal_date.year)

    swapped_inputs = [
        JournalLineCreate(
            account_id=line.account_id,
            currency_code=line.currency_code,
            txn_debit=line.txn_debit,
            txn_credit=line.txn_credit,
            debit=line.debit,
            credit=line.credit,
            exchange_rate=line.exchange_rate,
            rate_date=line.rate_date,
        )
        for line in original.lines
    ]

    reversal = JournalEntry(
        company_id=company_id,
        entry_no=entry_no,
        entry_date=reversal_date,
        period_id=period.id,
        source_type=original.source_type,
        source_id=original.source_id,
        reversal_of_id=original.id,
        custom_data={},
    )
    reversal.lines = _build_lines(company_id, swapped_inputs, entry_date=reversal_date, swap=True)
    session.add(reversal)
    await _commit_or_conflict(session, entity="JournalEntry (reversal)")
    await session.refresh(reversal, attribute_names=["lines"])
    return reversal


# ---------------------------------------------------------------------------
# Trial balance (ADR-005 Decision 4 — computed on the fly, no snapshot table)
# ---------------------------------------------------------------------------


async def get_trial_balance(
    session: AsyncSession, *, period_id: uuid.UUID | None = None
) -> list[TrialBalanceLine]:
    """Aggregate `journal_lines` by account, in the functional currency.

    Query parameter design (ADR-005 P3, left to implementation): supports
    exactly the two modes the brief asks for — `period_id=None` aggregates
    the company's entire journal history, `period_id=<uuid>` restricts to
    one accounting period. A date-range filter was considered and dropped
    for Week 2 scope (no stated caller needs it yet, and `period_id` already
    covers the month-end trial balance workflow that matters at Phase 1);
    revisit if/when a caller needs an arbitrary as-of-date report.

    `company_id` is filtered explicitly here (rather than relying solely on
    the automatic `do_orm_execute` tenant filter) because this query's
    FROM-clause anchor is a plain `sqlalchemy.table()` reference to
    `accounts`, not an ORM-mapped, `TenantScopedMixin`-registered class —
    see module docstring on why. `JournalEntry`/`JournalLine` are still
    ORM-mapped and tenant-scoped, so the automatic filter also applies to
    them; this explicit predicate is belt-and-suspenders for the one join
    target the automatic mechanism cannot see.
    """
    company_id = require_current_company_id()
    stmt = (
        select(
            _ACCOUNTS.c.id.label("account_id"),
            _ACCOUNTS.c.code.label("account_code"),
            _ACCOUNTS.c.name.label("account_name"),
            _ACCOUNTS.c.type.label("account_type"),
            func.coalesce(func.sum(JournalLine.debit), 0).label("total_debit"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("total_credit"),
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(_ACCOUNTS, JournalLine.account_id == _ACCOUNTS.c.id)
        .where(JournalEntry.company_id == company_id)
        .group_by(_ACCOUNTS.c.id, _ACCOUNTS.c.code, _ACCOUNTS.c.name, _ACCOUNTS.c.type)
        .order_by(_ACCOUNTS.c.code)
    )
    if period_id is not None:
        stmt = stmt.where(JournalEntry.period_id == period_id)

    result = await session.execute(stmt)
    return [
        TrialBalanceLine(
            account_id=row.account_id,
            account_code=row.account_code,
            account_name=row.account_name,
            account_type=row.account_type,
            total_debit=row.total_debit,
            total_credit=row.total_credit,
        )
        for row in result.all()
    ]
