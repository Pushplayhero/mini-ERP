"""platform.workflow SQLAlchemy models — ADR-012 / Week 12 Slice B.

Maps:
- `workflow_definitions`: Tenant-defined approval flow specifications.
- `workflow_steps`: Ordered approval steps with amount thresholds and required permissions.
- `workflow_instances`: Concrete approval requests tracking snapshot steps and lifecycle states.
- `workflow_approval_logs`: Append-only audit logs recording decisions per step.

All tables inherit `TenantScopedMixin` (enforcing company_id FK + do_orm_execute auto-filtering)
and `TimestampAuditMixin` (created_at, updated_at, created_by, updated_by).
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampAuditMixin
from app.core.tenancy import TenantScopedMixin

_AMOUNT_PRECISION = Numeric(20, 6)

_VALID_ENTITY_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


class WorkflowDefinition(Base, TenantScopedMixin, TimestampAuditMixin):
    """Tenant-configured workflow definition governing state transitions."""

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_workflow_def_company_id"),
        UniqueConstraint(
            "company_id",
            "entity_type",
            "transition",
            name="uq_workflow_def_company_entity_transition",
        ),
        CheckConstraint(
            _VALID_ENTITY_TRANSITIONS_SQL,
            name="ck_workflow_def_entity_transition_valid",
        ),
        CheckConstraint("length(btrim(name)) > 0", name="ck_workflow_def_name_nonempty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    transition: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Explicit primaryjoin and foreign_keys prevent composite company_id conflicts
    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="definition",
        order_by="WorkflowStep.step_order",
        cascade="all, delete-orphan",
        primaryjoin="WorkflowDefinition.id == WorkflowStep.definition_id",
        foreign_keys="[WorkflowStep.definition_id]",
    )


class WorkflowStep(Base, TenantScopedMixin, TimestampAuditMixin):
    """A single sequential approval step belonging to a WorkflowDefinition."""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "definition_id"],
            ["workflow_definitions.company_id", "workflow_definitions.id"],
            ondelete="CASCADE",
            name="fk_workflow_step_company_def",
        ),
        UniqueConstraint("definition_id", "step_order", name="uq_workflow_step_def_order"),
        CheckConstraint("step_order >= 1", name="ck_workflow_step_order_positive"),
        CheckConstraint("min_amount >= 0", name="ck_workflow_step_min_amount_nonnegative"),
        CheckConstraint("length(btrim(step_name)) > 0", name="ck_workflow_step_name_nonempty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(120), nullable=False)
    min_amount: Mapped[Decimal] = mapped_column(
        _AMOUNT_PRECISION, nullable=False, default=Decimal("0.000000")
    )
    required_permission: Mapped[str] = mapped_column(String(100), nullable=False)

    definition: Mapped[WorkflowDefinition] = relationship(
        back_populates="steps",
        primaryjoin="WorkflowDefinition.id == WorkflowStep.definition_id",
        foreign_keys=[definition_id],
    )


class WorkflowInstance(Base, TenantScopedMixin, TimestampAuditMixin):
    """A concrete workflow instance tracking approval state for an entity transition."""

    __tablename__ = "workflow_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "definition_id"],
            ["workflow_definitions.company_id", "workflow_definitions.id"],
            ondelete="RESTRICT",
            name="fk_workflow_instance_company_def",
        ),
        UniqueConstraint("company_id", "id", name="uq_workflow_instance_company_id"),
        CheckConstraint(
            _VALID_ENTITY_TRANSITIONS_SQL,
            name="ck_workflow_instance_entity_transition_valid",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'CONSUMED')",
            name="ck_workflow_instance_status_valid",
        ),
        CheckConstraint(
            "target_amount >= 0",
            name="ck_workflow_instance_target_amount_nonnegative",
        ),
        CheckConstraint(
            "jsonb_typeof(steps_snapshot) = 'array'",
            name="ck_workflow_instance_steps_snapshot_is_array",
        ),
        CheckConstraint(
            "(status = 'PENDING') = (current_step_order IS NOT NULL)",
            name="ck_workflow_instance_pending_has_step",
        ),
        CheckConstraint(
            "(status = 'CONSUMED') = (consumed_at IS NOT NULL)",
            name="ck_workflow_instance_consumed_timestamp",
        ),
        Index(
            "uq_workflow_instance_active",
            "company_id",
            "entity_type",
            "entity_id",
            "transition",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'APPROVED')"),
        ),
        Index(
            "ix_workflow_instances_lookup",
            "company_id",
            "entity_type",
            "entity_id",
            "transition",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    transition: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(_AMOUNT_PRECISION, nullable=False)
    current_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steps_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    definition: Mapped[WorkflowDefinition] = relationship(
        primaryjoin="WorkflowDefinition.id == WorkflowInstance.definition_id",
        foreign_keys=[definition_id],
    )
    logs: Mapped[list[WorkflowApprovalLog]] = relationship(
        back_populates="instance",
        order_by="WorkflowApprovalLog.step_order",
        cascade="all, delete-orphan",
        primaryjoin="WorkflowInstance.id == WorkflowApprovalLog.instance_id",
        foreign_keys="[WorkflowApprovalLog.instance_id]",
    )


class WorkflowApprovalLog(Base, TenantScopedMixin, TimestampAuditMixin):
    """Audit log entry recording an approval/rejection decision on a workflow step."""

    __tablename__ = "workflow_approval_logs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "instance_id"],
            ["workflow_instances.company_id", "workflow_instances.id"],
            ondelete="RESTRICT",
            name="fk_workflow_log_company_instance",
        ),
        UniqueConstraint("instance_id", "step_order", name="uq_workflow_log_instance_step"),
        CheckConstraint("step_order >= 1", name="ck_workflow_log_step_order_positive"),
        CheckConstraint("length(btrim(step_name)) > 0", name="ck_workflow_log_step_name_nonempty"),
        CheckConstraint("decision IN ('APPROVE', 'REJECT')", name="ck_workflow_log_decision_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(120), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    instance: Mapped[WorkflowInstance] = relationship(
        back_populates="logs",
        primaryjoin="WorkflowInstance.id == WorkflowApprovalLog.instance_id",
        foreign_keys=[instance_id],
    )
