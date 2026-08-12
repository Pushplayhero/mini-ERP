"""Core exception hierarchy.

Kept in `app.core` (not per-module) so both the kernel and business modules can
raise/catch a shared vocabulary, and so `app.main` can register a single set of
FastAPI exception handlers.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application-raised errors."""


class TenancyContextError(AppError):
    """Raised when a query touches tenant-scoped data without a company context.

    This is the fail-closed guard required by master-plan §10.2: an unset
    company context must never silently return an unfiltered (or empty)
    result set — it must raise.
    """


class NotFoundError(AppError):
    """Requested entity does not exist (or is not visible in the current tenant)."""

    def __init__(self, entity: str, entity_id: object) -> None:
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} {entity_id!r} not found")


class ConflictError(AppError):
    """A uniqueness/state constraint was violated (maps to HTTP 409)."""
