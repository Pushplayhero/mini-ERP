"""platform.integration FastAPI routes — ADR-013 / Week 13 Slice B & C.

Endpoints mounted under `/api/v1/integrations`:
- `GET /api/v1/integrations/webhooks`: List webhook endpoints.
- `POST /api/v1/integrations/webhooks`: Register a webhook endpoint.
- `GET /api/v1/integrations/webhooks/{endpoint_id}`: Get a webhook endpoint.
- `PATCH /api/v1/integrations/webhooks/{endpoint_id}`: Update endpoint configuration.
- `POST /api/v1/integrations/webhooks/{endpoint_id}/rotate-secret`: Rotate signing secret.
- `DELETE /api/v1/integrations/webhooks/{endpoint_id}`: Soft-deactivate a webhook endpoint.
- `GET /api/v1/integrations/deliveries`: List delivery history.
- `GET /api/v1/integrations/deliveries/{delivery_id}`: Get delivery record.
- `POST /api/v1/integrations/deliveries/{delivery_id}/retry`: Retry dead delivery.
- `POST /api/v1/integrations/imports`: Execute batch masterdata import.
- `POST /api/v1/integrations/exports`: Execute batch entity export.
- `GET /api/v1/integrations/jobs`: List batch import/export audit jobs.
- `GET /api/v1/integrations/jobs/{job_id}`: Get batch job details.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.platform.integration import batch, service
from app.platform.integration.schemas import (
    BatchExportRequest,
    BatchExportResponse,
    BatchImportRequest,
    BatchImportResponse,
    IntegrationBatchJobRead,
    WebhookDeliveryRead,
    WebhookEndpointCreate,
    WebhookEndpointCreatedRead,
    WebhookEndpointRead,
    WebhookEndpointSecretRotateRead,
    WebhookEndpointUpdate,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


# ---------------------------------------------------------------------------
# Webhook Endpoint CRUD & Secret Rotation
# ---------------------------------------------------------------------------


@router.get(
    "/webhooks",
    response_model=list[WebhookEndpointRead],
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def list_endpoints_endpoint(
    session: DbSession,
    is_active: Annotated[
        bool | None,
        Query(description="Filter by active status"),
    ] = None,
) -> list[WebhookEndpointRead]:
    """List webhook endpoints for the active tenant."""
    endpoints = await service.list_webhook_endpoints(session, is_active=is_active)
    return [WebhookEndpointRead.model_validate(ep) for ep in endpoints]


@router.post(
    "/webhooks",
    response_model=WebhookEndpointCreatedRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("platform.integration.manage"))],
)
async def create_endpoint_endpoint(
    payload: WebhookEndpointCreate,
    session: DbSession,
) -> WebhookEndpointCreatedRead:
    """Create a new webhook endpoint and return its plaintext signing secret once."""
    endpoint, secret = await service.create_webhook_endpoint(session, payload)
    data = WebhookEndpointRead.model_validate(endpoint).model_dump()
    return WebhookEndpointCreatedRead(**data, signing_secret=secret)


@router.get(
    "/webhooks/{endpoint_id}",
    response_model=WebhookEndpointRead,
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def get_endpoint_endpoint(
    endpoint_id: uuid.UUID,
    session: DbSession,
) -> WebhookEndpointRead:
    """Retrieve details of a webhook endpoint by ID."""
    endpoint = await service.get_webhook_endpoint(session, endpoint_id)
    return WebhookEndpointRead.model_validate(endpoint)


@router.patch(
    "/webhooks/{endpoint_id}",
    response_model=WebhookEndpointRead,
    dependencies=[Depends(RequiresPermission("platform.integration.manage"))],
)
async def update_endpoint_endpoint(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointUpdate,
    session: DbSession,
) -> WebhookEndpointRead:
    """Update mutable attributes of a webhook endpoint."""
    endpoint = await service.update_webhook_endpoint(session, endpoint_id, payload)
    return WebhookEndpointRead.model_validate(endpoint)


@router.post(
    "/webhooks/{endpoint_id}/rotate-secret",
    response_model=WebhookEndpointSecretRotateRead,
    dependencies=[Depends(RequiresPermission("platform.integration.manage"))],
)
async def rotate_secret_endpoint(
    endpoint_id: uuid.UUID,
    session: DbSession,
) -> WebhookEndpointSecretRotateRead:
    """Rotate the HMAC signing secret of an endpoint with a dual-secret overlap window."""
    endpoint, secret = await service.rotate_webhook_endpoint_secret(session, endpoint_id)
    return WebhookEndpointSecretRotateRead(
        endpoint_id=endpoint.id,
        signing_secret=secret,
        secret_prefix=endpoint.secret_prefix,
        previous_secret_expires_at=endpoint.previous_secret_expires_at,
    )


@router.delete(
    "/webhooks/{endpoint_id}",
    response_model=WebhookEndpointRead,
    dependencies=[Depends(RequiresPermission("platform.integration.manage"))],
)
async def deactivate_endpoint_endpoint(
    endpoint_id: uuid.UUID,
    session: DbSession,
) -> WebhookEndpointRead:
    """Soft-deactivate a webhook endpoint and cancel pending queued deliveries."""
    endpoint = await service.deactivate_webhook_endpoint(session, endpoint_id)
    return WebhookEndpointRead.model_validate(endpoint)


# ---------------------------------------------------------------------------
# Delivery History & DLQ
# ---------------------------------------------------------------------------


@router.get(
    "/deliveries",
    response_model=list[WebhookDeliveryRead],
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def list_deliveries_endpoint(
    session: DbSession,
    endpoint_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter by endpoint ID"),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Filter by status (PENDING, IN_PROGRESS, DELIVERED, DEAD)",
        ),
    ] = None,
    event_type: Annotated[
        str | None,
        Query(description="Filter by event type"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WebhookDeliveryRead]:
    """List webhook delivery records."""
    deliveries = await service.list_webhook_deliveries(
        session,
        endpoint_id=endpoint_id,
        status=status_filter,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    return [WebhookDeliveryRead.model_validate(d) for d in deliveries]


@router.get(
    "/deliveries/{delivery_id}",
    response_model=WebhookDeliveryRead,
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def get_delivery_endpoint(
    delivery_id: uuid.UUID,
    session: DbSession,
) -> WebhookDeliveryRead:
    """Retrieve details of a single delivery record."""
    delivery = await service.get_webhook_delivery(session, delivery_id)
    return WebhookDeliveryRead.model_validate(delivery)


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=WebhookDeliveryRead,
    dependencies=[Depends(RequiresPermission("platform.integration.manage"))],
)
async def retry_delivery_endpoint(
    delivery_id: uuid.UUID,
    session: DbSession,
) -> WebhookDeliveryRead:
    """Requeue a dead-letter delivery back to PENDING status."""
    delivery = await service.retry_webhook_delivery(session, delivery_id)
    return WebhookDeliveryRead.model_validate(delivery)


# ---------------------------------------------------------------------------
# Batch Import / Export & Job Audit History
# ---------------------------------------------------------------------------


@router.post(
    "/imports",
    response_model=BatchImportResponse,
    dependencies=[Depends(RequiresPermission("platform.integration.import"))],
)
async def batch_import_endpoint(
    payload: BatchImportRequest,
    session: DbSession,
) -> BatchImportResponse:
    """Execute a batch master data import (CSV or JSON) with optional dry-run."""
    job, errors = await batch.import_batch_data(
        session,
        payload.entity_type,
        payload.content,
        data_format=payload.format,
        dry_run=payload.dry_run,
    )
    return BatchImportResponse(
        job=IntegrationBatchJobRead.model_validate(job),
        errors=errors,
    )


@router.post(
    "/exports",
    response_model=BatchExportResponse,
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def batch_export_endpoint(
    payload: BatchExportRequest,
    session: DbSession,
) -> BatchExportResponse:
    """Execute a batch entity export (CSV or JSON) for the active tenant."""
    content, job = await batch.export_batch_data(
        session,
        payload.entity_type,
        data_format=payload.format,
        include_bom=payload.include_bom,
    )
    return BatchExportResponse(
        job=IntegrationBatchJobRead.model_validate(job),
        content=content,
    )


@router.get(
    "/jobs",
    response_model=list[IntegrationBatchJobRead],
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def list_batch_jobs_endpoint(
    session: DbSession,
    job_type: Annotated[
        str | None,
        Query(description="Filter by job type (IMPORT, EXPORT)"),
    ] = None,
    entity_type: Annotated[str | None, Query(description="Filter by entity type")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[IntegrationBatchJobRead]:
    """List batch import/export audit jobs for the active tenant."""
    jobs = await service.list_batch_jobs(
        session,
        job_type=job_type,
        entity_type=entity_type,
        limit=limit,
        offset=offset,
    )
    return [IntegrationBatchJobRead.model_validate(j) for j in jobs]


@router.get(
    "/jobs/{job_id}",
    response_model=IntegrationBatchJobRead,
    dependencies=[Depends(RequiresPermission("platform.integration.read"))],
)
async def get_batch_job_endpoint(
    job_id: uuid.UUID,
    session: DbSession,
) -> IntegrationBatchJobRead:
    """Retrieve a single batch job record by ID."""
    job = await service.get_batch_job(session, job_id)
    return IntegrationBatchJobRead.model_validate(job)
