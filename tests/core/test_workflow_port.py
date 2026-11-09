"""Unit tests for the workflow Core Port (ADR-012 / Week 12 Slice A)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, DomainValidationError
from app.core.workflow import (
    VALID_ENTITY_TRANSITIONS,
    WorkflowApprovalGate,
    WorkflowApprovalGatePort,
    WorkflowApprovalRequiredError,
    WorkflowDecision,
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTargetResolver,
    WorkflowTargetResolverPort,
    WorkflowTransition,
    get_workflow_approval_gate,
    get_workflow_target_resolver,
    is_valid_entity_transition,
    resolve_workflow_target,
    set_workflow_approval_gate,
    set_workflow_target_resolver,
    validate_entity_transition,
    verify_workflow_approval,
)


@pytest.fixture(autouse=True)
def _reset_workflow_ports() -> Any:
    """Ensure workflow ports are reset before and after each test."""
    orig_gate = get_workflow_approval_gate()
    orig_resolver = get_workflow_target_resolver()
    set_workflow_approval_gate(None)
    set_workflow_target_resolver(None)
    yield
    set_workflow_approval_gate(orig_gate)
    set_workflow_target_resolver(orig_resolver)


def test_workflow_enums() -> None:
    assert len(WorkflowEntityType) == 4
    assert WorkflowEntityType.SALES_ORDER.value == "sales_order"
    assert WorkflowEntityType.INVOICE.value == "invoice"
    assert WorkflowEntityType.PAYMENT.value == "payment"
    assert WorkflowEntityType.JOURNAL_ENTRY.value == "journal_entry"

    assert len(WorkflowTransition) == 4
    assert WorkflowTransition.CONFIRM.value == "confirm"
    assert WorkflowTransition.CANCEL.value == "cancel"
    assert WorkflowTransition.VOID.value == "void"
    assert WorkflowTransition.REVERSE.value == "reverse"

    assert len(WorkflowInstanceStatus) == 5
    assert WorkflowInstanceStatus.PENDING.value == "PENDING"
    assert WorkflowInstanceStatus.APPROVED.value == "APPROVED"
    assert WorkflowInstanceStatus.REJECTED.value == "REJECTED"
    assert WorkflowInstanceStatus.CANCELLED.value == "CANCELLED"
    assert WorkflowInstanceStatus.CONSUMED.value == "CONSUMED"

    assert len(WorkflowDecision) == 2
    assert WorkflowDecision.APPROVE.value == "APPROVE"
    assert WorkflowDecision.REJECT.value == "REJECT"


def test_valid_entity_transitions() -> None:
    expected = {
        ("sales_order", "confirm"),
        ("sales_order", "cancel"),
        ("invoice", "void"),
        ("payment", "void"),
        ("journal_entry", "reverse"),
    }
    assert expected == VALID_ENTITY_TRANSITIONS

    for e, t in expected:
        assert is_valid_entity_transition(e, t) is True
        e_enum, t_enum = validate_entity_transition(e, t)
        assert e_enum.value == e
        assert t_enum.value == t

    assert is_valid_entity_transition("sales_order", "void") is False
    assert is_valid_entity_transition("invoice", "confirm") is False
    assert is_valid_entity_transition("unknown", "confirm") is False

    with pytest.raises(DomainValidationError, match="Invalid workflow entity transition"):
        validate_entity_transition("unknown_entity", "confirm")

    with pytest.raises(DomainValidationError, match="Unsupported workflow entity transition"):
        validate_entity_transition("sales_order", "void")


def test_workflow_exception_hierarchy() -> None:
    assert issubclass(WorkflowApprovalRequiredError, ConflictError)
    err = WorkflowApprovalRequiredError("Sales order SO-001 requires approval before confirm")
    assert isinstance(err, ConflictError)


def test_port_aliases() -> None:
    assert WorkflowApprovalGatePort is WorkflowApprovalGate
    assert WorkflowTargetResolverPort is WorkflowTargetResolver


@pytest.mark.asyncio
async def test_verify_workflow_approval_no_gate_registered() -> None:
    """When no gate is registered, acts as a transparent no-op."""
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    # Should succeed without raising
    await verify_workflow_approval(
        dummy_session,
        entity_type="sales_order",
        entity_id=order_id,
        transition="confirm",
        amount=Decimal("100.50"),
    )


@pytest.mark.asyncio
async def test_verify_workflow_approval_validates_inputs() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    # Invalid transition
    with pytest.raises(DomainValidationError, match="Unsupported workflow entity transition"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="void",
            amount=Decimal("100.00"),
        )

    # Invalid amount types
    with pytest.raises(DomainValidationError, match="must be a Decimal instance"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=True,  # type: ignore[arg-type]
        )

    with pytest.raises(DomainValidationError, match="must be a Decimal instance"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=100.5,  # type: ignore[arg-type]
        )

    # Negative and non-finite amounts
    with pytest.raises(DomainValidationError, match="must be a finite Decimal"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("NaN"),
        )

    with pytest.raises(DomainValidationError, match="must be a finite Decimal"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("Infinity"),
        )

    with pytest.raises(DomainValidationError, match="must be non-negative"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("-1.00"),
        )


@pytest.mark.asyncio
async def test_verify_workflow_approval_delegates_to_registered_gate() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()
    called = False

    class MockGate(WorkflowApprovalGate):
        async def __call__(
            self,
            session: AsyncSession,
            *,
            entity_type: str,
            entity_id: uuid.UUID,
            transition: str,
            amount: Decimal,
        ) -> None:
            nonlocal called
            called = True
            assert session is dummy_session
            assert entity_type == "sales_order"
            assert entity_id == order_id
            assert transition == "confirm"
            assert amount == Decimal("5000.00")

    gate = MockGate()
    set_workflow_approval_gate(gate)
    assert get_workflow_approval_gate() is gate

    await verify_workflow_approval(
        dummy_session,
        entity_type="sales_order",
        entity_id=order_id,
        transition="confirm",
        amount=Decimal("5000.00"),
    )
    assert called is True


@pytest.mark.asyncio
async def test_verify_workflow_approval_propagates_approval_required_error() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    class RejectingGate(WorkflowApprovalGate):
        async def __call__(
            self,
            session: AsyncSession,
            *,
            entity_type: str,
            entity_id: uuid.UUID,
            transition: str,
            amount: Decimal,
        ) -> None:
            _ = (session, amount)
            raise WorkflowApprovalRequiredError(
                f"Transition {transition} on {entity_type} {entity_id} requires pending approval"
            )

    set_workflow_approval_gate(RejectingGate())

    with pytest.raises(WorkflowApprovalRequiredError, match="requires pending approval"):
        await verify_workflow_approval(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("15000.00"),
        )


@pytest.mark.asyncio
async def test_resolve_workflow_target_fail_closed_without_resolver() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    with pytest.raises(DomainValidationError, match="Workflow target resolver is not registered"):
        await resolve_workflow_target(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
        )


@pytest.mark.asyncio
async def test_resolve_workflow_target_delegates_to_registered_resolver() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    class MockResolver(WorkflowTargetResolver):
        async def __call__(
            self,
            session: AsyncSession,
            *,
            entity_type: str,
            entity_id: uuid.UUID,
            transition: str,
        ) -> Decimal:
            assert session is dummy_session
            assert entity_type == "sales_order"
            assert entity_id == order_id
            assert transition == "confirm"
            return Decimal("12345.670000")

    resolver = MockResolver()
    set_workflow_target_resolver(resolver)
    assert get_workflow_target_resolver() is resolver

    amt = await resolve_workflow_target(
        dummy_session,
        entity_type="sales_order",
        entity_id=order_id,
        transition="confirm",
    )
    assert amt == Decimal("12345.670000")


@pytest.mark.asyncio
async def test_resolve_workflow_target_validates_resolver_output() -> None:
    dummy_session = AsyncMock(spec=AsyncSession)
    order_id = uuid.uuid4()

    class BoolResolver(WorkflowTargetResolver):
        async def __call__(self, *_args: Any, **_kwargs: Any) -> Decimal:
            return True  # type: ignore[return-value]

    set_workflow_target_resolver(BoolResolver())
    with pytest.raises(DomainValidationError, match="must be a Decimal"):
        await resolve_workflow_target(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
        )

    class NanResolver(WorkflowTargetResolver):
        async def __call__(self, *_args: Any, **_kwargs: Any) -> Decimal:
            return Decimal("NaN")

    set_workflow_target_resolver(NanResolver())
    with pytest.raises(DomainValidationError, match="must be a finite Decimal"):
        await resolve_workflow_target(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
        )

    class NegativeResolver(WorkflowTargetResolver):
        async def __call__(self, *_args: Any, **_kwargs: Any) -> Decimal:
            return Decimal("-10.00")

    set_workflow_target_resolver(NegativeResolver())
    with pytest.raises(DomainValidationError, match="must be non-negative"):
        await resolve_workflow_target(
            dummy_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
        )
