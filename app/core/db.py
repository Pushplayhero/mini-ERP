"""Async SQLAlchemy 2.0 session management + the multi-company ORM filter.

Provides:
- `Base`: declarative base every mapped model inherits from.
- `TimestampAuditMixin` / `CustomDataMixin`: the "foundation columns" every
  transactional master-data table gets per master-plan §2.4/§2.8.
- `get_engine` / `get_session_factory` / `get_db_session`: async session
  plumbing for the running application (FastAPI dependency).
- The `do_orm_execute` event that enforces master-plan §10.2: any SELECT
  touching a `TenantScopedMixin` subclass is automatically filtered to the
  active company context, and raises fail-closed if no context is bound.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    mapped_column,
    with_loader_criteria,
)

from app.core.exceptions import TenancyContextError
from app.core.settings import get_settings
from app.core.tenancy import TenantScopedMixin, current_company_id_or_none


class Base(DeclarativeBase):
    """Declarative base for all mapped models in the application."""


class TimestampAuditMixin:
    """`created_at / updated_at / created_by / updated_by` — master-plan §2.8."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class CustomDataMixin:
    """`custom_data JSONB` — master-plan §2.4 (customization-without-forking)."""

    custom_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


# ---------------------------------------------------------------------------
# Multi-company enforcement (master-plan §10.2)
# ---------------------------------------------------------------------------
#
# Registered on the base `Session` class rather than a specific instance:
# `AsyncSession` delegates statement execution to a plain `Session` running
# in the greenlet/thread underneath, so `do_orm_execute` fires for async
# queries too — this is the documented SQLAlchemy pattern for combining
# `with_loader_criteria`-based row filtering with `AsyncSession`.
@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_scope(orm_execute_state: ORMExecuteState) -> None:
    if not orm_execute_state.is_select:
        return

    touches_tenant_data = any(
        issubclass(mapper.class_, TenantScopedMixin) for mapper in orm_execute_state.all_mappers
    )
    if not touches_tenant_data:
        return

    company_id = current_company_id_or_none()
    if company_id is None:
        raise TenancyContextError(
            "Query touches tenant-scoped entity "
            f"({[m.class_.__name__ for m in orm_execute_state.all_mappers]}) "
            "without an active company context. Fail-closed per "
            "master-plan §10.2 — refusing to run unfiltered."
        )

    orm_execute_state.statement = orm_execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.company_id == company_id,
            include_aliases=True,
        )
    )


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------
# Lazily constructed (not a module-level singleton bound at import time) so
# tests can point at an ephemeral database without import-order surprises.

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped `AsyncSession`."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the module-level engine (used on app shutdown / test teardown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
