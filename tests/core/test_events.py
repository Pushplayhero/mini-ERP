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


@pytest.fixture(autouse=True)
def _isolated_event_registry():
    """Snapshot + restore `app.core.events`' registries around every test here.

    Added alongside the new `unregister()`/`reset()` tests below (ADR-006
    R3): `reset()` wipes *every* registered event schema/subscriber
    process-wide, including `app.main`'s real, import-time-only
    registrations (`ledger_posting.SYNTHETIC_SALE_EVENT_TYPE` and its
    posting subscriber, `sales.order_confirmed`'s schema). Every pre-existing
    test in this file already avoids collisions via unique `event_type`
    naming and is unaffected by this fixture wrapping them too (save/restore
    around a test that never touches `reset()`/`unregister()` is a no-op).
    """
    saved_schemas = dict(events._schemas)
    saved_handlers = {event_type: list(fns) for event_type, fns in events._handlers.items()}
    yield
    events._schemas.clear()
    events._schemas.update(saved_schemas)
    events._handlers.clear()
    events._handlers.update(saved_handlers)


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


# ---------------------------------------------------------------------------
# unregister / reset (ADR-006 R3, added Week 4 — purely additive; none of the
# tests above were modified to make room for these)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregister_stops_handler_from_being_called(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    events.register_event("core_events_test.unregister", _ValidPayload)
    company_id = await create_company(client, "EVTUNREG")

    calls: list[str] = []

    async def handler(_session: AsyncSession, _payload: BaseModel) -> None:
        calls.append("called")

    events.subscribe("core_events_test.unregister", handler)
    events.unregister("core_events_test.unregister", handler)

    with company_context(company_id):
        await events.publish(
            db_session,
            "core_events_test.unregister",
            {"company_id": str(company_id), "amount": "1"},
        )
    await db_session.rollback()

    assert calls == []


@pytest.mark.asyncio
async def test_unregister_removes_every_occurrence_of_a_duplicate_subscription(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Diff-review regression: `unregister` used to call `list.remove()`,
    which only strips the *first* match. If `handler` had somehow been
    subscribed twice to the same event_type, one `unregister` call left it
    still active — contradicting `unregister`'s own documented "this is not
    listening" postcondition.
    """
    events.register_event("core_events_test.dup_unregister", _ValidPayload)
    company_id = await create_company(client, "EVTDUPUNREG")

    calls: list[str] = []

    async def handler(_session: AsyncSession, _payload: BaseModel) -> None:
        calls.append("called")

    events.subscribe("core_events_test.dup_unregister", handler)
    events.subscribe("core_events_test.dup_unregister", handler)
    events.unregister("core_events_test.dup_unregister", handler)

    with company_context(company_id):
        await events.publish(
            db_session,
            "core_events_test.dup_unregister",
            {"company_id": str(company_id), "amount": "1"},
        )
    await db_session.rollback()

    assert calls == []


def test_unregister_of_a_handler_never_subscribed_is_a_silent_no_op() -> None:
    async def never_subscribed(_session: AsyncSession, _payload: BaseModel) -> None:
        pass

    # Unknown event_type entirely.
    events.unregister("core_events_test.unknown_event_type", never_subscribed)

    # Known event_type, but this handler was never subscribed to it.
    events.register_event("core_events_test.discard_semantics", _ValidPayload)
    events.subscribe("core_events_test.discard_semantics", never_subscribed)
    events.unregister("core_events_test.discard_semantics", never_subscribed)
    # A second unregister of the same (now-removed) handler must also
    # silently no-op, not raise.
    events.unregister("core_events_test.discard_semantics", never_subscribed)


@pytest.mark.asyncio
async def test_reset_clears_schemas_and_handlers(db_session: AsyncSession) -> None:
    events.register_event("core_events_test.reset_me", _ValidPayload)

    calls: list[str] = []

    async def handler(_session: AsyncSession, _payload: BaseModel) -> None:
        calls.append("called")

    events.subscribe("core_events_test.reset_me", handler)

    events.reset()

    assert "core_events_test.reset_me" not in events._schemas
    with pytest.raises(events.UnknownEventTypeError):
        await events.publish(
            db_session,
            "core_events_test.reset_me",
            {"company_id": str(uuid.uuid4()), "amount": "1"},
        )
    assert calls == []
