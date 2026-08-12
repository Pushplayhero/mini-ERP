"""Multi-company tenancy context (master-plan §10.2).

Two pieces live here:

1. `_current_company_id`: a `contextvars.ContextVar` holding the active
   company for the current request. Set by `TenancyMiddleware` in
   `app.main` from a trusted header (see `Settings.tenant_header_name`),
   never from a client-supplied body field.

2. `TenantScopedMixin`: a *marker* mixin (not a full ORM base) that
   tenant-scoped models inherit from. `app.core.db` registers a
   `do_orm_execute` event that, for any SELECT touching a
   `TenantScopedMixin` subclass, injects a `with_loader_criteria` filter
   restricting rows to the active company — and raises `TenancyContextError`
   (fail-closed) if no company context is set, rather than silently
   returning zero or all rows.

This module intentionally has no FastAPI or SQLAlchemy-session-level wiring
beyond the mixin/contextvar primitives, so it stays testable and reusable
from plain scripts (e.g. the future outbox replay CLI, alembic env.py, etc).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.exceptions import TenancyContextError

if TYPE_CHECKING:
    pass

_current_company_id: ContextVar[uuid.UUID | None] = ContextVar("current_company_id", default=None)


def set_current_company_id(company_id: uuid.UUID) -> Token[uuid.UUID | None]:
    """Bind `company_id` as the active tenant for the current context (task).

    Returns a token that must be passed to `reset_current_company_id` when
    the scope ends (e.g. in a `finally` block), mirroring `ContextVar.reset`.
    """
    return _current_company_id.set(company_id)


def reset_current_company_id(token: Token[uuid.UUID | None]) -> None:
    _current_company_id.reset(token)


def current_company_id_or_none() -> uuid.UUID | None:
    """Return the active company id, or `None` if no context is bound.

    Prefer `require_current_company_id` in application/service code — this
    accessor exists for the low-level SQLAlchemy event hook, which needs to
    distinguish "no context" from "raise" at the point it decides whether a
    given statement even touches tenant-scoped data.
    """
    return _current_company_id.get()


def require_current_company_id() -> uuid.UUID:
    """Return the active company id, raising `TenancyContextError` if unset.

    Fail-closed per master-plan §10.2: callers must never fall back to "no
    filter" behaviour.
    """
    company_id = _current_company_id.get()
    if company_id is None:
        raise TenancyContextError(
            "No company context is set. Every tenant-scoped operation must "
            "run inside a bound company context (see app.core.tenancy)."
        )
    return company_id


class CompanyContext:
    """Context manager / convenience wrapper around set/reset for scripts and tests.

    Usage:
        with company_context(company_id):
            ...  # all tenant-scoped queries here are filtered to company_id
    """

    def __init__(self, company_id: uuid.UUID) -> None:
        self._company_id = company_id
        self._token: Token[uuid.UUID | None] | None = None

    def __enter__(self) -> uuid.UUID:
        self._token = set_current_company_id(self._company_id)
        return self._company_id

    def __exit__(self, *exc_info: object) -> None:
        assert self._token is not None
        reset_current_company_id(self._token)


# Lowercase alias: used call-site like `with company_context(company_id):`,
# mirroring `contextlib.contextmanager`-style naming for context managers
# even though this one is implemented as a class (ruff N801 exempted via
# this alias rather than a rule suppression).
company_context = CompanyContext


class TenantScopedMixin:
    """Marker mixin for models that belong to exactly one company.

    Do NOT mix this into `Company` itself (the tenant root) or into global
    reference data (currencies, UoM, exchange rates) that is intentionally
    shared across companies in Phase 1 — see mini-erp-architecture.md /
    README "Design Decisions" for the reasoning.
    """

    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
