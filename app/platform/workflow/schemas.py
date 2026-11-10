"""platform.workflow Pydantic schemas — ADR-012 / Week 12 Slice B.

Defines API DTOs, amount quantization (Banker's rounding), and structure validation
for workflow definitions, steps, instances, and approval logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.exceptions import DomainValidationError
from app.core.workflow import (
    VALID_ENTITY_TRANSITIONS,
    WorkflowEntityType,
    WorkflowTransition,
    is_valid_entity_transition,
)

_DECIMAL_QUANTUM = Decimal("0.000001")
_MAX_DECIMAL_EXCLUSIVE = Decimal("100000000000000")  # 10^14


def parse_and_quantize_amount(value: Any, context_name: str = "amount") -> Decimal:
    """Parse an amount and quantize via Banker's Rounding (ROUND_HALF_EVEN).

    Rejects floats, booleans, NaNs, infinities, negatives, and >= 10^14.
    """
    if isinstance(value, bool):
        raise DomainValidationError(
            f"{context_name} must be a valid decimal number, got boolean {value!r}"
        )
    if isinstance(value, float):
        raise DomainValidationError(
            f"{context_name} rejects float {value!r} to avoid binary floating-point inaccuracy. "
            "Pass as string, int, or Decimal."
        )
    try:
        dec = Decimal(str(value) if not isinstance(value, Decimal) else value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainValidationError(
            f"{context_name} is not a valid decimal number: {value!r}"
        ) from exc

    if not dec.is_finite():
        raise DomainValidationError(f"{context_name} cannot be NaN or Infinity, got {value!r}")

    rounded = dec.quantize(_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)
    if rounded < Decimal("0"):
        raise DomainValidationError(f"{context_name} must be non-negative, got {rounded}")
    if rounded >= _MAX_DECIMAL_EXCLUSIVE:
        raise DomainValidationError(
            f"{context_name} out of bounds: {rounded} must be strictly less than 10^14"
        )
    return rounded


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class WorkflowStepInput(BaseModel):
    """Input payload for a workflow step within definition create/update."""

    model_config = ConfigDict(extra="forbid")

    step_order: int = Field(ge=1, description="1-based sequential step order")
    step_name: str = Field(min_length=1, max_length=120)
    min_amount: Decimal = Field(default=Decimal("0.000000"))
    required_permission: str = Field(min_length=1, max_length=100)

    @field_validator("step_name")
    @classmethod
    def validate_step_name_nonempty(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("step_name cannot be whitespace only")
        return trimmed

    @field_validator("required_permission")
    @classmethod
    def validate_required_permission_nonempty(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("required_permission cannot be whitespace only")
        return trimmed

    @field_validator("min_amount", mode="before")
    @classmethod
    def validate_min_amount(cls, v: Any) -> Decimal:
        return parse_and_quantize_amount(v, "min_amount")


class WorkflowStepRead(BaseModel):
    """Read representation of an persisted workflow step."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    definition_id: uuid.UUID
    step_order: int
    step_name: str
    min_amount: Decimal
    required_permission: str
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Workflow Definitions
# ---------------------------------------------------------------------------


def _validate_steps_sequence(steps: list[WorkflowStepInput]) -> None:
    if not steps:
        raise DomainValidationError("Workflow definition must have at least one step")
    # Verify strict 1..N order
    for idx, step in enumerate(steps, start=1):
        if step.step_order != idx:
            raise DomainValidationError(
                f"Workflow steps must be numbered sequentially 1..{len(steps)}, "
                f"found step_order={step.step_order} at position {idx}"
            )


class WorkflowDefinitionCreate(BaseModel):
    """Payload for creating a new workflow definition."""

    model_config = ConfigDict(extra="forbid")

    entity_type: WorkflowEntityType
    transition: WorkflowTransition
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    allow_self_approval: bool = False
    steps: list[WorkflowStepInput] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name_nonempty(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name cannot be whitespace only")
        return trimmed

    @model_validator(mode="after")
    def validate_entity_transition_and_steps(self) -> WorkflowDefinitionCreate:
        if not is_valid_entity_transition(self.entity_type, self.transition):
            valid_pairs = sorted(
                (
                    e.value if isinstance(e, WorkflowEntityType) else str(e),
                    t.value if isinstance(t, WorkflowTransition) else str(t),
                )
                for e, t in VALID_ENTITY_TRANSITIONS
            )
            raise DomainValidationError(
                f"Invalid workflow entity_type and transition combination: "
                f"({self.entity_type.value}, {self.transition.value}). "
                f"Valid combinations are: {valid_pairs}"
            )
        _validate_steps_sequence(self.steps)
        return self


class WorkflowDefinitionUpdate(BaseModel):
    """Payload for updating mutable attributes of an existing workflow definition."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    allow_self_approval: bool | None = None
    steps: list[WorkflowStepInput] | None = None

    @field_validator("name")
    @classmethod
    def validate_name_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name cannot be whitespace only")
        return trimmed

    @model_validator(mode="after")
    def validate_update_payload(self) -> WorkflowDefinitionUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in update payload")
        for non_null_field in ("name", "is_active", "allow_self_approval"):
            if non_null_field in self.model_fields_set and getattr(self, non_null_field) is None:
                raise ValueError(f"{non_null_field} cannot be null")
        if "steps" in self.model_fields_set:
            if self.steps is None or len(self.steps) == 0:
                raise DomainValidationError("steps cannot be empty when provided")
            _validate_steps_sequence(self.steps)
        return self


class WorkflowDefinitionRead(BaseModel):
    """Read representation of a workflow definition."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    entity_type: str
    transition: str
    name: str
    description: str | None = None
    is_active: bool
    allow_self_approval: bool
    steps: list[WorkflowStepRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Workflow Instances & Logs
# ---------------------------------------------------------------------------


class WorkflowInstanceSubmit(BaseModel):
    """Payload for submitting an entity transition into workflow approval."""

    model_config = ConfigDict(extra="forbid")

    entity_type: WorkflowEntityType
    entity_id: uuid.UUID
    transition: WorkflowTransition

    @model_validator(mode="after")
    def validate_entity_transition(self) -> WorkflowInstanceSubmit:
        if not is_valid_entity_transition(self.entity_type, self.transition):
            raise DomainValidationError(
                f"Invalid workflow entity_type and transition combination: "
                f"({self.entity_type.value}, {self.transition.value})."
            )
        return self


class WorkflowDecisionInput(BaseModel):
    """Payload for approve/reject decision on a pending workflow instance."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=1000)


class WorkflowApprovalLogRead(BaseModel):
    """Read representation of an immutable workflow approval log entry."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    instance_id: uuid.UUID
    step_order: int
    step_name: str
    decision: str
    actor_id: uuid.UUID
    comment: str | None = None
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None


class WorkflowInstanceRead(BaseModel):
    """Read representation of a workflow instance."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    definition_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    transition: str
    status: str
    target_amount: Decimal
    current_step_order: int | None = None
    steps_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    requested_by: uuid.UUID
    consumed_at: datetime | None = None
    consumed_by: uuid.UUID | None = None
    logs: list[WorkflowApprovalLogRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None
