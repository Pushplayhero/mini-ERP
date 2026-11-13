"""tests/platform/test_integration_service.py — Unit tests for platform.integration.

ADR-013 / Week 13 Slice B.

Covers:
1. SSRF prevention:
   - Insecure HTTP rejection (by default)
   - Loopback rejection (127.0.0.1, ::1)
   - RFC-1918 private IP rejection (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
   - Cloud metadata IP rejection (169.254.169.254, fd00:ec2::254)
   - IPv4-mapped IPv6 rejection (::ffff:127.0.0.1)
   - User credentials in URL rejection
   - Invalid port range rejection
   - Unresolvable DNS hostname rejection
   - Rejection of hostnames resolving to private IPs
   - Acceptance of valid public destinations
2. HMAC-SHA256 signing and verification:
   - Secret generation format and masking
   - Signature header generation (t=...,v1=...)
   - Verification success and failure modes (tampering, key mismatch, timestamp skew)
3. WebhookEndpoint service CRUD and tenant scoping.
4. WebhookDelivery DLQ retry and validation.
"""

from __future__ import annotations

import socket
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, set_current_actor
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.tenancy import set_current_company_id
from app.platform.integration import service
from app.platform.integration.models import WebhookDelivery
from app.platform.integration.schemas import (
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
)
from app.platform.integration.signing import (
    generate_webhook_secret,
    get_secret_prefix,
    mask_webhook_secret,
    sign_webhook_payload,
    verify_webhook_signature,
)
from app.platform.integration.ssrf import (
    SSRFValidationError,
    validate_webhook_url,
)
from tests.platform.permissions._helpers import create_company

# ---------------------------------------------------------------------------
# SSRF Protection Tests
# ---------------------------------------------------------------------------


def test_ssrf_rejects_insecure_http_by_default() -> None:
    with pytest.raises(SSRFValidationError, match="Insecure HTTP scheme is not allowed"):
        validate_webhook_url("http://example.com/webhook")


def test_ssrf_allows_http_when_flag_enabled() -> None:
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        url = validate_webhook_url("http://example.com/webhook", allow_insecure_http=True)
        assert url == "http://example.com/webhook"


@pytest.mark.parametrize(
    "blocked_url",
    [
        "https://127.0.0.1/hook",
        "https://127.0.0.2:8443/hook",
        "https://[::1]/hook",
        "https://10.0.0.1/hook",
        "https://172.16.0.1/hook",
        "https://192.168.1.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://[fd00:ec2::254]/metadata",
        "https://0.0.0.0/hook",
        "https://224.0.0.1/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://[::ffff:10.0.0.1]/hook",
    ],
)
def test_ssrf_rejects_restricted_ips(blocked_url: str) -> None:
    with pytest.raises(SSRFValidationError, match="restricted"):
        validate_webhook_url(blocked_url)


def test_ssrf_rejects_userinfo_credentials() -> None:
    with pytest.raises(SSRFValidationError, match="user credentials"):
        validate_webhook_url("https://admin:pass@example.com/webhook")


@pytest.mark.parametrize("invalid_port", ["0", "65536", "70000"])
def test_ssrf_rejects_invalid_ports(invalid_port: str) -> None:
    with pytest.raises(SSRFValidationError, match="Invalid port"):
        validate_webhook_url(f"https://example.com:{invalid_port}/webhook")


def test_ssrf_rejects_unresolvable_hostname() -> None:
    with (
        patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")),
        pytest.raises(SSRFValidationError, match="Failed to resolve hostname"),
    ):
        validate_webhook_url("https://nonexistent-host-test.org/webhook")


def test_ssrf_rejects_hostname_resolving_to_private_ip() -> None:
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))]
    with (
        patch("socket.getaddrinfo", return_value=fake_addrinfo),
        pytest.raises(SSRFValidationError, match="resolves to restricted IP"),
    ):
        validate_webhook_url("https://internal.company.com/webhook")


def test_ssrf_accepts_valid_public_destination() -> None:
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        url = validate_webhook_url("https://api.example.com/v1/webhook")
        assert url == "https://api.example.com/v1/webhook"


# ---------------------------------------------------------------------------
# Signing & Verification Tests
# ---------------------------------------------------------------------------


def test_signing_secret_generation_and_masking() -> None:
    secret = generate_webhook_secret()
    assert secret.startswith("whsec_")
    assert len(secret) == 54

    prefix = get_secret_prefix(secret)
    assert prefix == secret[:12]
    assert len(prefix) == 12

    masked = mask_webhook_secret(secret)
    assert masked == f"{secret[:10]}****"


def test_sign_and_verify_signature_roundtrip() -> None:
    secret = generate_webhook_secret()
    payload = b'{"event":"sales_order.confirmed","id":"123"}'
    timestamp = 1700000000

    header, ts = sign_webhook_payload(payload, secret, timestamp=timestamp)
    assert ts == timestamp
    assert header.startswith(f"t={timestamp},v1=")

    # Verification passes
    assert verify_webhook_signature(
        payload, secret, header, tolerance_seconds=300, current_time=timestamp + 10
    )

    # Verification fails on payload tampering
    tampered = b'{"event":"sales_order.confirmed","id":"999"}'
    assert not verify_webhook_signature(
        tampered, secret, header, tolerance_seconds=300, current_time=timestamp + 10
    )

    # Verification fails on secret mismatch
    other_secret = generate_webhook_secret()
    assert not verify_webhook_signature(
        payload, other_secret, header, tolerance_seconds=300, current_time=timestamp + 10
    )

    # Verification fails on timestamp expiration
    assert not verify_webhook_signature(
        payload, secret, header, tolerance_seconds=300, current_time=timestamp + 301
    )


# ---------------------------------------------------------------------------
# Service CRUD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_endpoint_service_lifecycle(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "INTEG_SVC_CO1")
    set_current_company_id(co_id)
    actor_id = uuid.uuid4()
    set_current_actor(
        ActorContext(
            user_id=actor_id,
            permission_codes=frozenset(
                {"platform.integration.manage", "platform.integration.read"}
            ),
        )
    )

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        # 1. Create endpoint
        create_in = WebhookEndpointCreate(
            name="Primary Webhook",
            url="https://api.external.com/webhooks",
            event_types=["sales_order.confirmed"],
            timeout_seconds=Decimal("15.00"),
            max_attempts=3,
        )
        endpoint, secret = await service.create_webhook_endpoint(db_session, create_in)
        assert endpoint.id is not None
        assert endpoint.company_id == co_id
        assert endpoint.name == "Primary Webhook"
        assert endpoint.signing_secret == secret
        assert endpoint.secret_prefix == secret[:12]
        assert endpoint.is_active is True

        # 2. Conflict on duplicate name
        with pytest.raises(ConflictError, match="already exists"):
            await service.create_webhook_endpoint(db_session, create_in)

        # 3. Get by ID
        fetched = await service.get_webhook_endpoint(db_session, endpoint.id)
        assert fetched.id == endpoint.id

        # 4. List endpoints
        endpoints = await service.list_webhook_endpoints(db_session)
        assert len(endpoints) == 1
        assert endpoints[0].id == endpoint.id

        # 5. Update endpoint
        update_in = WebhookEndpointUpdate(
            name="Updated Webhook",
            is_active=False,
            timeout_seconds=Decimal("20.00"),
        )
        updated = await service.update_webhook_endpoint(db_session, endpoint.id, update_in)
        assert updated.name == "Updated Webhook"
        assert updated.is_active is False
        assert updated.timeout_seconds == Decimal("20.00")

        # 6. Rotate secret with overlap window
        rotated, new_secret = await service.rotate_webhook_endpoint_secret(
            db_session, endpoint.id, overlap_seconds=300
        )
        assert new_secret != secret
        assert rotated.signing_secret == new_secret
        assert rotated.secret_prefix == new_secret[:12]
        assert rotated.previous_signing_secret == secret
        assert rotated.previous_secret_expires_at is not None

        # 7. Soft-deactivate endpoint (record stays readable for audit trail)
        deactivated = await service.deactivate_webhook_endpoint(db_session, endpoint.id)
        assert deactivated.is_active is False
        fetched_after = await service.get_webhook_endpoint(db_session, endpoint.id)
        assert fetched_after.is_active is False

        # Missing ID still raises NotFoundError
        with pytest.raises(NotFoundError):
            await service.get_webhook_endpoint(db_session, uuid.uuid4())


@pytest.mark.asyncio
async def test_webhook_delivery_retry(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "INTEG_SVC_CO2")
    set_current_company_id(co_id)
    actor_id = uuid.uuid4()
    set_current_actor(
        ActorContext(
            user_id=actor_id,
            permission_codes=frozenset(
                {"platform.integration.manage", "platform.integration.read"}
            ),
        )
    )

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        endpoint, _ = await service.create_webhook_endpoint(
            db_session,
            WebhookEndpointCreate(
                name="Delivery Target",
                url="https://api.external.com/webhooks",
            ),
        )

    # Insert an outbox dummy event and a dead delivery
    from sqlalchemy import insert

    from app.core import events

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="test.event",
            payload={"company_id": str(co_id), "msg": "hello"},
        )
    )
    await db_session.flush()

    delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=endpoint.id,
        outbox_event_id=outbox_id,
        event_type="test.event",
        payload={"msg": "hello"},
        status="DEAD",
        attempts=5,
        max_attempts=5,
        last_error="500 Internal Server Error",
    )
    db_session.add(delivery)
    await db_session.flush()

    # Requeue delivery
    retried = await service.retry_webhook_delivery(db_session, delivery.id)
    assert retried.status == "PENDING"
    assert retried.attempts == 0
    assert retried.last_error is None

    # Retrying a non-dead delivery raises DomainValidationError
    with pytest.raises(DomainValidationError, match="must be DEAD"):
        await service.retry_webhook_delivery(db_session, delivery.id)


@pytest.mark.parametrize(
    ("url", "allow_insecure_http"),
    [
        ("https://example.com:8443/webhook", False),
        ("https://example.com:22/webhook", False),
        ("http://example.com:8080/webhook", True),
    ],
)
def test_ssrf_rejects_non_standard_ports(url: str, allow_insecure_http: bool) -> None:
    with pytest.raises(SSRFValidationError, match="Destination port .* is restricted"):
        validate_webhook_url(url, allow_insecure_http=allow_insecure_http)


def test_signing_dual_secret_and_malformed_header_rejection() -> None:
    current_secret = generate_webhook_secret()
    previous_secret = generate_webhook_secret()
    payload = b'{"event":"invoice.posted"}'
    timestamp = 1700000000

    header, ts = sign_webhook_payload(
        payload,
        current_secret,
        previous_secret=previous_secret,
        timestamp=timestamp,
    )
    assert ts == timestamp
    assert header.count(",v1=") == 2

    # Both current and previous secrets verify during the rotation overlap window.
    assert verify_webhook_signature(
        payload, current_secret, header, tolerance_seconds=300, current_time=timestamp + 5
    )
    assert verify_webhook_signature(
        payload, previous_secret, header, tolerance_seconds=300, current_time=timestamp + 5
    )

    # Duplicate t= fields, negative tolerance, empty v1=, and non-digit timestamps fail closed.
    single_sig = header.split(",")[1]
    assert not verify_webhook_signature(
        payload,
        current_secret,
        f"t={timestamp},t={timestamp - 1000},{single_sig}",
        tolerance_seconds=300,
        current_time=timestamp,
    )
    assert not verify_webhook_signature(
        payload, current_secret, header, tolerance_seconds=-1, current_time=timestamp
    )
    assert not verify_webhook_signature(
        payload, current_secret, f"t={timestamp},v1=", current_time=timestamp
    )
    assert not verify_webhook_signature(
        payload, current_secret, f"t=abc,{single_sig}", current_time=timestamp
    )


@pytest.mark.asyncio
async def test_service_rbac_and_deactivation_cancels_pending_deliveries(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "INTEG_RBAC_CO")
    set_current_company_id(co_id)
    actor_id = uuid.uuid4()

    # Actor with no permissions is blocked at the service boundary.
    set_current_actor(ActorContext(user_id=actor_id, permission_codes=frozenset()))
    with pytest.raises(ActionDeniedError):
        await service.list_webhook_endpoints(db_session)
    with pytest.raises(ActionDeniedError):
        await service.create_webhook_endpoint(
            db_session,
            WebhookEndpointCreate(name="Blocked", url="https://127.0.0.1/hook"),
        )

    # Grant manage permission (without read) and create endpoint + mixed deliveries.
    set_current_actor(
        ActorContext(
            user_id=actor_id,
            permission_codes=frozenset({"platform.integration.manage"}),
        )
    )
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        endpoint, _ = await service.create_webhook_endpoint(
            db_session,
            WebhookEndpointCreate(name="Deactivate Cancel", url="https://api.external.com/hook"),
        )

    from sqlalchemy import insert

    from app.core import events

    outbox_pending = uuid.uuid4()
    outbox_done = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE),
        [
            {"id": outbox_pending, "event_type": "e1", "payload": {"company_id": str(co_id)}},
            {"id": outbox_done, "event_type": "e2", "payload": {"company_id": str(co_id)}},
        ],
    )
    pending_deliv = WebhookDelivery(
        company_id=co_id,
        endpoint_id=endpoint.id,
        outbox_event_id=outbox_pending,
        event_type="e1",
        payload={},
        status="PENDING",
    )
    done_deliv = WebhookDelivery(
        company_id=co_id,
        endpoint_id=endpoint.id,
        outbox_event_id=outbox_done,
        event_type="e2",
        payload={},
        status="DELIVERED",
    )
    db_session.add_all([pending_deliv, done_deliv])
    await db_session.flush()

    deactivated = await service.deactivate_webhook_endpoint(db_session, endpoint.id)
    assert deactivated.is_active is False

    await db_session.refresh(pending_deliv)
    await db_session.refresh(done_deliv)
    assert pending_deliv.status == "DEAD"
    assert pending_deliv.last_error == "Webhook endpoint deactivated"
    assert done_deliv.status == "DELIVERED"
