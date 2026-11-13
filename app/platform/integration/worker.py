"""platform.integration outbox worker — ADR-013 / Week 13 Slice C.

Implements:
1. Outbox event fan-out with `FOR UPDATE SKIP LOCKED`, `ON CONFLICT DO NOTHING`
   idempotency, and per-row savepoint poison-pill isolation.
2. Concurrent multi-tenant fair claim (`ROW_NUMBER() OVER (PARTITION BY company_id)`)
   paired with `SELECT ... FOR UPDATE OF webhook_deliveries SKIP LOCKED`.
3. IP-pinned outbound HTTP dispatch strictly OUTSIDE DB locks (closing DNS rebinding),
   with `Accept-Encoding: identity`, `aiter_raw()` decompression-bomb defense, and
   `asyncio.timeout` total wall-clock caps.
4. Dual-secret HMAC-SHA256 signing during secret-rotation overlap windows.
5. Exponential retry backoff and bounded error persistence into `DEAD` status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpcore
import httpx
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import events
from app.core.settings import get_settings
from app.core.tenancy import company_context
from app.platform.integration.models import (
    IntegrationOutboxReceipt,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.platform.integration.signing import sign_webhook_payload
from app.platform.integration.ssrf import (
    PinnedDestination,
    TransientDNSResolutionError,
    resolve_and_pin_webhook_url,
)

logger = logging.getLogger("app.platform.integration.worker")

_MAX_ERROR_LENGTH = 1000
_DEFAULT_DISPATCH_CONCURRENCY = 5


@dataclass
class WorkerRunSummary:
    """Summary of outbox fan-out and delivery activity."""

    fanned_out_events: int = 0
    created_deliveries: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """Snapshot of a delivery claimed for network dispatch."""

    id: uuid.UUID
    company_id: uuid.UUID
    endpoint_id: uuid.UUID
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    event_type: str = ""


class _PinnedIPNetworkBackend(httpcore.AnyIOBackend):
    """ConnectTCP backend forcing outbound TCP sockets to pre-validated IPs."""

    def __init__(
        self,
        expected_host: str,
        expected_port: int,
        pinned_ips: tuple[str, ...],
    ) -> None:
        super().__init__()
        self._expected_host = expected_host
        self._expected_port = expected_port
        self._pinned_ips = pinned_ips

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (
            host.lower() != self._expected_host.lower()
            or port != self._expected_port
            or not self._pinned_ips
        ):
            raise httpcore.ConnectError(
                f"Blocked unexpected outbound connection target {host}:{port}"
            )

        last_exc: Exception | None = None
        for pinned_ip in self._pinned_ips:
            try:
                return await super().connect_tcp(
                    pinned_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """AsyncHTTPTransport pinned to pre-resolved public IPs with env proxy disabled."""

    def __init__(self, destination: PinnedDestination) -> None:
        httpx.AsyncBaseTransport.__init__(self)
        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            http1=True,
            http2=False,
            network_backend=_PinnedIPNetworkBackend(
                expected_host=destination.hostname,
                expected_port=destination.port,
                pinned_ips=destination.resolved_ips,
            ),
        )


def _truncate_error(message: str | None) -> str | None:
    if message is None:
        return None
    return message[:_MAX_ERROR_LENGTH]


async def _record_outbox_receipt(session: AsyncSession, outbox_id: uuid.UUID) -> None:
    stmt = (
        pg_insert(IntegrationOutboxReceipt)
        .values(outbox_id=outbox_id)
        .on_conflict_do_nothing(index_elements=["outbox_id"])
    )
    await session.execute(stmt)


async def _fan_out_single_row(
    session: AsyncSession,
    outbox_id: uuid.UUID,
    event_type: str,
    raw_payload: Any,
) -> int:
    """Fan out one outbox row inside a savepoint so poison pills never block others."""
    created_deliveries = 0
    async with session.begin_nested():
        if not isinstance(raw_payload, dict):
            logger.warning("Skipping non-dict outbox payload for event %s", outbox_id)
            await _record_outbox_receipt(session, outbox_id)
            return 0

        company_id_raw = raw_payload.get("company_id")
        if not company_id_raw:
            await _record_outbox_receipt(session, outbox_id)
            return 0

        try:
            company_id = uuid.UUID(str(company_id_raw))
        except (ValueError, TypeError):
            logger.warning("Skipping outbox event %s with invalid company_id", outbox_id)
            await _record_outbox_receipt(session, outbox_id)
            return 0

        with company_context(company_id):
            ep_stmt = select(WebhookEndpoint).where(
                WebhookEndpoint.company_id == company_id,
                WebhookEndpoint.is_active.is_(True),
            )
            endpoints = list((await session.scalars(ep_stmt)).all())

        for ep in endpoints:
            subscribed = not ep.event_types or "*" in ep.event_types or event_type in ep.event_types
            if not subscribed:
                continue

            deliv_stmt = (
                pg_insert(WebhookDelivery)
                .values(
                    id=uuid.uuid4(),
                    company_id=company_id,
                    endpoint_id=ep.id,
                    outbox_event_id=outbox_id,
                    event_type=event_type,
                    payload=raw_payload,
                    status="PENDING",
                    attempts=0,
                    max_attempts=ep.max_attempts,
                )
                .on_conflict_do_nothing(constraint="uq_webhook_deliveries_idempotency")
                .returning(WebhookDelivery.id)
            )
            inserted_id = await session.scalar(deliv_stmt)
            if inserted_id is not None:
                created_deliveries += 1

        await _record_outbox_receipt(session, outbox_id)
    return created_deliveries


async def fan_out_outbox_events(
    session: AsyncSession,
    *,
    batch_size: int = 100,
) -> tuple[int, int]:
    """Scan un-receipted outbox rows with SKIP LOCKED and populate webhook deliveries.

    Returns:
        tuple of (fanned_out_events_count, created_deliveries_count)
    """
    receipt_exists = (
        select(1).where(IntegrationOutboxReceipt.outbox_id == events.OUTBOX_TABLE.c.id).exists()
    )

    stmt = (
        select(
            events.OUTBOX_TABLE.c.id,
            events.OUTBOX_TABLE.c.event_type,
            events.OUTBOX_TABLE.c.payload,
        )
        .where(~receipt_exists)
        .order_by(events.OUTBOX_TABLE.c.occurred_at)
        .limit(batch_size)
        .with_for_update(of=events.OUTBOX_TABLE, skip_locked=True)
    )

    rows = (await session.execute(stmt)).all()
    if not rows:
        await session.commit()
        return 0, 0

    fanned_out_events = 0
    created_deliveries = 0

    for row in rows:
        try:
            created = await _fan_out_single_row(session, row.id, row.event_type, row.payload)
            fanned_out_events += 1
            created_deliveries += created
        except Exception:
            logger.exception("Failed to fan out outbox event %s; isolating row", row.id)

    await session.commit()
    return fanned_out_events, created_deliveries


async def claim_due_deliveries(
    session: AsyncSession,
    *,
    batch_size: int = 20,
) -> list[ClaimedDelivery]:
    """Claim pending/in-progress due deliveries with cross-tenant round-robin fairness.

    Uses `ROW_NUMBER() OVER (PARTITION BY company_id ...)` joined with
    `FOR UPDATE OF webhook_deliveries SKIP LOCKED` so noisy tenants cannot starve
    others and concurrent workers never double-claim rows.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    lease_deadline = now + timedelta(seconds=settings.webhook_lease_seconds)

    due_ranked = (
        select(
            WebhookDelivery.id.label("delivery_id"),
            sa.func.row_number()
            .over(
                partition_by=WebhookDelivery.company_id,
                order_by=(
                    WebhookDelivery.next_attempt_at.asc(),
                    WebhookDelivery.created_at.asc(),
                ),
            )
            .label("tenant_turn"),
        )
        .where(
            WebhookDelivery.status.in_(("PENDING", "IN_PROGRESS")),
            WebhookDelivery.next_attempt_at <= now,
            sa.or_(
                WebhookDelivery.lease_until.is_(None),
                WebhookDelivery.lease_until < now,
            ),
        )
        .subquery()
    )

    claim_stmt = (
        select(WebhookDelivery)
        .join(due_ranked, WebhookDelivery.id == due_ranked.c.delivery_id)
        .order_by(due_ranked.c.tenant_turn.asc(), WebhookDelivery.next_attempt_at.asc())
        .limit(batch_size)
        .with_for_update(of=WebhookDelivery, skip_locked=True)
    )

    deliveries = list((await session.scalars(claim_stmt)).all())
    if not deliveries:
        await session.commit()
        return []

    claimed: list[ClaimedDelivery] = []
    for d in deliveries:
        d.status = "IN_PROGRESS"
        d.lease_until = lease_deadline
        claimed.append(
            ClaimedDelivery(
                id=d.id,
                company_id=d.company_id,
                endpoint_id=d.endpoint_id,
                payload=d.payload,
                attempts=d.attempts,
                max_attempts=d.max_attempts,
                event_type=d.event_type,
            )
        )

    await session.commit()
    return claimed


async def _read_bounded_response(
    resp: httpx.Response,
    max_response_bytes: int,
) -> tuple[bool, str]:
    """Read raw wire bytes without automatic decompression, capped at max_response_bytes."""
    if resp.is_stream_consumed:
        raw_content = resp.content
        if len(raw_content) > max_response_bytes:
            return False, f"Response exceeded max allowed size of {max_response_bytes} bytes"
        return True, raw_content[:200].decode(errors="replace")

    body_chunks: list[bytes] = []
    preview_len = 0
    bytes_read = 0
    async for chunk in resp.aiter_raw():
        bytes_read += len(chunk)
        if bytes_read > max_response_bytes:
            return False, f"Response exceeded max allowed size of {max_response_bytes} bytes"
        if preview_len < 1024:
            take = chunk[: 1024 - preview_len]
            body_chunks.append(take)
            preview_len += len(take)

    return True, (b"".join(body_chunks)).decode(errors="replace")[:200]


async def _perform_http_dispatch(
    destination: PinnedDestination,
    payload_bytes: bytes,
    headers: dict[str, str],
    timeout_sec: float,
    max_response_bytes: int = 65536,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bool, int | None, str | None]:
    """Execute HTTP POST via IP-pinned transport, wall-clock cap, and raw stream limit."""
    req_headers = {**headers, "Accept-Encoding": "identity"}
    try:
        async with asyncio.timeout(timeout_sec + 1.0):
            if http_client is not None:
                async with http_client.stream(
                    "POST",
                    destination.clean_url,
                    content=payload_bytes,
                    headers=req_headers,
                    timeout=timeout_sec,
                    follow_redirects=False,
                ) as resp:
                    http_status = resp.status_code
                    within_limit, body_preview = await _read_bounded_response(
                        resp, max_response_bytes
                    )
            else:
                transport = _PinnedAsyncHTTPTransport(destination)
                async with (
                    httpx.AsyncClient(
                        transport=transport,
                        timeout=timeout_sec,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        "POST",
                        destination.clean_url,
                        content=payload_bytes,
                        headers=req_headers,
                    ) as resp,
                ):
                    http_status = resp.status_code
                    within_limit, body_preview = await _read_bounded_response(
                        resp, max_response_bytes
                    )

        if not within_limit:
            return False, http_status, body_preview
        if 200 <= http_status < 300:
            return True, http_status, None
        return False, http_status, f"HTTP {http_status}: {body_preview}"
    except Exception as exc:
        return False, None, _truncate_error(f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    """Result of SSRF pre-check and payload serialization before network dispatch."""

    destination: PinnedDestination | None = None
    payload_bytes: bytes | None = None
    dead_error: str | None = None
    retryable_error: str | None = None


def _prepare_delivery_payload(
    endpoint: WebhookEndpoint | None,
    payload: dict[str, Any],
    *,
    resolve_dns: bool,
) -> _PreparedDelivery:
    """Validate endpoint URL + serialize payload, distinguishing transient DNS errors."""
    if endpoint is None or not endpoint.is_active:
        return _PreparedDelivery(dead_error="Target webhook endpoint is inactive or was deleted")

    settings = get_settings()
    try:
        destination = resolve_and_pin_webhook_url(
            endpoint.url,
            allow_insecure_http=settings.webhook_allow_insecure_http,
            allow_local_test_destinations=False,
            resolve_dns=resolve_dns,
        )
    except TransientDNSResolutionError as exc:
        return _PreparedDelivery(retryable_error=_truncate_error(f"Transient DNS failure: {exc}"))
    except Exception as exc:
        return _PreparedDelivery(
            dead_error=_truncate_error(f"SSRF re-validation failed at dispatch: {exc}")
        )

    try:
        payload_bytes = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return _PreparedDelivery(destination=destination, payload_bytes=payload_bytes)
    except Exception as exc:
        return _PreparedDelivery(
            dead_error=_truncate_error(f"Malformed payload serialization error: {exc}")
        )


async def _settle_failed_attempt(
    session: AsyncSession,
    delivery: WebhookDelivery,
    claimed: ClaimedDelivery,
    error_message: str | None,
    *,
    http_status: int | None = None,
) -> str:
    """Persist retry backoff or transition delivery to DEAD once attempts exhaust."""
    settle_now = datetime.now(UTC)
    new_attempts = claimed.attempts + 1
    delivery.attempts = new_attempts
    delivery.last_http_status = http_status
    delivery.last_error = _truncate_error(error_message)
    delivery.lease_until = None

    if new_attempts >= claimed.max_attempts:
        delivery.status = "DEAD"
        await session.commit()
        return "DEAD"

    delay_seconds = min(3600, 5 * (2 ** (new_attempts - 1)))
    delivery.status = "PENDING"
    delivery.next_attempt_at = settle_now + timedelta(seconds=delay_seconds)
    await session.commit()
    return "PENDING"


def _active_previous_secret(endpoint: WebhookEndpoint, now: datetime) -> str | None:
    if (
        endpoint.previous_signing_secret
        and endpoint.previous_secret_expires_at is not None
        and endpoint.previous_secret_expires_at > now
    ):
        return endpoint.previous_signing_secret
    return None


async def dispatch_single_delivery(
    session: AsyncSession,
    claimed: ClaimedDelivery,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Perform network delivery for one claimed webhook attempt."""
    with company_context(claimed.company_id):
        endpoint = await session.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == claimed.endpoint_id,
                WebhookEndpoint.company_id == claimed.company_id,
            )
        )

    delivery = await session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.id == claimed.id,
            WebhookDelivery.company_id == claimed.company_id,
        )
    )
    if delivery is None:
        return "DEAD"

    prepared = _prepare_delivery_payload(
        endpoint,
        claimed.payload,
        resolve_dns=(http_client is None),
    )
    if prepared.retryable_error is not None:
        return await _settle_failed_attempt(
            session,
            delivery,
            claimed,
            prepared.retryable_error,
        )
    if (
        prepared.dead_error is not None
        or prepared.payload_bytes is None
        or prepared.destination is None
    ):
        delivery.status = "DEAD"
        delivery.lease_until = None
        delivery.last_error = _truncate_error(prepared.dead_error)
        await session.commit()
        return "DEAD"

    assert endpoint is not None
    now = datetime.now(UTC)
    prev_secret = _active_previous_secret(endpoint, now)
    sig_header, ts = sign_webhook_payload(
        prepared.payload_bytes,
        endpoint.signing_secret,
        previous_secret=prev_secret,
    )
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": sig_header,
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Event": claimed.event_type or delivery.event_type,
        "User-Agent": "MiniERP-Integration-Worker/1.0",
        "X-Delivery-Id": str(claimed.id),
    }

    settings = get_settings()
    max_dispatch_timeout = max(1.0, float(settings.webhook_lease_seconds) - 5.0)
    raw_timeout = float(endpoint.timeout_seconds or settings.webhook_timeout_seconds)
    timeout_sec = min(raw_timeout, max_dispatch_timeout)

    success, http_status, error_message = await _perform_http_dispatch(
        prepared.destination,
        prepared.payload_bytes,
        headers,
        timeout_sec,
        max_response_bytes=settings.webhook_max_response_bytes,
        http_client=http_client,
    )

    if success:
        delivery.attempts = claimed.attempts + 1
        delivery.status = "DELIVERED"
        delivery.delivered_at = datetime.now(UTC)
        delivery.last_http_status = http_status
        delivery.lease_until = None
        delivery.last_error = None
        await session.commit()
        return "DELIVERED"

    return await _settle_failed_attempt(
        session,
        delivery,
        claimed,
        error_message,
        http_status=http_status,
    )


async def process_outbox_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    http_client: httpx.AsyncClient | None = None,
    fanout_batch_size: int = 100,
    delivery_batch_size: int = _DEFAULT_DISPATCH_CONCURRENCY,
    max_concurrency: int = _DEFAULT_DISPATCH_CONCURRENCY,
) -> WorkerRunSummary:
    """Run one pass of fan-out and lease-bounded concurrent delivery processing."""
    summary = WorkerRunSummary()

    # Phase 1: Fan-out from durable outbox
    async with session_factory() as session:
        events_count, deliveries_count = await fan_out_outbox_events(
            session,
            batch_size=fanout_batch_size,
        )
        summary.fanned_out_events = events_count
        summary.created_deliveries = deliveries_count

    # Phase 2: Claim at most max_concurrency rows so queued items never outlive leases
    effective_batch = max(1, min(delivery_batch_size, max_concurrency))
    async with session_factory() as session:
        claimed = await claim_due_deliveries(
            session,
            batch_size=effective_batch,
        )

    # Phase 3: Dispatch network deliveries concurrently
    dispatch_semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _safe_dispatch(item: ClaimedDelivery) -> str:
        async with dispatch_semaphore, session_factory() as session:
            return await dispatch_single_delivery(
                session,
                item,
                http_client=http_client,
            )

    results = await asyncio.gather(
        *[_safe_dispatch(item) for item in claimed],
        return_exceptions=True,
    )
    for item, outcome in zip(claimed, results, strict=True):
        if isinstance(outcome, BaseException):
            logger.exception("Unhandled error dispatching delivery %s", item.id, exc_info=outcome)
            continue
        if outcome == "DELIVERED":
            summary.delivered += 1
        elif outcome == "PENDING":
            summary.retried += 1
        elif outcome == "DEAD":
            summary.dead += 1

    return summary
