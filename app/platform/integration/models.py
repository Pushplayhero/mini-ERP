"""platform.integration SQLAlchemy models — ADR-013 / Week 13 Slice B.

Maps:
- `webhook_endpoints`: Tenant-configured webhook subscription targets.
- `integration_outbox_receipts`: Deduplication watermark receipts for outbox fan-out.
- `webhook_deliveries`: Delivery attempts, retry schedules, and DLQ status per endpoint.
- `integration_batch_jobs`: Audit history for batch import and export executions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin


class WebhookEndpoint(Base, TenantScopedMixin, TimestampAuditMixin):
    """Tenant webhook endpoint registration."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_webhook_endpoints_company_id"),
        UniqueConstraint("company_id", "name", name="uq_webhook_endpoints_company_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_webhook_endpoints_name_nonempty"),
        CheckConstraint("length(btrim(url)) > 0", name="ck_webhook_endpoints_url_nonempty"),
        CheckConstraint(
            "timeout_seconds >= 1.00 AND timeout_seconds <= 60.00",
            name="ck_webhook_endpoints_timeout",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 20",
            name="ck_webhook_endpoints_max_attempts",
        ),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_webhook_endpoints_company",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    signing_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_signing_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    secret_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_seconds: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)


class IntegrationOutboxReceipt(Base):
    """Processed outbox event marker ensuring exactly-once delivery fanout."""

    __tablename__ = "integration_outbox_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["outbox_id"],
            ["outbox.id"],
            ondelete="CASCADE",
            name="fk_integration_outbox_receipts_outbox",
        ),
    )

    outbox_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class WebhookDelivery(Base):
    """Scheduled and historical webhook HTTP delivery attempts.

    Intentionally omits `TenantScopedMixin` so the cross-tenant outbox worker
    can claim due rows across companies without ambient `company_context`.
    Tenant isolation is preserved via composite FK `(company_id, endpoint_id)`,
    explicit `company_id == require_current_company_id()` filtering in all
    service queries, and bound `company_context(claimed.company_id)` on dispatch.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "endpoint_id"],
            ["webhook_endpoints.company_id", "webhook_endpoints.id"],
            ondelete="CASCADE",
            name="fk_webhook_deliveries_company_endpoint",
        ),
        ForeignKeyConstraint(
            ["outbox_event_id"],
            ["outbox.id"],
            ondelete="CASCADE",
            name="fk_webhook_deliveries_outbox",
        ),
        UniqueConstraint(
            "company_id",
            "endpoint_id",
            "outbox_event_id",
            name="uq_webhook_deliveries_idempotency",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'DEAD')",
            name="ck_webhook_deliveries_status",
        ),
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('PENDING', 'IN_PROGRESS')"),
        ),
        Index(
            "ix_webhook_deliveries_endpoint",
            "company_id",
            "endpoint_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    outbox_event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class IntegrationBatchJob(Base, TenantScopedMixin, TimestampAuditMixin):
    """Audit log of asynchronous batch data import and export jobs."""

    __tablename__ = "integration_batch_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_integration_batch_jobs_company",
        ),
        CheckConstraint(
            "job_type IN ('IMPORT', 'EXPORT')",
            name="ck_integration_batch_jobs_type",
        ),
        CheckConstraint(
            "format IN ('CSV', 'JSON')",
            name="ck_integration_batch_jobs_format",
        ),
        CheckConstraint(
            "status IN ('COMPLETED', 'FAILED', 'DRY_RUN')",
            name="ck_integration_batch_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="CSV")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
