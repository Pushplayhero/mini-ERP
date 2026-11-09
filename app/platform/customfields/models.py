"""platform.customfields SQLAlchemy models — ADR-011 / Week 11 Slice B.

Maps `custom_field_definitions` table representing tenant-defined custom field schemas.
Inherits `TenantScopedMixin` (enforcing company_id FK + do_orm_execute auto-filtering)
and `TimestampAuditMixin` (created_at, updated_at, created_by, updated_by).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin


class CustomFieldDefinition(Base, TenantScopedMixin, TimestampAuditMixin):
    """Tenant-defined custom field specification for a business entity."""

    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "entity_type",
            "field_key",
            name="uq_custom_field_company_entity_key",
        ),
        CheckConstraint(
            "field_key ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_custom_field_key_format",
        ),
        CheckConstraint(
            "entity_type IN ('customer', 'product', 'account', 'accounting_period', "
            "'journal_entry', 'sales_order', 'invoice', 'payment')",
            name="ck_custom_field_entity_type_valid",
        ),
        CheckConstraint(
            "field_type IN ('TEXT', 'INTEGER', 'DECIMAL', 'BOOLEAN', 'DATE', 'ENUM')",
            name="ck_custom_field_type_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(validation_rules) = 'object'",
            name="ck_custom_field_validation_rules_is_object",
        ),
        CheckConstraint(
            "length(btrim(label)) > 0",
            name="ck_custom_field_label_nonempty",
        ),
        Index(
            "ix_custom_field_definitions_company_entity",
            "company_id",
            "entity_type",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    field_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_value: Mapped[Any | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, default=None
    )
    validation_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
