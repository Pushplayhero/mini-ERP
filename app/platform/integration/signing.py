"""HMAC-SHA256 signing and verification for webhooks — ADR-013 / Week 13 Slice B.

Implements standard Stripe/Svix compatible header signatures:
    X-Webhook-Signature: t=1700000000,v1=a1b2c3d4...
where v1 is HMAC-SHA256(secret, "${timestamp}.${payload}").
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime


def generate_webhook_secret() -> str:
    """Generate a high-entropy signing secret starting with 'whsec_'."""
    return f"whsec_{secrets.token_hex(24)}"


def get_secret_prefix(secret: str) -> str:
    """Return a short prefix of the secret for administrative identification."""
    if len(secret) <= 12:
        return secret
    return secret[:12]


def mask_webhook_secret(secret: str) -> str:
    """Return a masked representation of the signing secret."""
    if len(secret) <= 10:
        return "****"
    return f"{secret[:10]}****"


def sign_webhook_payload(
    payload_bytes: bytes,
    secret: str,
    timestamp: int | None = None,
    *,
    previous_secret: str | None = None,
) -> tuple[str, int]:
    """Sign payload bytes using HMAC-SHA256.

    When `previous_secret` is supplied during a rotation overlap window, emits
    both `v1=<current>` and `v1=<previous>` signatures in the header so
    receivers can migrate without downtime.

    Returns:
        tuple of (signature_header, unix_timestamp)
    """
    if timestamp is None:
        timestamp = int(datetime.now(UTC).timestamp())

    signature_payload = f"{timestamp}.".encode() + payload_bytes
    signature = hmac.new(
        secret.encode(),
        signature_payload,
        hashlib.sha256,
    ).hexdigest()

    header_parts = [f"t={timestamp}", f"v1={signature}"]
    if previous_secret and previous_secret != secret:
        prev_signature = hmac.new(
            previous_secret.encode(),
            signature_payload,
            hashlib.sha256,
        ).hexdigest()
        header_parts.append(f"v1={prev_signature}")

    return ",".join(header_parts), timestamp


def _parse_signature_header(header: str) -> tuple[str, list[str]] | None:
    """Parse ``t=...,v1=...`` webhook signature header or return None if malformed."""
    ts_str: str | None = None
    v1_signatures: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            return None
        k, v = part.split("=", 1)
        k_clean = k.strip()
        v_clean = v.strip()
        if not k_clean or not v_clean:
            return None
        if k_clean == "t":
            # Reject duplicate timestamp fields so stale t= cannot be masked.
            if ts_str is not None:
                return None
            ts_str = v_clean
        elif k_clean == "v1":
            v1_signatures.append(v_clean)

    if not ts_str or not ts_str.isdigit() or not v1_signatures:
        return None
    return ts_str, v1_signatures


def verify_webhook_signature(
    payload_bytes: bytes,
    secret: str,
    header: str,
    *,
    tolerance_seconds: int = 300,
    current_time: int | None = None,
) -> bool:
    """Verify an incoming webhook signature header.

    Returns True if the signature matches and timestamp is within tolerance.
    """
    if not header or not secret or tolerance_seconds < 0:
        return False

    parsed = _parse_signature_header(header)
    if parsed is None:
        return False

    ts_str, v1_signatures = parsed
    ts = int(ts_str)
    now = int(time.time()) if current_time is None else current_time
    if abs(now - ts) > tolerance_seconds:
        return False

    signature_payload = f"{ts}.".encode() + payload_bytes
    expected_sig = hmac.new(
        secret.encode(),
        signature_payload,
        hashlib.sha256,
    ).hexdigest()

    return any(hmac.compare_digest(expected_sig, sig) for sig in v1_signatures)
