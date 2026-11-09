"""Workflow core port — ADR-012 / Week 12 Slice A.

Defines:
- Supported business entities (`WorkflowEntityType`)
- Supported state transitions (`WorkflowTransition`)
- Canonical valid transition pairs (`VALID_ENTITY_TRANSITIONS`)
- Workflow instance statuses (`WorkflowInstanceStatus`)
- Workflow step approval decisions (`WorkflowDecision`)
- Domain exceptions (`WorkflowApprovalRequiredError`)
- Runtime protocols and port functions:
  - `WorkflowApprovalGate` (`WorkflowApprovalGatePort`)
  - `WorkflowTargetResolver` (`WorkflowTargetResolverPort`)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, DomainValidationError


class WorkflowEntityType(StrEnum):
    """Tenant-scoped business entities supporting approval workflow gating."""

    SALES_ORDER = "sales_order"
    INVOICE = "invoice"
    PAYMENT = "payment"
    JOURNAL_ENTRY = "journal_entry"


class WorkflowTransition(StrEnum):
    """Business state transitions supporting workflow gating."""

    CONFIRM = "confirm"
    CANCEL = "cancel"
    VOID = "void"
    REVERSE = "reverse"


class WorkflowInstanceStatus(StrEnum):
    """Lifecycle statuses of a workflow approval instance."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CONSUMED = "CONSUMED"


class WorkflowDecision(StrEnum):
    """Approval action decisions on a workflow step."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


VALID_ENTITY_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (WorkflowEntityType.SALES_ORDER.value, WorkflowTransition.CONFIRM.value),
        (WorkflowEntityType.SALES_ORDER.value, WorkflowTransition.CANCEL.value),
        (WorkflowEntityType.INVOICE.value, WorkflowTransition.VOID.value),
        (WorkflowEntityType.PAYMENT.value, WorkflowTransition.VOID.value),
        (WorkflowEntityType.JOURNAL_ENTRY.value, WorkflowTransition.REVERSE.value),
    }
)


def is_valid_entity_transition(entity_type: str, transition: str) -> bool:
    """Return True if (entity_type, transition) is a recognized workflow transition."""
    return (entity_type, transition) in VALID_ENTITY_TRANSITIONS


def validate_entity_transition(
    entity_type: str, transition: str
) -> tuple[WorkflowEntityType, WorkflowTransition]:
    """Validate and return enum instances of (entity_type, transition).

    Raises DomainValidationError if the pair is invalid or unsupported.
    """
    try:
        e_type = WorkflowEntityType(entity_type)
        t_type = WorkflowTransition(transition)
    except ValueError as exc:
        raise DomainValidationError(
            f"Invalid workflow entity transition ({entity_type!r}, {transition!r})"
        ) from exc

    if (e_type.value, t_type.value) not in VALID_ENTITY_TRANSITIONS:
        raise DomainValidationError(
            f"Unsupported workflow entity transition ({e_type.value!r}, {t_type.value!r})"
        )
    return e_type, t_type


class WorkflowApprovalRequiredError(ConflictError):
    """Raised when an operation requires an approved workflow instance that is missing/unapproved.

    Subclasses ConflictError so it automatically maps to HTTP 409 Conflict.
    """


class WorkflowApprovalGate(Protocol):
    """Protocol for verifying that an approved workflow instance exists before state transition."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        transition: str,
        amount: Decimal,
    ) -> None:
        """Verify workflow approval and consume the approved instance atomically."""
        ...


class WorkflowTargetResolver(Protocol):
    """Protocol for resolving authoritative target entity information and amount."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        transition: str,
    ) -> Decimal:
        """Query and return the authoritative server-side Decimal amount for the target entity."""
        ...


WorkflowApprovalGatePort = WorkflowApprovalGate
WorkflowTargetResolverPort = WorkflowTargetResolver

_gate: WorkflowApprovalGate | None = None
_resolver: WorkflowTargetResolver | None = None


def set_workflow_approval_gate(gate: WorkflowApprovalGate | None) -> None:
    """Register the runtime approval gate implementation (called at composition root)."""
    global _gate
    _gate = gate


def get_workflow_approval_gate() -> WorkflowApprovalGate | None:
    """Return the currently registered approval gate, if any."""
    return _gate


def set_workflow_target_resolver(resolver: WorkflowTargetResolver | None) -> None:
    """Register the runtime target resolver implementation (called at composition root)."""
    global _resolver
    _resolver = resolver


def get_workflow_target_resolver() -> WorkflowTargetResolver | None:
    """Return the currently registered target resolver, if any."""
    return _resolver


async def verify_workflow_approval(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    transition: str,
    amount: Decimal,
) -> None:
    """Verify that any required workflow approval for (entity_type, transition) is satisfied.

    If an approval gate is registered by platform.workflow, delegates verification
    and atomic consumption to it. If no gate is registered (e.g. standalone test runs),
    runs parameter validation and acts as a safe no-op.
    """
    e_type, t_type = validate_entity_transition(entity_type, transition)
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise DomainValidationError(
            f"Workflow amount must be a Decimal instance, got {type(amount).__name__}"
        )
    if not amount.is_finite():
        raise DomainValidationError(f"Workflow amount must be a finite Decimal, got {amount}")
    if amount < Decimal("0"):
        raise DomainValidationError(f"Workflow amount must be non-negative, got {amount}")

    if _gate is not None:
        await _gate(
            session,
            entity_type=e_type.value,
            entity_id=entity_id,
            transition=t_type.value,
            amount=amount,
        )


async def resolve_workflow_target(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    transition: str,
) -> Decimal:
    """Resolve the authoritative server-side amount for a target entity through registered resolver.

    Fail-closed: raises DomainValidationError if no resolver is registered.
    """
    e_type, t_type = validate_entity_transition(entity_type, transition)
    if _resolver is None:
        raise DomainValidationError("Workflow target resolver is not registered")

    amount = await _resolver(
        session,
        entity_type=e_type.value,
        entity_id=entity_id,
        transition=t_type.value,
    )
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise DomainValidationError(
            f"Resolved workflow target amount must be a Decimal, got {type(amount).__name__}"
        )
    if not amount.is_finite():
        raise DomainValidationError(
            f"Resolved workflow target amount must be a finite Decimal, got {amount}"
        )
    if amount < Decimal("0"):
        raise DomainValidationError(
            f"Resolved workflow target amount must be non-negative, got {amount}"
        )
    return amount
