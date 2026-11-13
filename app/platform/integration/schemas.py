"""platform.integration Pydantic schemas — ADR-013 / Week 13 Slice B.

Defines API DTOs and validation rules for:
- Webhook endpoints (create, update, read, created-read, secret-rotate).
- Webhook delivery attempts and status records.
- Batch import/export job records and requests/responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.integration import BatchDataFormat, BatchEntityType


class WebhookEndpointCreate(BaseModel):
    """Payload to register a new tenant webhook endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="Endpoint identifier name")
    url: str = Field(min_length=1, max_length=2048, description="Target destination URL")
    event_types: list[str] = Field(
        default_factory=list,
        description="Subscribed event types or empty/['*'] for all events",
    )
    timeout_seconds: Decimal | None = Field(
        default=None,
        ge=Decimal("1.00"),
        le=Decimal("60.00"),
        description="Per-delivery HTTP timeout in seconds (defaults to server setting)",
    )
    max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum retry attempts before routing to dead-letter queue",
    )


class WebhookEndpointUpdate(BaseModel):
    """Payload to update an existing webhook endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_types: list[str] | None = None
    is_active: bool | None = None
    timeout_seconds: Decimal | None = Field(
        default=None,
        ge=Decimal("1.00"),
        le=Decimal("60.00"),
    )
    max_attempts: int | None = Field(
        default=None,
        ge=1,
        le=20,
    )


class WebhookEndpointRead(BaseModel):
    """Public tenant view of a webhook endpoint (secret masked/prefix only)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    url: str
    secret_prefix: str
    previous_secret_expires_at: datetime | None = None
    event_types: list[str]
    is_active: bool
    timeout_seconds: Decimal
    max_attempts: int
    created_at: datetime
    updated_at: datetime


class WebhookEndpointCreatedRead(WebhookEndpointRead):
    """Returned once upon endpoint creation: includes the plaintext signing secret."""

    signing_secret: str


class WebhookEndpointSecretRotateRead(BaseModel):
    """Returned upon secret rotation: includes the newly generated plaintext secret."""

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: uuid.UUID
    signing_secret: str
    secret_prefix: str
    previous_secret_expires_at: datetime | None = None


class WebhookDeliveryRead(BaseModel):
    """Public tenant view of a webhook delivery attempt."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    endpoint_id: uuid.UUID
    outbox_event_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_until: datetime | None = None
    last_http_status: int | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IntegrationBatchJobRead(BaseModel):
    """Public tenant view of a batch import or export job."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    job_type: str
    entity_type: str
    format: str
    status: str
    dry_run: bool
    total_rows: int
    succeeded_rows: int
    failed_rows: int
    error_summary: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class BatchImportRequest(BaseModel):
    """Payload to trigger a batch entity import."""

    model_config = ConfigDict(extra="forbid")

    entity_type: BatchEntityType
    format: BatchDataFormat = BatchDataFormat.CSV
    dry_run: bool = False
    content: str = Field(
        min_length=1,
        max_length=1_048_576,
        description="CSV text or JSON array content",
    )


class BatchImportResponse(BaseModel):
    """Result envelope for HTTP batch entity import."""

    job: IntegrationBatchJobRead
    errors: list[dict[str, Any]]


class BatchExportRequest(BaseModel):
    """Payload to trigger a batch entity export."""

    model_config = ConfigDict(extra="forbid")

    entity_type: BatchEntityType
    format: BatchDataFormat = BatchDataFormat.CSV
    include_bom: bool = False


class BatchExportResponse(BaseModel):
    """Result envelope for HTTP batch entity export."""

    job: IntegrationBatchJobRead
    content: str
