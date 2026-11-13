"""platform.integration service layer — ADR-013 / Week 13 Slice B.

Implements:
1. Webhook endpoint lifecycle (create, update, dual-secret rotation, soft deactivation)
   serialized per-tenant via `lock_company_unchecked` with service-layer RBAC checks.
2. SSRF validation on target URLs using system settings.
3. Plaintext secret generation and one-time display guarantees.
4. Webhook delivery history listing, lookup, and dead-letter queue manual retry.
5. Batch import/export job history inspection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import require_current_actor, require_permission
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.settings import get_settings
from app.core.tenancy import require_current_company_id
from app.modules.masterdata import service as masterdata_service
from app.platform.integration.models import (
    IntegrationBatchJob,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.platform.integration.schemas import (
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
)
from app.platform.integration.signing import generate_webhook_secret, get_secret_prefix
from app.platform.integration.ssrf import validate_webhook_url

_DEFAULT_ROTATION_OVERLAP_SECONDS = 86_400


async def _load_webhook_endpoint_unchecked(
    db: AsyncSession,
    company_id: uuid.UUID,
    endpoint_id: uuid.UUID,
) -> WebhookEndpoint:
    """Load an endpoint within `company_id` without asserting read permission."""
    endpoint = await db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.company_id == company_id,
        )
    )
    if endpoint is None:
        raise NotFoundError("WebhookEndpoint", endpoint_id)
    return endpoint


async def _load_webhook_delivery_unchecked(
    db: AsyncSession,
    company_id: uuid.UUID,
    delivery_id: uuid.UUID,
) -> WebhookDelivery:
    """Load a delivery within `company_id` without asserting read permission."""
    delivery = await db.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.company_id == company_id,
        )
    )
    if delivery is None:
        raise NotFoundError("WebhookDelivery", delivery_id)
    return delivery


async def _cancel_pending_deliveries(
    db: AsyncSession,
    company_id: uuid.UUID,
    endpoint_id: uuid.UUID,
) -> None:
    """Mark queued PENDING deliveries DEAD when an endpoint is deactivated."""
    await db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.company_id == company_id,
            WebhookDelivery.endpoint_id == endpoint_id,
            WebhookDelivery.status == "PENDING",
        )
        .values(
            status="DEAD",
            lease_until=None,
            last_error="Webhook endpoint deactivated",
        )
    )


async def create_webhook_endpoint(
    db: AsyncSession,
    payload: WebhookEndpointCreate,
) -> tuple[WebhookEndpoint, str]:
    """Create a new webhook endpoint for the active tenant.

    Returns:
        tuple of (WebhookEndpoint, plaintext_signing_secret)
    """
    require_permission("platform.integration.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(db, company_id)

    settings = get_settings()
    validated_url = validate_webhook_url(
        payload.url,
        allow_insecure_http=settings.webhook_allow_insecure_http,
    )

    existing = await db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.company_id == company_id,
            WebhookEndpoint.name == payload.name,
        )
    )
    if existing is not None:
        raise ConflictError(f"Webhook endpoint '{payload.name}' already exists")

    secret = generate_webhook_secret()
    prefix = get_secret_prefix(secret)
    timeout_seconds = (
        payload.timeout_seconds
        if payload.timeout_seconds is not None
        else Decimal(str(settings.webhook_timeout_seconds)).quantize(Decimal("0.01"))
    )

    endpoint = WebhookEndpoint(
        company_id=company_id,
        name=payload.name,
        url=validated_url,
        signing_secret=secret,
        secret_prefix=prefix,
        event_types=payload.event_types,
        is_active=True,
        timeout_seconds=timeout_seconds,
        max_attempts=payload.max_attempts,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint, secret


async def get_webhook_endpoint(
    db: AsyncSession,
    endpoint_id: uuid.UUID,
) -> WebhookEndpoint:
    """Retrieve an endpoint by ID for the active tenant."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    return await _load_webhook_endpoint_unchecked(db, company_id, endpoint_id)


async def list_webhook_endpoints(
    db: AsyncSession,
    *,
    is_active: bool | None = None,
) -> list[WebhookEndpoint]:
    """List webhook endpoints for the active tenant."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.company_id == company_id)
    if is_active is not None:
        stmt = stmt.where(WebhookEndpoint.is_active == is_active)
    stmt = stmt.order_by(WebhookEndpoint.created_at.desc())
    result = await db.scalars(stmt)
    return list(result.all())


async def update_webhook_endpoint(
    db: AsyncSession,
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointUpdate,
) -> WebhookEndpoint:
    """Update mutable attributes of an existing webhook endpoint."""
    require_permission("platform.integration.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(db, company_id)
    endpoint = await _load_webhook_endpoint_unchecked(db, company_id, endpoint_id)

    if payload.name is not None and payload.name != endpoint.name:
        existing = await db.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.company_id == company_id,
                WebhookEndpoint.name == payload.name,
                WebhookEndpoint.id != endpoint_id,
            )
        )
        if existing is not None:
            raise ConflictError(f"Webhook endpoint '{payload.name}' already exists")
        endpoint.name = payload.name

    if payload.url is not None:
        settings = get_settings()
        endpoint.url = validate_webhook_url(
            payload.url,
            allow_insecure_http=settings.webhook_allow_insecure_http,
        )

    if payload.event_types is not None:
        endpoint.event_types = payload.event_types

    if payload.is_active is not None:
        endpoint.is_active = payload.is_active
        if not payload.is_active:
            await _cancel_pending_deliveries(db, company_id, endpoint_id)

    if payload.timeout_seconds is not None:
        endpoint.timeout_seconds = payload.timeout_seconds

    if payload.max_attempts is not None:
        endpoint.max_attempts = payload.max_attempts

    endpoint.updated_by = actor.user_id
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


async def rotate_webhook_endpoint_secret(
    db: AsyncSession,
    endpoint_id: uuid.UUID,
    *,
    overlap_seconds: int = _DEFAULT_ROTATION_OVERLAP_SECONDS,
) -> tuple[WebhookEndpoint, str]:
    """Rotate the HMAC signing secret while preserving a dual-signature overlap window.

    Returns:
        tuple of (WebhookEndpoint, new_plaintext_signing_secret)
    """
    require_permission("platform.integration.manage")
    if overlap_seconds < 0:
        raise DomainValidationError("overlap_seconds must be non-negative")

    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(db, company_id)
    endpoint = await _load_webhook_endpoint_unchecked(db, company_id, endpoint_id)

    new_secret = generate_webhook_secret()
    if overlap_seconds > 0:
        endpoint.previous_signing_secret = endpoint.signing_secret
        endpoint.previous_secret_expires_at = datetime.now(UTC) + timedelta(seconds=overlap_seconds)
    else:
        endpoint.previous_signing_secret = None
        endpoint.previous_secret_expires_at = None

    endpoint.signing_secret = new_secret
    endpoint.secret_prefix = get_secret_prefix(new_secret)
    endpoint.updated_by = actor.user_id

    await db.commit()
    await db.refresh(endpoint)
    return endpoint, new_secret


async def deactivate_webhook_endpoint(
    db: AsyncSession,
    endpoint_id: uuid.UUID,
) -> WebhookEndpoint:
    """Soft-deactivate an endpoint and cancel its pending queued deliveries."""
    require_permission("platform.integration.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(db, company_id)
    endpoint = await _load_webhook_endpoint_unchecked(db, company_id, endpoint_id)

    endpoint.is_active = False
    endpoint.updated_by = actor.user_id
    await _cancel_pending_deliveries(db, company_id, endpoint_id)

    await db.commit()
    await db.refresh(endpoint)
    return endpoint


async def list_webhook_deliveries(
    db: AsyncSession,
    *,
    endpoint_id: uuid.UUID | None = None,
    status: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[WebhookDelivery]:
    """List webhook delivery history for the active tenant."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    stmt = select(WebhookDelivery).where(WebhookDelivery.company_id == company_id)

    if endpoint_id is not None:
        stmt = stmt.where(WebhookDelivery.endpoint_id == endpoint_id)
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    if event_type is not None:
        stmt = stmt.where(WebhookDelivery.event_type == event_type)

    stmt = stmt.order_by(WebhookDelivery.created_at.desc()).limit(limit).offset(offset)
    result = await db.scalars(stmt)
    return list(result.all())


async def get_webhook_delivery(
    db: AsyncSession,
    delivery_id: uuid.UUID,
) -> WebhookDelivery:
    """Retrieve a single webhook delivery record by ID."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    return await _load_webhook_delivery_unchecked(db, company_id, delivery_id)


async def retry_webhook_delivery(
    db: AsyncSession,
    delivery_id: uuid.UUID,
) -> WebhookDelivery:
    """Requeue a dead-letter delivery back to PENDING status."""
    require_permission("platform.integration.manage")
    company_id = require_current_company_id()
    await masterdata_service.lock_company_unchecked(db, company_id)

    delivery = await _load_webhook_delivery_unchecked(db, company_id, delivery_id)
    if delivery.status not in ("DEAD", "FAILED"):
        raise DomainValidationError(
            f"Cannot retry delivery in status '{delivery.status}': must be DEAD"
        )

    delivery.status = "PENDING"
    delivery.attempts = 0
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.lease_until = None
    delivery.last_error = None
    await db.commit()
    await db.refresh(delivery)
    return delivery


async def list_batch_jobs(
    db: AsyncSession,
    *,
    job_type: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[IntegrationBatchJob]:
    """List batch import/export audit jobs for the active tenant."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    stmt = select(IntegrationBatchJob).where(IntegrationBatchJob.company_id == company_id)
    if job_type is not None:
        stmt = stmt.where(IntegrationBatchJob.job_type == job_type)
    if entity_type is not None:
        stmt = stmt.where(IntegrationBatchJob.entity_type == entity_type)
    stmt = stmt.order_by(IntegrationBatchJob.created_at.desc()).limit(limit).offset(offset)
    result = await db.scalars(stmt)
    return list(result.all())


async def get_batch_job(
    db: AsyncSession,
    job_id: uuid.UUID,
) -> IntegrationBatchJob:
    """Retrieve a single batch job record for the active tenant."""
    require_permission("platform.integration.read")
    company_id = require_current_company_id()
    job = await db.scalar(
        select(IntegrationBatchJob).where(
            IntegrationBatchJob.id == job_id,
            IntegrationBatchJob.company_id == company_id,
        )
    )
    if job is None:
        raise NotFoundError("IntegrationBatchJob", job_id)
    return job
