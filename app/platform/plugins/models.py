"""platform.plugins SQLAlchemy models — ADR-010 / Week 10 Slice B.

Maps `company_plugins` table representing per-company plugin state and configuration.
Inherits `TenantScopedMixin` (enforcing company_id FK + do_orm_execute auto-filtering)
and `TimestampAuditMixin` (created_at, updated_at, created_by, updated_by).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin


class CompanyPlugin(Base, TenantScopedMixin, TimestampAuditMixin):
    """Per-company enablement state and configuration for a loaded plugin."""

    __tablename__ = "company_plugins"
    __table_args__ = (
        UniqueConstraint("company_id", "plugin_name", name="uq_company_plugins_company_plugin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plugin_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
