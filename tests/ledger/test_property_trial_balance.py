"""Hypothesis property test — ADR-005 Action Item 4 / master-plan §10:

"random valid entries ⇒ trial balance always balances".

This drives `app.modules.ledger.service` and `app.modules.masterdata.service`
directly (not the HTTP layer) with a hand-rolled `AsyncEngine` per example,
rather than reusing the `client`/`db_engine` fixtures. Reason: Hypothesis's
`@given` wraps a *synchronous* function, and each example needs its own
`asyncio.run(...)` call; the `db_engine` fixture's engine (and the asyncpg
connections in its pool) are bound to pytest-asyncio's per-test event loop,
which is a different loop than the one each `asyncio.run()` call creates —
reusing that engine here would hit exactly the cross-loop asyncpg failure
mode `tests/conftest.py`'s module docstring warns about. Creating a fresh
`AsyncEngine` for the DSN inside every `asyncio.run()` call (and disposing
it before returning) keeps each example's engine lifecycle inside a single
event loop, which is safe.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from app.modules.ledger.schemas import AccountingPeriodCreate, JournalEntryCreate, JournalLineCreate
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import AccountCreate, CompanyCreate

ENTRY_DATE = date(2026, 1, 15)

# Cents, so we can build exact Decimal amounts without float rounding noise.
cent_amounts = st.integers(min_value=1, max_value=1_000_000)


async def _setup_company(dsn: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = create_async_engine(dsn)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            company = await masterdata_service.create_company(
                session,
                CompanyCreate(
                    code=f"HYP{uuid.uuid4().hex[:10].upper()}",
                    name="Hypothesis Co",
                    functional_currency_code="TWD",
                ),
            )
        with company_context(company.id):
            async with session_factory() as session:
                debit_account = await masterdata_service.create_account(
                    session, AccountCreate(code="1000", name="Cash", type="asset")
                )
            async with session_factory() as session:
                credit_account = await masterdata_service.create_account(
                    session, AccountCreate(code="4000", name="Revenue", type="revenue")
                )
            async with session_factory() as session:
                await ledger_service.create_period(
                    session, AccountingPeriodCreate(year=ENTRY_DATE.year, month=ENTRY_DATE.month)
                )
        return company.id, debit_account.id, credit_account.id
    finally:
        await engine.dispose()


async def _post_entries_and_check_balance(
    dsn: str,
    company_id: uuid.UUID,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    cents: list[int],
) -> tuple[Decimal, Decimal]:
    """Post one entry per amount in `cents`, then assert the trial balance —

    which reflects *all* entries ever posted for this company across every
    Hypothesis example so far, not just this call's `cents` — still
    balances. Returns this call's own posted total so the caller can track
    a running expected total across examples (the company/accounts are
    created once and reused for the whole Hypothesis run, not per example).
    """
    engine = create_async_engine(dsn)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        with company_context(company_id):
            posted_total = Decimal("0")
            for amount_cents in cents:
                amount = (Decimal(amount_cents) / Decimal(100)).quantize(Decimal("0.01"))
                lines = [
                    JournalLineCreate(
                        account_id=debit_account_id,
                        currency_code="TWD",
                        txn_debit=amount,
                        debit=amount,
                        exchange_rate=Decimal("1"),
                    ),
                    JournalLineCreate(
                        account_id=credit_account_id,
                        currency_code="TWD",
                        txn_credit=amount,
                        credit=amount,
                        exchange_rate=Decimal("1"),
                    ),
                ]
                async with session_factory() as session:
                    await ledger_service.create_journal_entry(
                        session, JournalEntryCreate(entry_date=ENTRY_DATE, lines=lines)
                    )
                posted_total += amount

            async with session_factory() as session:
                trial_balance = await ledger_service.get_trial_balance(session)

            total_debit = sum((line.total_debit for line in trial_balance), Decimal("0"))
            total_credit = sum((line.total_credit for line in trial_balance), Decimal("0"))
            assert (
                total_debit == total_credit
            ), f"trial balance does not balance: debit={total_debit} credit={total_credit}"
            return posted_total, total_debit
    finally:
        await engine.dispose()


def test_hypothesis_random_balanced_entries_keep_trial_balance_balanced(
    postgres_dsn: str,
) -> None:
    """`postgres_dsn` (session-scoped) guarantees the schema is migrated and

    the DSN is exported before this runs; everything else is self-contained
    per the module docstring above.

    Deliberately a plain (non-`async def`) test function: `asyncio.run()`
    refuses to start a new event loop while one is already running, so if
    this test itself were a coroutine under pytest-asyncio's per-test loop,
    every `asyncio.run()` call below would raise `RuntimeError`. Staying
    fully synchronous here means each `asyncio.run()` call (in `_setup_company`
    and once per Hypothesis example) owns a brand-new loop end to end, which
    is exactly the isolation this test needs (see module docstring).
    pytest-asyncio still resolves the autouse async fixtures this test
    transitively depends on (`db_engine` et al., for schema/reference-data
    setup) in their own loop before this function body ever runs — that is
    standard, supported pytest-asyncio behaviour for sync tests with async
    fixtures.
    """
    company_id, debit_account_id, credit_account_id = asyncio.run(_setup_company(postgres_dsn))
    cumulative_expected = Decimal("0")

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(cents=st.lists(cent_amounts, min_size=1, max_size=4))
    def run_example(cents: list[int]) -> None:
        nonlocal cumulative_expected
        posted_total, observed_total_debit = asyncio.run(
            _post_entries_and_check_balance(
                postgres_dsn, company_id, debit_account_id, credit_account_id, cents
            )
        )
        cumulative_expected += posted_total
        assert observed_total_debit == cumulative_expected, (
            "trial balance total debit does not match the running total of everything "
            "posted so far across this Hypothesis run"
        )

    run_example()
