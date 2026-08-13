"""tests/core/test_events.py — `app.core.events` (ADR-004).

Each test registers its own uniquely-named `core_events_test.*` event_type
so tests never collide with each other or with the real
`ledger.posting.SYNTHETIC_SALE_EVENT_TYPE` registration that happens once,
at import time, in `app.main` (triggered here via the `client` fixture).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.tenancy import company_context
from app.modules.masterdata.models import Currency, OutboxEvent
from tests.ledger._helpers import create_company


class _ValidPayload(BaseModel):
    company_id: uuid.UUID
    amount: Decimal


def test_register_event_requires_company_id_field() -> None:
    class MissingCompanyId(BaseModel):
        amount: Decimal

    with pytest.raises(events.EventSchemaError):
        events.register_event("core_events_test.missing_company_id", MissingCompanyId)

    assert "core_events_test.missing_company_id" not in events._schemas


def test_register_event_rejects_wrong_company_id_type() -> None:
    class WrongType(BaseModel):
        company_id: str
        amount: Decimal

    with pytest.raises(events.EventSchemaError):
        events.register_event("core_events_test.wrong_company_id_type", WrongType)


@pytest.mark.asyncio
async def test_publish_unregistered_event_type_raises(db_session: AsyncSession) -> None:
    with pytest.raises(events.UnknownEventTypeError):
        await events.publish(db_session, "core_events_test.never_registered", {})


@pytest.mark.asyncio
async def test_publish_invalid_payload_raises(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    events.register_event("core_events_test.schema_check", _ValidPayload)
    company_id = await create_company(client, "EVTSCH")

    with company_context(company_id), pytest.raises(events.EventPayloadValidationError):
        await events.publish(
            db_session, "core_events_test.schema_check", {"company_id": str(company_id)}
        )


@pytest.mark.asyncio
async def test_publish_company_mismatch_raises(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    events.register_event("core_events_test.mismatch", _ValidPayload)
    company_a = await create_company(client, "EVTA")
    company_b = await create_company(client, "EVTB")

    with company_context(company_a), pytest.raises(events.EventCompanyMismatchError):
        await events.publish(
            db_session,
            "core_events_test.mismatch",
            {"company_id": str(company_b), "amount": "1"},
        )


@pytest.mark.asyncio
async def test_publish_writes_outbox_row_in_same_transaction(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    events.register_event("core_events_test.outbox_write", _ValidPayload)
    company_id = await create_company(client, "EVTOUT")

    with company_context(company_id):
        await events.publish(
            db_session,
            "core_events_test.outbox_write",
            {"company_id": str(company_id), "amount": "42.5"},
        )
    await db_session.commit()

    result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.event_type == "core_events_test.outbox_write")
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["company_id"] == str(company_id)
    assert rows[0].payload["amount"] == "42.5"
    assert rows[0].dispatched_at is None


@pytest.mark.asyncio
async def test_publish_handler_exception_aborts_callers_transaction(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A handler exception must abort the *whole* caller transaction — both

    the outbox write `publish` just made and any other business write the
    caller made earlier in the same session/transaction.
    """
    events.register_event("core_events_test.handler_fails", _ValidPayload)
    company_id = await create_company(client, "EVTFAIL")

    async def _boom(_session: AsyncSession, _payload: BaseModel) -> None:
        raise RuntimeError("handler exploded")

    events.subscribe("core_events_test.handler_fails", _boom)

    with company_context(company_id):
        # A business write in the *same* session/transaction as the publish
        # call below — this is what proves the abort is transactional, not
        # just "the outbox insert didn't happen".
        db_session.add(Currency(code="ZZZ", name="Test currency", decimal_places=2, is_active=True))

        with pytest.raises(RuntimeError, match="handler exploded"):
            await events.publish(
                db_session,
                "core_events_test.handler_fails",
                {"company_id": str(company_id), "amount": "1"},
            )
        await db_session.rollback()

    currency_result = await db_session.execute(select(Currency).where(Currency.code == "ZZZ"))
    assert currency_result.scalar_one_or_none() is None

    outbox_result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.event_type == "core_events_test.handler_fails")
    )
    assert outbox_result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_publish_calls_handlers_in_subscription_order(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    events.register_event("core_events_test.ordering", _ValidPayload)
    company_id = await create_company(client, "EVTORD")

    calls: list[str] = []

    async def _first(_session: AsyncSession, _payload: BaseModel) -> None:
        calls.append("first")

    async def _second(_session: AsyncSession, _payload: BaseModel) -> None:
        calls.append("second")

    events.subscribe("core_events_test.ordering", _first)
    events.subscribe("core_events_test.ordering", _second)

    with company_context(company_id):
        await events.publish(
            db_session, "core_events_test.ordering", {"company_id": str(company_id), "amount": "1"}
        )
    await db_session.rollback()

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_redispatch_does_not_write_outbox(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """ADR-004 R1: `redispatch` (the replay CLI's entry point) must never touch `outbox`."""
    events.register_event("core_events_test.redispatch", _ValidPayload)
    company_id = await create_company(client, "EVTREDIS")

    calls: list[uuid.UUID] = []

    async def _record(_session: AsyncSession, payload: BaseModel) -> None:
        calls.append(payload.company_id)  # type: ignore[attr-defined]

    events.subscribe("core_events_test.redispatch", _record)

    with company_context(company_id):
        await events.redispatch(
            db_session,
            "core_events_test.redispatch",
            {"company_id": str(company_id), "amount": "1"},
        )
    await db_session.commit()

    assert calls == [company_id]
    result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.event_type == "core_events_test.redispatch")
    )
    assert result.scalar_one_or_none() is None
