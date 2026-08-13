"""tests/ledger/test_posting.py — the posting engine (ADR-003) + replay (ADR-004 R1).

Every test here exercises the Week 3 synthetic self-validation event
(`posting.SYNTHETIC_SALE_EVENT_TYPE`) — see `app/modules/ledger/posting.py`'s
module docstring for why. All tests take the `client` fixture even where
they never issue an HTTP call themselves: importing `app.main` (which the
`client` fixture does) is what registers the synthetic event schema and
subscribes the posting handler onto the bus (`app/main.py`), and every test
here also uses `client` to create companies/accounts/periods through the
real HTTP API, matching how these resources would actually be created.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import replay_outbox
from app.core import events
from app.core.tenancy import company_context, current_company_id_or_none
from app.modules.ledger import posting, service
from app.modules.masterdata.models import OutboxEvent
from tests.ledger._helpers import create_account, create_company, create_period


async def _setup_company_with_accounts(
    client: AsyncClient, code: str, *, with_accounts: bool = True
) -> uuid.UUID:
    company_id = await create_company(client, code)
    await create_period(client, company_id, date.today().year, date.today().month)
    if with_accounts:
        await create_account(client, company_id, "1100", "Accounts Receivable", "asset")
        await create_account(client, company_id, "4000", "Revenue", "revenue")
    return company_id


def _synthetic_payload(
    company_id: uuid.UUID, source_id: uuid.UUID, amount: str
) -> dict[str, object]:
    return {"company_id": str(company_id), "source_id": str(source_id), "amount": amount}


@pytest.mark.asyncio
async def test_synthetic_event_posts_balanced_entry(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    company_id = await _setup_company_with_accounts(client, "POST1")
    source_id = uuid.uuid4()

    with company_context(company_id):
        await events.publish(
            db_session,
            posting.SYNTHETIC_SALE_EVENT_TYPE,
            _synthetic_payload(company_id, source_id, "1500.50"),
        )
        await db_session.commit()

        entries = await service.list_journal_entries(db_session)

    matching = [e for e in entries if e.source_id == source_id]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.source_type == posting.SYNTHETIC_SALE_EVENT_TYPE
    assert len(entry.lines) == 2

    total_debit = sum((line.debit for line in entry.lines), Decimal("0"))
    total_credit = sum((line.credit for line in entry.lines), Decimal("0"))
    assert total_debit == total_credit == Decimal("1500.500000")


@pytest.mark.asyncio
async def test_missing_posting_rule_aborts(db_session: AsyncSession, client: AsyncClient) -> None:
    company_id = await _setup_company_with_accounts(client, "POST2")
    payload = posting.SyntheticSalePayload(
        company_id=company_id, source_id=uuid.uuid4(), amount=Decimal("10")
    )

    with company_context(company_id):
        with pytest.raises(posting.PostingRuleNotFoundError):
            await posting.handle_posting_event(db_session, "no.such.event.type", payload)

        entries = await service.list_journal_entries(db_session)
    assert entries == []


@pytest.mark.asyncio
async def test_missing_account_code_aborts_without_partial_write(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # No accounts created for this company at all — resolution must fail.
    company_id = await _setup_company_with_accounts(client, "POST3", with_accounts=False)
    source_id = uuid.uuid4()

    with company_context(company_id):
        with pytest.raises(posting.AccountResolutionError):
            await events.publish(
                db_session,
                posting.SYNTHETIC_SALE_EVENT_TYPE,
                _synthetic_payload(company_id, source_id, "100"),
            )
        await db_session.rollback()

        entries = await service.list_journal_entries(db_session)
    assert entries == []


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    company_id = await _setup_company_with_accounts(client, "POST4")
    source_id = uuid.uuid4()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    for _ in range(2):
        async with session_factory() as session:
            with company_context(company_id):
                # Must not raise on either delivery — the second is a
                # no-op skip, not an error (ADR-003 R2).
                await events.publish(
                    session,
                    posting.SYNTHETIC_SALE_EVENT_TYPE,
                    _synthetic_payload(company_id, source_id, "777"),
                )
            await session.commit()

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
    matching = [e for e in entries if e.source_id == source_id]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_replay_deduplicates_and_marks_dispatched(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    company_id = await _setup_company_with_accounts(client, "POST5")
    source_id = uuid.uuid4()
    payload = _synthetic_payload(company_id, source_id, "250")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Simulate two at-least-once duplicate outbox rows for the same event.
    async with session_factory() as session:
        for _ in range(2):
            session.add(OutboxEvent(event_type=posting.SYNTHETIC_SALE_EVENT_TYPE, payload=payload))
        await session.commit()

    summary_1 = await replay_outbox.replay_outbox()
    summary_2 = await replay_outbox.replay_outbox()

    assert summary_1.failed == 0
    assert summary_1.succeeded + summary_1.skipped == 2
    assert summary_2.total == 0  # nothing left un-dispatched

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
        matching_entries = [e for e in entries if e.source_id == source_id]
        assert len(matching_entries) == 1

        outbox_result = await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == posting.SYNTHETIC_SALE_EVENT_TYPE,
                OutboxEvent.payload["source_id"].astext == str(source_id),
            )
        )
        outbox_rows = outbox_result.scalars().all()
        assert len(outbox_rows) == 2
        assert all(row.dispatched_at is not None for row in outbox_rows)


@pytest.mark.asyncio
async def test_replay_works_without_ambient_tenancy_context(
    db_engine: AsyncEngine, client: AsyncClient
) -> None:
    """The whole point of replay: it must work with zero HTTP request in sight.

    If this test fails, the replay CLI is unusable outside a request context
    — see ADR-004 R2/R3.
    """
    company_id = await _setup_company_with_accounts(client, "POST6")
    source_id = uuid.uuid4()
    payload = _synthetic_payload(company_id, source_id, "999")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(OutboxEvent(event_type=posting.SYNTHETIC_SALE_EVENT_TYPE, payload=payload))
        await session.commit()

    # No `with company_context(...)` anywhere around this call — proves
    # replay binds its own context per row instead of relying on ambient
    # request state.
    assert current_company_id_or_none() is None
    summary = await replay_outbox.replay_outbox()
    assert current_company_id_or_none() is None  # context is unbound again afterwards

    assert summary.failed == 0
    assert summary.succeeded == 1

    async with session_factory() as session:
        with company_context(company_id):
            entries = await service.list_journal_entries(session)
    matching_entries = [e for e in entries if e.source_id == source_id]
    assert len(matching_entries) == 1
