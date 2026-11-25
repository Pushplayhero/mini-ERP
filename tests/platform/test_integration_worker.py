"""tests/platform/test_integration_worker.py — Outbox worker test suite.

ADR-013 / Week 13 Slice C.

Covers:
1. Outbox fan-out to matching subscribed endpoints.
2. Filtering out non-matching endpoints.
3. Idempotent watermark receipts in `integration_outbox_receipts`.
4. Concurrent-safe claim with `FOR UPDATE SKIP LOCKED` and lease deadlines.
5. Successful HTTP delivery (DELIVERED).
6. Retry scheduling with exponential backoff on HTTP/network errors.
7. DLQ routing (DEAD) when max_attempts is exceeded.
8. Crash lease recovery: expired lease in IN_PROGRESS is reclaimed by subsequent pass.
"""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.crypto import encrypt_secret
from app.core.tenancy import company_context
from app.platform.integration import service
from app.platform.integration.models import (
    IntegrationOutboxReceipt,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.platform.integration.schemas import WebhookEndpointCreate
from app.platform.integration.signing import (
    generate_webhook_secret,
    get_secret_prefix,
)
from app.platform.integration.worker import (
    claim_due_deliveries,
    dispatch_single_delivery,
    fan_out_outbox_events,
)
from tests.platform.permissions._helpers import create_company


@pytest.mark.asyncio
async def test_worker_fan_out_and_deduplication(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "WORKER_FO_CO1")
    other_co_id = await create_company(db_session, "WORKER_FO_CO2")

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        from app.core.authorization import ActorContext, actor_context
        from app.core.tenancy import company_context

        actor = ActorContext(
            user_id=uuid.uuid4(),
            permission_codes=frozenset({"platform.integration.manage"}),
        )
        with company_context(co_id), actor_context(actor):
            ep_match, _ = await service.create_webhook_endpoint(
                db_session,
                WebhookEndpointCreate(
                    name="Matching Hook",
                    url="https://api.partner.com/matched",
                    event_types=["sales.order_confirmed"],
                ),
            )
            ep_wildcard, _ = await service.create_webhook_endpoint(
                db_session,
                WebhookEndpointCreate(
                    name="Wildcard Hook",
                    url="https://api.partner.com/wildcard",
                    event_types=["*"],
                ),
            )
            ep_unmatched, _ = await service.create_webhook_endpoint(
                db_session,
                WebhookEndpointCreate(
                    name="Unmatched Hook",
                    url="https://api.partner.com/unmatched",
                    event_types=["invoice.issued"],
                ),
            )

        with company_context(other_co_id), actor_context(actor):
            ep_other_co, _ = await service.create_webhook_endpoint(
                db_session,
                WebhookEndpointCreate(
                    name="Other Co Hook",
                    url="https://api.partner.com/other",
                    event_types=["*"],
                ),
            )

    # Insert an outbox event for co_id
    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="sales.order_confirmed",
            payload={"company_id": str(co_id), "order_no": "SO-001"},
        )
    )
    await db_session.commit()

    # Pass 1: Fan out
    fanned_events, created_delivs = await fan_out_outbox_events(db_session)
    assert fanned_events == 1
    assert created_delivs == 2  # ep_match and ep_wildcard, not ep_unmatched, not ep_other_co

    # Verify deliveries created
    delivs = list(
        (
            await db_session.scalars(
                select(WebhookDelivery).where(WebhookDelivery.outbox_event_id == outbox_id)
            )
        ).all()
    )
    assert len(delivs) == 2
    ep_ids = {d.endpoint_id for d in delivs}
    assert ep_ids == {ep_match.id, ep_wildcard.id}

    # Verify receipt exists
    receipt = await db_session.get(IntegrationOutboxReceipt, outbox_id)
    assert receipt is not None

    # Pass 2: Re-run fan out — deduplication ensures 0 fanned out
    fanned_events_2, created_delivs_2 = await fan_out_outbox_events(db_session)
    assert fanned_events_2 == 0
    assert created_delivs_2 == 0


@pytest.mark.asyncio
async def test_worker_delivery_success_and_retry_backoff(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "WORKER_DISP_CO")
    ep_id = uuid.uuid4()
    plain_secret = generate_webhook_secret()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Target Dispatch",
        url="https://api.example.com/webhook",
        signing_secret=encrypt_secret(plain_secret),
        secret_prefix=get_secret_prefix(plain_secret),
        event_types=["*"],
        is_active=True,
        timeout_seconds=Decimal("5.00"),
        max_attempts=3,
    )
    with company_context(co_id):
        db_session.add(endpoint)
        await db_session.flush()

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="test.event",
            payload={"company_id": str(co_id)},
        )
    )

    delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=outbox_id,
        event_type="test.event",
        payload={"company_id": str(co_id), "data": "payload"},
        status="PENDING",
        attempts=0,
        max_attempts=3,
    )
    db_session.add(delivery)
    await db_session.commit()

    # 1. Claim delivery
    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1
    assert claimed[0].id == delivery.id

    # 2. Simulate 500 error on first attempt
    def _mock_handler_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport_error = httpx.MockTransport(_mock_handler_error)
    async with httpx.AsyncClient(transport=transport_error) as mock_client:
        outcome = await dispatch_single_delivery(
            db_session,
            claimed[0],
            http_client=mock_client,
        )
        assert outcome == "PENDING"

    # Refresh delivery state
    d_after_fail = await db_session.get(WebhookDelivery, delivery.id)
    assert d_after_fail is not None
    assert d_after_fail.status == "PENDING"
    assert d_after_fail.attempts == 1
    assert d_after_fail.last_http_status == 500
    assert d_after_fail.next_attempt_at > datetime.now(UTC)

    # 3. Fast-forward next_attempt_at to now and simulate 200 OK
    d_after_fail.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    claimed_2 = await claim_due_deliveries(db_session)
    assert len(claimed_2) == 1

    def _mock_handler_ok(request: httpx.Request) -> httpx.Response:
        assert "X-Webhook-Signature" in request.headers
        assert request.headers["X-Delivery-Id"] == str(delivery.id)
        return httpx.Response(200, text="OK")

    transport_ok = httpx.MockTransport(_mock_handler_ok)
    async with httpx.AsyncClient(transport=transport_ok) as mock_client:
        outcome_ok = await dispatch_single_delivery(
            db_session,
            claimed_2[0],
            http_client=mock_client,
        )
        assert outcome_ok == "DELIVERED"

    d_success = await db_session.get(WebhookDelivery, delivery.id)
    assert d_success is not None
    assert d_success.status == "DELIVERED"
    assert d_success.delivered_at is not None
    assert d_success.last_http_status == 200
    assert d_success.lease_until is None


@pytest.mark.asyncio
async def test_worker_dlq_routing_on_max_attempts(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "WORKER_DLQ_CO")
    ep_id = uuid.uuid4()
    plain_secret = generate_webhook_secret()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Target DLQ",
        url="https://api.example.com/webhook",
        signing_secret=encrypt_secret(plain_secret),
        secret_prefix=get_secret_prefix(plain_secret),
        event_types=["*"],
        is_active=True,
        timeout_seconds=Decimal("5.00"),
        max_attempts=2,
    )
    with company_context(co_id):
        db_session.add(endpoint)
        await db_session.flush()

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="test.event",
            payload={"company_id": str(co_id)},
        )
    )

    # Delivery already at 1 attempt out of max 2
    delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=outbox_id,
        event_type="test.event",
        payload={"company_id": str(co_id)},
        status="PENDING",
        attempts=1,
        max_attempts=2,
    )
    db_session.add(delivery)
    await db_session.commit()

    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1

    def _mock_handler_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    transport_error = httpx.MockTransport(_mock_handler_error)
    async with httpx.AsyncClient(transport=transport_error) as mock_client:
        outcome = await dispatch_single_delivery(
            db_session,
            claimed[0],
            http_client=mock_client,
        )
        assert outcome == "DEAD"

    d_dead = await db_session.get(WebhookDelivery, delivery.id)
    assert d_dead is not None
    assert d_dead.status == "DEAD"
    assert d_dead.attempts == 2


@pytest.mark.asyncio
async def test_worker_crash_lease_recovery(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "WORKER_LEASE_CO")
    ep_id = uuid.uuid4()
    plain_secret = generate_webhook_secret()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Target Lease",
        url="https://api.example.com/webhook",
        signing_secret=plain_secret,
        secret_prefix=get_secret_prefix(plain_secret),
        event_types=["*"],
        is_active=True,
    )
    with company_context(co_id):
        db_session.add(endpoint)
        await db_session.flush()

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="test.event",
            payload={"company_id": str(co_id)},
        )
    )

    # Crashed worker left delivery in IN_PROGRESS with lease_until in the past
    delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=outbox_id,
        event_type="test.event",
        payload={"company_id": str(co_id)},
        status="IN_PROGRESS",
        attempts=1,
        max_attempts=5,
        next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
        lease_until=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(delivery)
    await db_session.commit()

    # Subsequent worker pass must successfully reclaim it
    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1
    assert claimed[0].id == delivery.id


async def _seed_inflight_endpoint(db_session: AsyncSession, co_id: uuid.UUID) -> uuid.UUID:
    ep_id = uuid.uuid4()
    plain_secret = generate_webhook_secret()
    with company_context(co_id):
        db_session.add(
            WebhookEndpoint(
                id=ep_id,
                company_id=co_id,
                name="Inflight Hook",
                url="https://api.example.com/webhook",
                signing_secret=encrypt_secret(plain_secret),
                secret_prefix=get_secret_prefix(plain_secret),
                event_types=["*"],
                is_active=True,
                max_attempts=5,
            )
        )
        await db_session.flush()
    return ep_id


@pytest.mark.asyncio
async def test_worker_dispatch_defends_against_concurrent_settlement_and_deactivation(
    db_session: AsyncSession,
) -> None:
    """In-flight settlement and endpoint deactivation are respected on lock re-check."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    co_id = await create_company(db_session, "WORKER_INFLIGHT_CO")
    ep_id = await _seed_inflight_endpoint(db_session, co_id)

    ob1, ob2 = uuid.uuid4(), uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE),
        [
            {"id": ob1, "event_type": "e1", "payload": {"company_id": str(co_id)}},
            {"id": ob2, "event_type": "e2", "payload": {"company_id": str(co_id)}},
        ],
    )
    d_stomp = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=ob1,
        event_type="e1",
        payload={"company_id": str(co_id)},
        status="PENDING",
        attempts=0,
        max_attempts=5,
    )
    d_deact = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=ob2,
        event_type="e2",
        payload={"company_id": str(co_id)},
        status="PENDING",
        attempts=0,
        max_attempts=5,
    )
    db_session.add_all([d_stomp, d_deact])
    await db_session.commit()
    stomp_id = d_stomp.id
    deact_id = d_deact.id

    claimed = await claim_due_deliveries(db_session, batch_size=2)
    assert len(claimed) == 2
    claim_by_id = {c.id: c for c in claimed}
    session_factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    # 1. Another worker marks d_stomp DELIVERED while this HTTP call fails with 500
    async def _stomp_handler(_request: httpx.Request) -> httpx.Response:
        async with session_factory() as other:
            row = await other.get(WebhookDelivery, stomp_id)
            assert row is not None
            row.status = "DELIVERED"
            row.attempts = 9
            row.last_error = None
            await other.commit()
        return httpx.Response(500, text="Late failure should not stomp DELIVERED")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_stomp_handler)) as client1:
        outcome_1 = await dispatch_single_delivery(
            db_session, claim_by_id[stomp_id], http_client=client1
        )
    assert outcome_1 == "DELIVERED"
    db_session.expire_all()
    refreshed_stomp = await db_session.get(WebhookDelivery, stomp_id)
    assert refreshed_stomp is not None
    assert refreshed_stomp.status == "DELIVERED"
    assert refreshed_stomp.attempts == 9

    # 2. Endpoint is deactivated while d_deact HTTP call is in flight -> marks DEAD immediately
    async def _deact_handler(_request: httpx.Request) -> httpx.Response:
        async with session_factory() as other:
            with company_context(co_id):
                ep_row = await other.get(WebhookEndpoint, ep_id)
                assert ep_row is not None
                ep_row.is_active = False
                await other.commit()
        return httpx.Response(502, text="Bad Gateway after deactivate")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_deact_handler)) as client2:
        outcome_2 = await dispatch_single_delivery(
            db_session, claim_by_id[deact_id], http_client=client2
        )
    assert outcome_2 == "DEAD"
    db_session.expire_all()
    refreshed_deact = await db_session.get(WebhookDelivery, deact_id)
    assert refreshed_deact is not None
    assert refreshed_deact.status == "DEAD"
    assert refreshed_deact.last_error == "Webhook endpoint deactivated"
