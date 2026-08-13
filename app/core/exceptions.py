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


class DomainValidationError(AppError):
    """Input passed schema validation but violates a domain invariant (maps to HTTP 422).

    Used by `ledger` (ADR-005) for checks that are cheap to do before ever
    touching the database — e.g. an entry whose lines do not balance, or a
    line quoted in an unsupported currency — so the caller gets a precise
    422 instead of waiting for the DB constraint trigger (R1/Decision 2) to
    reject an unbalanced entry at commit time. The DB trigger remains the
    defense-in-depth backstop for any writer that bypasses this service.
    """


class PeriodNotOpenError(ConflictError):
    """Posting targets an accounting period that is closed or does not exist (HTTP 409).

    ADR-005 R4: `entry_date` must resolve to an `accounting_periods` row with
    `status = 'open'`. Subclasses `ConflictError` so it is handled by the
    same 409 handler without extra registration in `app.main`.
    """

    def __init__(self, year: int, month: int, *, reason: str) -> None:
        self.year = year
        self.month = month
        super().__init__(f"No open accounting period for {year:04d}-{month:02d} ({reason})")


class ReversalNotAllowedError(ConflictError):
    """A reversal was requested that ADR-005 R3 explicitly forbids (HTTP 409).

    Either the target entry has already been reversed once (`reversal_of_id`
    is `UNIQUE`), or the target entry is itself a reversal entry.
    """
