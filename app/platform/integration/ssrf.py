"""SSRF protection for outbound webhooks — ADR-013 / Week 13 Slice B.

Enforces:
1. URL scheme validation (HTTPS enforced unless allow_insecure_http is explicitly set).
2. Elimination of URL userinfo/credentials.
3. Strict port allowlist (443 only by default; 80 and 443 when allow_insecure_http=True).
4. DNS resolution and rejection of loopback, private RFC-1918, CGNAT, benchmark,
   link-local, multicast, unspecified, reserved, IPv4-mapped IPv6, and cloud metadata IPs.
5. IP-pinning metadata (`PinnedDestination`) so HTTP clients connect directly to validated
   IPs without performing a second DNS lookup (closing the DNS-rebinding TOCTOU window).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.exceptions import DomainValidationError


class SSRFValidationError(DomainValidationError):
    """Raised when a destination URL fails SSRF safety validation."""


class TransientDNSResolutionError(SSRFValidationError):
    """Raised when outbound hostname DNS lookup fails transiently."""


@dataclass(frozen=True, slots=True)
class PinnedDestination:
    """Validated webhook target together with pinned IP addresses."""

    clean_url: str
    scheme: str
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]


_CLOUD_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud IMDS
    }
)

_ADDITIONAL_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 Carrier-Grade NAT (CGNAT)
    ipaddress.ip_network("198.18.0.0/15"),  # RFC 2544 Benchmark testing
)


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP address is private, loopback, link-local, or cloud metadata."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip in _CLOUD_METADATA_IPS:
        return True

    if any(ip in net for net in _ADDITIONAL_BLOCKED_NETWORKS):
        return True

    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _normalize_hostname(hostname: str) -> str:
    """Normalize a non-IP hostname to lowercase ASCII IDNA Punycode."""
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SSRFValidationError(f"Invalid webhook hostname '{hostname}': {exc}") from exc
    if not ascii_host:
        raise SSRFValidationError("Webhook URL must include a valid hostname")
    return ascii_host


def _resolve_and_validate_hostname(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve hostname to IP addresses, assert none are restricted, and return pinned IPs."""
    try:
        addrinfo = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransientDNSResolutionError(
            f"Failed to resolve hostname '{hostname}': {exc}"
        ) from exc
    except Exception as exc:
        raise SSRFValidationError(f"DNS resolution error for '{hostname}': {exc}") from exc

    if not addrinfo:
        raise SSRFValidationError(f"Hostname '{hostname}' resolved to no addresses")

    pinned_ips: list[str] = []
    seen: set[str] = set()
    for entry in addrinfo:
        sockaddr = entry[4]
        ip_str = str(sockaddr[0])
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise SSRFValidationError(f"Invalid resolved IP '{ip_str}': {exc}") from exc

        if is_ip_blocked(resolved_ip):
            raise SSRFValidationError(
                f"Hostname '{hostname}' resolves to restricted IP '{resolved_ip}'"
            )
        normalized = str(resolved_ip)
        if normalized not in seen:
            seen.add(normalized)
            pinned_ips.append(normalized)

    return tuple(pinned_ips)


def resolve_and_pin_webhook_url(
    url: str,
    *,
    allow_insecure_http: bool = False,
    allow_local_test_destinations: bool = False,
    resolve_dns: bool = True,
) -> PinnedDestination:
    """Validate a webhook URL and resolve pinned public IP addresses.

    Raises SSRFValidationError on any scheme, credential, port, IP range, or DNS violation.
    """
    clean_url = url.strip()
    if not clean_url:
        raise SSRFValidationError("Webhook URL must not be empty")

    try:
        parsed = urlsplit(clean_url)
        raw_port = parsed.port
    except ValueError as exc:
        raise SSRFValidationError(f"Invalid port: {exc}") from exc
    except Exception as exc:
        raise SSRFValidationError(f"Malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = {"https", "http"} if allow_insecure_http else {"https"}
    if scheme not in allowed_schemes:
        if scheme == "http":
            raise SSRFValidationError("Insecure HTTP scheme is not allowed; HTTPS is required")
        raise SSRFValidationError(f"Unsupported URL scheme '{scheme}': must be HTTPS")

    if parsed.username or parsed.password:
        raise SSRFValidationError("Webhook URLs must not include user credentials")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFValidationError("Webhook URL must include a valid hostname")

    if raw_port is not None and not (1 <= raw_port <= 65535):
        raise SSRFValidationError(f"Invalid port: {raw_port}")

    port = raw_port or (443 if scheme == "https" else 80)

    # Check direct IP literals before port restrictions so restricted IPs report clearly.
    raw_host = hostname.strip("[]")
    try:
        ip = ipaddress.ip_address(raw_host)
        if not allow_local_test_destinations and is_ip_blocked(ip):
            raise SSRFValidationError(f"Destination IP '{ip}' is in a restricted range")
        if not allow_local_test_destinations:
            _assert_allowed_port(port, allow_insecure_http=allow_insecure_http)
        return PinnedDestination(
            clean_url=clean_url,
            scheme=scheme,
            hostname=hostname,
            port=port,
            resolved_ips=(str(ip),),
        )
    except ValueError:
        pass

    if not allow_local_test_destinations:
        _assert_allowed_port(port, allow_insecure_http=allow_insecure_http)

    normalized_host = _normalize_hostname(hostname)

    if allow_local_test_destinations or not resolve_dns:
        return PinnedDestination(
            clean_url=clean_url,
            scheme=scheme,
            hostname=normalized_host,
            port=port,
            resolved_ips=(),
        )

    pinned_ips = _resolve_and_validate_hostname(normalized_host, port)
    return PinnedDestination(
        clean_url=clean_url,
        scheme=scheme,
        hostname=normalized_host,
        port=port,
        resolved_ips=pinned_ips,
    )


def _assert_allowed_port(port: int, *, allow_insecure_http: bool) -> None:
    """Enforce strict webhook egress port allowlist."""
    allowed_ports = {443, 80} if allow_insecure_http else {443}
    if port not in allowed_ports:
        raise SSRFValidationError(
            f"Destination port {port} is restricted; allowed ports: {sorted(allowed_ports)}"
        )


def validate_webhook_url(
    url: str,
    *,
    allow_insecure_http: bool = False,
    allow_local_test_destinations: bool = False,
) -> str:
    """Validate a webhook URL against SSRF and protocol safety rules.

    Returns the stripped, validated URL string if safe.
    Raises SSRFValidationError on any safety or format violation.
    """
    destination = resolve_and_pin_webhook_url(
        url,
        allow_insecure_http=allow_insecure_http,
        allow_local_test_destinations=allow_local_test_destinations,
        resolve_dns=True,
    )
    return destination.clean_url
