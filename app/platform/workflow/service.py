"""platform.workflow service layer — ADR-012 / Week 12 Slice B.

Implements:
1. Workflow definition CRUD with tenant root serialization (`lock_company_unchecked`)
   and two-phase flush pattern for steps.
2. Lightweight verification of permission codes via sa.table without importing permissions module.
3. Workflow instance submission, amount-threshold routing, step snapshotting,
   and automatic superseding of prior unconsumed APPROVED instances.
4. Approval and rejection handling with Separation of Duties (SoD) and step RBAC enforcement.
5. Cancellation handling for PENDING or unconsumed APPROVED instances.
6. Single-transaction state-transition gate (`verify_workflow_approval_unchecked`)
   implementing `WorkflowApprovalGate` with atomic transition to `CONSUMED`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.authorization import (
    require_current_actor,
    require_permission,
)
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    ReversalNotAllowedError,
)
from app.core.tenancy import require_current_company_id
from app.core.workflow import (
    WorkflowApprovalRequiredError,
    WorkflowDecision,
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
    resolve_workflow_target,
    validate_entity_transition,
)
from app.modules.masterdata import service as masterdata_service
from app.platform.workflow.models import (
    WorkflowApprovalLog,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.platform.workflow.schemas import (
    WorkflowDecisionInput,
    WorkflowDefinitionCreate,
    WorkflowDefinitionRead,
    WorkflowDefinitionUpdate,
    WorkflowInstanceRead,
    WorkflowInstanceSubmit,
    parse_and_quantize_amount,
)

# Lightweight table reflection for permissions without importing app.platform.permissions
_PERMISSIONS_TABLE = sa.table(
    "permissions",
    sa.column("code", sa.String),
)


async def _verify_permissions_exist(session: AsyncSession, permission_codes: set[str]) -> None:
    """Verify all permission codes exist in the permissions catalog.

    Fails with DomainValidationError if any permission code is unknown.
    """
    if not permission_codes:
        return
    stmt = select(_PERMISSIONS_TABLE.c.code).where(_PERMISSIONS_TABLE.c.code.in_(permission_codes))
    res = await session.execute(stmt)
    found_codes = set(res.scalars().all())
    missing = sorted(permission_codes - found_codes)
    if missing:
        raise DomainValidationError(
            "The following required_permission code(s) do not exist "
            f"in the permissions catalog: {missing}"
        )


def _normalize_entity_type(entity_type: WorkflowEntityType | str) -> str:
    try:
        return WorkflowEntityType(entity_type).value
    except ValueError as exc:
        raise DomainValidationError(f"Invalid workflow entity_type: {entity_type!r}") from exc


def _normalize_transition(transition: WorkflowTransition | str) -> str:
    try:
        return WorkflowTransition(transition).value
    except ValueError as exc:
        raise DomainValidationError(f"Invalid workflow transition: {transition!r}") from exc


def _normalize_status(status: WorkflowInstanceStatus | str) -> str:
    try:
        return WorkflowInstanceStatus(status).value
    except ValueError as exc:
        raise DomainValidationError(f"Invalid workflow instance status: {status!r}") from exc


# ---------------------------------------------------------------------------
# Workflow Definitions
# ---------------------------------------------------------------------------


async def _load_workflow_definition_unchecked(
    session: AsyncSession,
    definition_id: uuid.UUID,
) -> WorkflowDefinitionRead:
    """Reload a workflow definition without checking `platform.workflow.read`."""
    stmt = (
        select(WorkflowDefinition)
        .where(WorkflowDefinition.id == definition_id)
        .options(selectinload(WorkflowDefinition.steps))
    )
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("WorkflowDefinition", str(definition_id))
    return WorkflowDefinitionRead.model_validate(row)


async def list_workflow_definitions(
    session: AsyncSession,
    *,
    entity_type: WorkflowEntityType | str | None = None,
    transition: WorkflowTransition | str | None = None,
    active_only: bool = False,
) -> list[WorkflowDefinitionRead]:
    """List workflow definitions for the active tenant."""
    require_permission("platform.workflow.read")
    require_current_company_id()

    stmt = select(WorkflowDefinition).options(selectinload(WorkflowDefinition.steps))
    if entity_type is not None:
        stmt = stmt.where(WorkflowDefinition.entity_type == _normalize_entity_type(entity_type))
    if transition is not None:
        stmt = stmt.where(WorkflowDefinition.transition == _normalize_transition(transition))
    if active_only:
        stmt = stmt.where(WorkflowDefinition.is_active.is_(True))

    stmt = stmt.order_by(
        WorkflowDefinition.entity_type,
        WorkflowDefinition.transition,
        WorkflowDefinition.name,
    )
    res = await session.execute(stmt)
    rows = res.scalars().all()
    return [WorkflowDefinitionRead.model_validate(r) for r in rows]


async def get_workflow_definition(
    session: AsyncSession,
    definition_id: uuid.UUID,
) -> WorkflowDefinitionRead:
    """Retrieve a single workflow definition by ID for the active tenant."""
    require_permission("platform.workflow.read")
    require_current_company_id()
    return await _load_workflow_definition_unchecked(session, definition_id)


async def create_workflow_definition(
    session: AsyncSession,
    data: WorkflowDefinitionCreate,
) -> WorkflowDefinitionRead:
    """Create a new workflow definition for the active tenant.

    Serialized by locking the tenant root company row to eliminate TOCTOU races.
    """
    require_permission("platform.workflow.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    # Verify all step permissions exist before locking
    perm_codes = {step.required_permission for step in data.steps}
    await _verify_permissions_exist(session, perm_codes)

    # Lock tenant root row FOR UPDATE
    await masterdata_service.lock_company_unchecked(session, company_id)

    # Check for duplicate (company_id, entity_type, transition)
    stmt = select(WorkflowDefinition).where(
        WorkflowDefinition.entity_type == data.entity_type.value,
        WorkflowDefinition.transition == data.transition.value,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        raise ConflictError(
            f"Workflow definition already exists for entity {data.entity_type.value!r} "
            f"and transition {data.transition.value!r}"
        )

    definition_id = uuid.uuid4()
    definition = WorkflowDefinition(
        id=definition_id,
        company_id=company_id,
        entity_type=data.entity_type.value,
        transition=data.transition.value,
        name=data.name,
        description=data.description,
        is_active=data.is_active,
        allow_self_approval=data.allow_self_approval,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    steps = [
        WorkflowStep(
            id=uuid.uuid4(),
            company_id=company_id,
            definition_id=definition_id,
            step_order=s.step_order,
            step_name=s.step_name,
            min_amount=s.min_amount,
            required_permission=s.required_permission,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        for s in data.steps
    ]
    definition.steps = steps
    session.add(definition)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Conflict creating workflow definition for {data.entity_type.value!r} "
            f"-> {data.transition.value!r}"
        ) from exc

    # Reload with steps eagerly loaded
    return await _load_workflow_definition_unchecked(session, definition_id)


async def update_workflow_definition(
    session: AsyncSession,
    definition_id: uuid.UUID,
    data: WorkflowDefinitionUpdate,
) -> WorkflowDefinitionRead:
    """Update mutable attributes of a workflow definition for the active tenant.

    If steps are updated, employs steps.clear() + flush() two-phase write pattern
    to avoid UNIQUE(definition_id, step_order) collision.
    """
    require_permission("platform.workflow.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    if "steps" in data.model_fields_set and data.steps is not None:
        perm_codes = {step.required_permission for step in data.steps}
        await _verify_permissions_exist(session, perm_codes)

    await masterdata_service.lock_company_unchecked(session, company_id)

    stmt = (
        select(WorkflowDefinition)
        .where(WorkflowDefinition.id == definition_id)
        .options(selectinload(WorkflowDefinition.steps))
        .with_for_update()
    )
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("WorkflowDefinition", str(definition_id))

    if "name" in data.model_fields_set and data.name is not None:
        row.name = data.name
    if "description" in data.model_fields_set:
        row.description = data.description
    if "is_active" in data.model_fields_set and data.is_active is not None:
        row.is_active = data.is_active
    if "allow_self_approval" in data.model_fields_set and data.allow_self_approval is not None:
        row.allow_self_approval = data.allow_self_approval

    if "steps" in data.model_fields_set and data.steps is not None:
        # Two-phase flush: clear existing steps first
        row.steps.clear()
        await session.flush()

        new_steps = [
            WorkflowStep(
                id=uuid.uuid4(),
                company_id=company_id,
                definition_id=definition_id,
                step_order=s.step_order,
                step_name=s.step_name,
                min_amount=s.min_amount,
                required_permission=s.required_permission,
                created_by=actor.user_id,
                updated_by=actor.user_id,
            )
            for s in data.steps
        ]
        row.steps = new_steps

    row.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict updating workflow definition {definition_id!r}") from exc

    return await _load_workflow_definition_unchecked(session, definition_id)


async def deactivate_workflow_definition(
    session: AsyncSession,
    definition_id: uuid.UUID,
) -> WorkflowDefinitionRead:
    """Soft deactivate a workflow definition (is_active = False)."""
    require_permission("platform.workflow.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(session, company_id)

    stmt = (
        select(WorkflowDefinition)
        .where(WorkflowDefinition.id == definition_id)
        .options(selectinload(WorkflowDefinition.steps))
        .with_for_update()
    )
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("WorkflowDefinition", str(definition_id))

    row.is_active = False
    row.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict deactivating workflow definition {definition_id!r}") from exc

    return await _load_workflow_definition_unchecked(session, definition_id)


# ---------------------------------------------------------------------------
# Workflow Instances
# ---------------------------------------------------------------------------


async def _load_workflow_instance_unchecked(
    session: AsyncSession,
    instance_id: uuid.UUID,
) -> WorkflowInstanceRead:
    """Reload a workflow instance without checking `platform.workflow.read`."""
    stmt = (
        select(WorkflowInstance)
        .where(WorkflowInstance.id == instance_id)
        .options(selectinload(WorkflowInstance.logs))
    )
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("WorkflowInstance", str(instance_id))
    return WorkflowInstanceRead.model_validate(row)


async def list_workflow_instances(
    session: AsyncSession,
    *,
    entity_type: WorkflowEntityType | str | None = None,
    entity_id: uuid.UUID | None = None,
    transition: WorkflowTransition | str | None = None,
    status: WorkflowInstanceStatus | str | None = None,
) -> list[WorkflowInstanceRead]:
    """List workflow instances for the active tenant."""
    require_permission("platform.workflow.read")
    require_current_company_id()

    stmt = select(WorkflowInstance).options(selectinload(WorkflowInstance.logs))
    if entity_type is not None:
        stmt = stmt.where(WorkflowInstance.entity_type == _normalize_entity_type(entity_type))
    if entity_id is not None:
        stmt = stmt.where(WorkflowInstance.entity_id == entity_id)
    if transition is not None:
        stmt = stmt.where(WorkflowInstance.transition == _normalize_transition(transition))
    if status is not None:
        stmt = stmt.where(WorkflowInstance.status == _normalize_status(status))

    stmt = stmt.order_by(WorkflowInstance.created_at.desc())
    res = await session.execute(stmt)
    rows = res.scalars().all()
    return [WorkflowInstanceRead.model_validate(r) for r in rows]


async def get_workflow_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
) -> WorkflowInstanceRead:
    """Retrieve a single workflow instance by ID with its approval logs."""
    require_permission("platform.workflow.read")
    require_current_company_id()
    return await _load_workflow_instance_unchecked(session, instance_id)


async def submit_workflow_instance(
    session: AsyncSession,
    data: WorkflowInstanceSubmit,
) -> WorkflowInstanceRead:
    """Submit an entity transition into workflow approval.

    1. Resolves authoritative target amount and locks target document via resolve_workflow_target.
    2. Validates that an active WorkflowDefinition exists.
    3. Locks any existing active instance:
       - If an existing instance is PENDING -> raises ConflictError (409).
       - If an existing instance is APPROVED -> supersedes it by setting status='CANCELLED' + flush.
    4. Evaluates amount-threshold routing:
       - Filters steps where step.min_amount <= target_amount into steps_snapshot.
       - If matched steps > 0: status='PENDING', current_step_order=matched_steps[0]['step_order'].
       - If matched steps == 0: status='APPROVED', current_step_order=None.
    """
    require_permission("platform.workflow.approve")
    company_id = require_current_company_id()
    actor = require_current_actor()

    validate_entity_transition(data.entity_type, data.transition)

    # 1. Resolve target document amount under lock
    authoritative_amount = await resolve_workflow_target(
        session,
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        transition=data.transition,
    )
    quantized_amount = parse_and_quantize_amount(authoritative_amount, "target_amount")

    # 2. Find active definition
    stmt_def = (
        select(WorkflowDefinition)
        .where(
            WorkflowDefinition.entity_type == data.entity_type.value,
            WorkflowDefinition.transition == data.transition.value,
            WorkflowDefinition.is_active.is_(True),
        )
        .options(selectinload(WorkflowDefinition.steps))
    )
    definition = (await session.execute(stmt_def)).scalars().first()
    if definition is None:
        raise NotFoundError(
            "WorkflowDefinition",
            f"({data.entity_type.value}, {data.transition.value})",
        )

    # 3. Check for existing active instance under FOR UPDATE lock
    stmt_existing = (
        select(WorkflowInstance)
        .where(
            WorkflowInstance.entity_type == data.entity_type.value,
            WorkflowInstance.entity_id == data.entity_id,
            WorkflowInstance.transition == data.transition.value,
            WorkflowInstance.status.in_(["PENDING", "APPROVED"]),
        )
        .with_for_update()
    )
    existing_instances = (await session.execute(stmt_existing)).scalars().all()
    for existing in existing_instances:
        if existing.status == WorkflowInstanceStatus.PENDING.value:
            raise ConflictError(
                f"An active approval instance {existing.id} is already PENDING for "
                f"{data.entity_type.value} {data.entity_id} transition {data.transition.value!r}"
            )
        if existing.status == WorkflowInstanceStatus.APPROVED.value:
            # Supersede prior unconsumed approval
            existing.status = WorkflowInstanceStatus.CANCELLED.value
            existing.updated_by = actor.user_id
            await session.flush()

    # 4. Route steps by min_amount threshold
    matched_steps = [
        {
            "step_order": step.step_order,
            "step_name": step.step_name,
            "min_amount": str(step.min_amount),
            "required_permission": step.required_permission,
        }
        for step in definition.steps
        if step.min_amount <= quantized_amount
    ]
    # Sort by step_order ascending
    matched_steps.sort(key=lambda s: int(str(s["step_order"])))

    instance_id = uuid.uuid4()
    if matched_steps:
        status_val = WorkflowInstanceStatus.PENDING.value
        current_step_order = matched_steps[0]["step_order"]
    else:
        status_val = WorkflowInstanceStatus.APPROVED.value
        current_step_order = None

    instance = WorkflowInstance(
        id=instance_id,
        company_id=company_id,
        definition_id=definition.id,
        entity_type=data.entity_type.value,
        entity_id=data.entity_id,
        transition=data.transition.value,
        status=status_val,
        target_amount=quantized_amount,
        current_step_order=current_step_order,
        steps_snapshot=matched_steps,
        requested_by=actor.user_id,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    session.add(instance)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Conflict creating workflow instance for {data.entity_type.value} {data.entity_id}"
        ) from exc

    return await _load_workflow_instance_unchecked(session, instance_id)


async def approve_workflow_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
    data: WorkflowDecisionInput | None = None,
) -> WorkflowInstanceRead:
    """Approve the current step of a PENDING workflow instance.

    Enforces:
    1. Caller must have `platform.workflow.approve` permission.
    2. Caller must possess active step's `required_permission`.
    3. Separation of Duties (SoD): requester cannot approve own request
       unless allow_self_approval=True.
    Advances to next step or marks APPROVED if all steps completed.
    """
    require_permission("platform.workflow.approve")
    require_current_company_id()
    actor = require_current_actor()

    stmt = (
        select(WorkflowInstance)
        .where(WorkflowInstance.id == instance_id)
        .options(
            selectinload(WorkflowInstance.definition),
            selectinload(WorkflowInstance.logs),
        )
        .with_for_update()
    )
    res = await session.execute(stmt)
    instance = res.scalars().first()
    if instance is None:
        raise NotFoundError("WorkflowInstance", str(instance_id))

    if instance.status != WorkflowInstanceStatus.PENDING.value:
        raise ConflictError(
            f"Workflow instance {instance_id} is in status {instance.status!r}; "
            "only PENDING instances can be approved"
        )

    # Find current step in snapshot
    steps = instance.steps_snapshot
    current_step_idx = next(
        (i for i, s in enumerate(steps) if s.get("step_order") == instance.current_step_order),
        None,
    )
    if current_step_idx is None:
        raise DomainValidationError(
            f"Corrupted workflow instance {instance_id}: "
            f"current_step_order {instance.current_step_order} not found in steps_snapshot"
        )
    current_step = steps[current_step_idx]

    # Check SoD
    if not instance.definition.allow_self_approval and actor.user_id == instance.requested_by:
        raise ActionDeniedError(
            "Separation of Duties violation: requester cannot approve their own submission"
        )

    # Check step required_permission
    step_perm = current_step.get("required_permission")
    if step_perm:
        require_permission(step_perm)

    # Record log
    comment = data.comment if data else None
    log = WorkflowApprovalLog(
        id=uuid.uuid4(),
        company_id=instance.company_id,
        instance_id=instance.id,
        step_order=current_step["step_order"],
        step_name=current_step["step_name"],
        decision=WorkflowDecision.APPROVE.value,
        actor_id=actor.user_id,
        comment=comment,
        created_by=actor.user_id,
        updated_by=actor.user_id,
        instance=instance,
    )
    session.add(log)

    # Advance step or complete
    if current_step_idx + 1 < len(steps):
        next_step = steps[current_step_idx + 1]
        instance.current_step_order = next_step["step_order"]
    else:
        # All steps approved
        instance.status = WorkflowInstanceStatus.APPROVED.value
        instance.current_step_order = None

    instance.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict approving workflow instance {instance_id!r}") from exc

    return await _load_workflow_instance_unchecked(session, instance_id)


async def reject_workflow_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
    data: WorkflowDecisionInput | None = None,
) -> WorkflowInstanceRead:
    """Reject the current step of a PENDING workflow instance.

    Transitions instance to REJECTED and records audit log.
    """
    require_permission("platform.workflow.approve")
    require_current_company_id()
    actor = require_current_actor()

    stmt = (
        select(WorkflowInstance)
        .where(WorkflowInstance.id == instance_id)
        .options(
            selectinload(WorkflowInstance.definition),
            selectinload(WorkflowInstance.logs),
        )
        .with_for_update()
    )
    res = await session.execute(stmt)
    instance = res.scalars().first()
    if instance is None:
        raise NotFoundError("WorkflowInstance", str(instance_id))

    if instance.status != WorkflowInstanceStatus.PENDING.value:
        raise ConflictError(
            f"Workflow instance {instance_id} is in status {instance.status!r}; "
            "only PENDING instances can be rejected"
        )

    steps = instance.steps_snapshot
    current_step = next(
        (s for s in steps if s.get("step_order") == instance.current_step_order),
        None,
    )
    if current_step is None:
        raise DomainValidationError(
            f"Corrupted workflow instance {instance_id}: "
            f"current_step_order {instance.current_step_order} not found in steps_snapshot"
        )

    # Check SoD
    if not instance.definition.allow_self_approval and actor.user_id == instance.requested_by:
        raise ActionDeniedError(
            "Separation of Duties violation: requester cannot reject their own submission"
        )

    # Check step required_permission
    step_perm = current_step.get("required_permission")
    if step_perm:
        require_permission(step_perm)

    comment = data.comment if data else None
    log = WorkflowApprovalLog(
        id=uuid.uuid4(),
        company_id=instance.company_id,
        instance_id=instance.id,
        step_order=current_step["step_order"],
        step_name=current_step["step_name"],
        decision=WorkflowDecision.REJECT.value,
        actor_id=actor.user_id,
        comment=comment,
        created_by=actor.user_id,
        updated_by=actor.user_id,
        instance=instance,
    )
    session.add(log)

    instance.status = WorkflowInstanceStatus.REJECTED.value
    instance.current_step_order = None
    instance.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict rejecting workflow instance {instance_id!r}") from exc

    return await _load_workflow_instance_unchecked(session, instance_id)


async def cancel_workflow_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
) -> WorkflowInstanceRead:
    """Cancel a PENDING or unconsumed APPROVED workflow instance."""
    require_permission("platform.workflow.approve")
    require_current_company_id()
    actor = require_current_actor()

    stmt = select(WorkflowInstance).where(WorkflowInstance.id == instance_id).with_for_update()
    res = await session.execute(stmt)
    instance = res.scalars().first()
    if instance is None:
        raise NotFoundError("WorkflowInstance", str(instance_id))

    if instance.status not in (
        WorkflowInstanceStatus.PENDING.value,
        WorkflowInstanceStatus.APPROVED.value,
    ):
        raise ConflictError(
            f"Workflow instance {instance_id} is in status {instance.status!r}; "
            "only PENDING or APPROVED instances can be cancelled"
        )

    instance.status = WorkflowInstanceStatus.CANCELLED.value
    instance.current_step_order = None
    instance.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict cancelling workflow instance {instance_id!r}") from exc

    return await _load_workflow_instance_unchecked(session, instance_id)


# ---------------------------------------------------------------------------
# Runtime Approval Gate (implements WorkflowApprovalGate)
# ---------------------------------------------------------------------------


async def verify_workflow_approval_unchecked(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    transition: str,
    amount: Decimal,
) -> None:
    """Verify that an entity transition has satisfied workflow approval requirements.

    Called inside business module state transition transaction.
    If no active definition exists: returns None (no-op).
    If definition exists:
      - quantizes amount.
      - checks matching steps (step.min_amount <= amount).
      - if matching steps == 0: transition is allowed without requiring approval.
      - if matching steps > 0:
          - selects active WorkflowInstance FOR UPDATE.
          - if none exists or status != APPROVED: raises WorkflowApprovalRequiredError (409).
          - if instance.target_amount != amount: raises WorkflowApprovalRequiredError (409).
          - atomically marks instance status='CONSUMED', consumed_at=now, consumed_by=actor.user_id.
          - flushes changes to session (does NOT commit, caller owns transaction).
    """
    require_current_company_id()
    actor = require_current_actor()

    # 1. Check for active definition
    stmt_def = (
        select(WorkflowDefinition)
        .where(
            WorkflowDefinition.entity_type == entity_type,
            WorkflowDefinition.transition == transition,
            WorkflowDefinition.is_active.is_(True),
        )
        .options(selectinload(WorkflowDefinition.steps))
    )
    definition = (await session.execute(stmt_def)).scalars().first()
    if definition is None:
        return

    quantized_amount = parse_and_quantize_amount(amount, "amount")

    # 2. Check matching steps
    matching_steps = [s for s in definition.steps if s.min_amount <= quantized_amount]
    if not matching_steps:
        # Amount is below all step thresholds, direct transition allowed.
        # Consume a matching APPROVED instance, or cancel any stale PENDING/mismatched instance.
        stmt_inst = (
            select(WorkflowInstance)
            .where(
                WorkflowInstance.entity_type == entity_type,
                WorkflowInstance.entity_id == entity_id,
                WorkflowInstance.transition == transition,
                WorkflowInstance.status.in_(["PENDING", "APPROVED"]),
            )
            .with_for_update()
        )
        existing_active = (await session.execute(stmt_inst)).scalars().first()
        if existing_active is not None:
            if (
                existing_active.status == WorkflowInstanceStatus.APPROVED.value
                and existing_active.target_amount == quantized_amount
            ):
                existing_active.status = WorkflowInstanceStatus.CONSUMED.value
                existing_active.consumed_at = datetime.now(UTC)
                existing_active.consumed_by = actor.user_id
            else:
                existing_active.status = WorkflowInstanceStatus.CANCELLED.value
                existing_active.current_step_order = None
            existing_active.updated_by = actor.user_id
            await session.flush()
        return

    # 3. Matching steps exist: must have an APPROVED instance matching quantized_amount
    stmt_inst = (
        select(WorkflowInstance)
        .where(
            WorkflowInstance.entity_type == entity_type,
            WorkflowInstance.entity_id == entity_id,
            WorkflowInstance.transition == transition,
            WorkflowInstance.status.in_(["PENDING", "APPROVED"]),
        )
        .with_for_update()
    )
    instance = (await session.execute(stmt_inst)).scalars().first()

    if instance is None:
        raise WorkflowApprovalRequiredError(
            f"Workflow approval required for {entity_type} {entity_id} transition {transition!r} "
            f"(amount {quantized_amount} meets step thresholds, but no approval instance exists)"
        )

    if instance.status == WorkflowInstanceStatus.PENDING.value:
        raise WorkflowApprovalRequiredError(
            f"Workflow approval is still PENDING for {entity_type} {entity_id} "
            f"transition {transition!r} (instance {instance.id}, "
            f"current step {instance.current_step_order})"
        )

    if instance.status == WorkflowInstanceStatus.APPROVED.value:
        if instance.target_amount != quantized_amount:
            raise WorkflowApprovalRequiredError(
                f"Workflow approval amount mismatch for {entity_type} {entity_id} "
                f"transition {transition!r}: approved for {instance.target_amount}, "
                f"but current document total is {quantized_amount}. "
                "Document must be re-submitted for approval."
            )

        # Atomically consume
        instance.status = WorkflowInstanceStatus.CONSUMED.value
        instance.consumed_at = datetime.now(UTC)
        instance.consumed_by = actor.user_id
        instance.updated_by = actor.user_id
        await session.flush()
        return

    raise WorkflowApprovalRequiredError(
        f"Workflow approval required for {entity_type} {entity_id} transition {transition!r}"
    )


# ---------------------------------------------------------------------------
# Authoritative Target Resolver (implements WorkflowTargetResolver)
# ---------------------------------------------------------------------------

_SALES_ORDERS = sa.table(
    "sales_orders",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("status"),
    sa.column("total"),
)

_SALES_ORDER_LINES = sa.table(
    "sales_order_lines",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("order_id"),
    sa.column("product_id"),
    sa.column("qty"),
    sa.column("amount"),
    sa.column("unit_price_is_override"),
)

_PRODUCTS = sa.table(
    "products",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("list_price"),
)

_ACCOUNTS = sa.table(
    "accounts",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("code"),
    sa.column("is_control"),
)

_INVOICES = sa.table(
    "invoices",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("status"),
    sa.column("total"),
    sa.column("settled_amount"),
)

_PAYMENTS = sa.table(
    "payments",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("status"),
    sa.column("amount"),
    sa.column("allocated_amount"),
)

_JOURNAL_ENTRIES = sa.table(
    "journal_entries",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("reversal_of_id"),
)

_JOURNAL_LINES = sa.table(
    "journal_lines",
    sa.column("id"),
    sa.column("company_id"),
    sa.column("entry_id"),
    sa.column("account_id"),
    sa.column("debit"),
)


async def resolve_workflow_target_unchecked(  # noqa: PLR0915
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    transition: str,
) -> Decimal:
    """Resolve authoritative server-side amount for target entity (ADR-012 §2).

    Uses SQLAlchemy Core tables with explicit company_id scoping (Contract 13 compliant).
    """
    company_id = require_current_company_id()
    e_type, t_type = validate_entity_transition(entity_type, transition)

    # 1. ("sales_order", "confirm")
    if e_type is WorkflowEntityType.SALES_ORDER and t_type is WorkflowTransition.CONFIRM:
        stmt_order = (
            select(_SALES_ORDERS.c.id, _SALES_ORDERS.c.status)
            .where(_SALES_ORDERS.c.id == entity_id, _SALES_ORDERS.c.company_id == company_id)
            .with_for_update()
        )
        res_order = (await session.execute(stmt_order)).first()
        if res_order is None:
            raise NotFoundError("SalesOrder", str(entity_id))
        if str(res_order.status).lower() != "draft":
            raise ConflictError(
                f"SalesOrder {entity_id} is in status {res_order.status!r}; "
                "only draft orders can be submitted for confirm workflow"
            )

        # Reprice lines against products list_price unless unit_price_is_override is True
        stmt_lines = (
            select(
                _SALES_ORDER_LINES.c.id,
                _SALES_ORDER_LINES.c.product_id,
                _SALES_ORDER_LINES.c.qty,
                _SALES_ORDER_LINES.c.amount,
                _SALES_ORDER_LINES.c.unit_price_is_override,
                _PRODUCTS.c.list_price,
            )
            .select_from(
                _SALES_ORDER_LINES.outerjoin(
                    _PRODUCTS,
                    sa.and_(
                        _PRODUCTS.c.id == _SALES_ORDER_LINES.c.product_id,
                        _PRODUCTS.c.company_id == company_id,
                    ),
                )
            )
            .where(
                _SALES_ORDER_LINES.c.order_id == entity_id,
                _SALES_ORDER_LINES.c.company_id == company_id,
            )
        )
        lines = (await session.execute(stmt_lines)).all()
        if not lines:
            raise DomainValidationError(
                f"SalesOrder {entity_id} has no lines; cannot be submitted for confirmation"
            )

        new_total = Decimal("0")
        for line in lines:
            if line.list_price is None:
                raise DomainValidationError(
                    f"SalesOrder {entity_id} line references "
                    f"invalid or missing product {line.product_id}"
                )
            if line.unit_price_is_override:
                new_total += line.amount
            else:
                new_total += line.qty * line.list_price

        if new_total <= 0:
            raise DomainValidationError(
                f"SalesOrder {entity_id} recomputed total {new_total} is not positive"
            )
        return parse_and_quantize_amount(new_total, "recomputed_total")

    # 2. ("sales_order", "cancel")
    if e_type is WorkflowEntityType.SALES_ORDER and t_type is WorkflowTransition.CANCEL:
        stmt_order = (
            select(_SALES_ORDERS.c.id, _SALES_ORDERS.c.status, _SALES_ORDERS.c.total)
            .where(_SALES_ORDERS.c.id == entity_id, _SALES_ORDERS.c.company_id == company_id)
            .with_for_update()
        )
        res_order = (await session.execute(stmt_order)).first()
        if res_order is None:
            raise NotFoundError("SalesOrder", str(entity_id))
        status_str = str(res_order.status).lower()
        if status_str == "cancelled":
            raise ConflictError(f"SalesOrder {entity_id} is already cancelled")
        if status_str == "shipped":
            raise ConflictError(
                f"SalesOrder {entity_id} has already shipped and cannot be cancelled"
            )
        return parse_and_quantize_amount(res_order.total, "order_total")

    # 3. ("invoice", "void")
    if e_type is WorkflowEntityType.INVOICE and t_type is WorkflowTransition.VOID:
        stmt_inv = (
            select(
                _INVOICES.c.id,
                _INVOICES.c.status,
                _INVOICES.c.total,
                _INVOICES.c.settled_amount,
            )
            .where(_INVOICES.c.id == entity_id, _INVOICES.c.company_id == company_id)
            .with_for_update()
        )
        res_inv = (await session.execute(stmt_inv)).first()
        if res_inv is None:
            raise NotFoundError("Invoice", str(entity_id))
        if str(res_inv.status).lower() == "voided":
            raise ConflictError(f"Invoice {entity_id} is already voided")
        if res_inv.settled_amount != 0:
            raise ConflictError(
                f"Invoice {entity_id} has settled_amount={res_inv.settled_amount} != 0; "
                "cannot void a partially or fully settled invoice"
            )
        return parse_and_quantize_amount(res_inv.total, "invoice_total")

    # 4. ("payment", "void")
    if e_type is WorkflowEntityType.PAYMENT and t_type is WorkflowTransition.VOID:
        stmt_pay = (
            select(
                _PAYMENTS.c.id,
                _PAYMENTS.c.status,
                _PAYMENTS.c.amount,
                _PAYMENTS.c.allocated_amount,
            )
            .where(_PAYMENTS.c.id == entity_id, _PAYMENTS.c.company_id == company_id)
            .with_for_update()
        )
        res_pay = (await session.execute(stmt_pay)).first()
        if res_pay is None:
            raise NotFoundError("Payment", str(entity_id))
        if str(res_pay.status).lower() == "voided":
            raise ConflictError(f"Payment {entity_id} is already voided")
        if res_pay.allocated_amount != 0:
            raise ConflictError(
                f"Payment {entity_id} has allocated_amount={res_pay.allocated_amount} != 0; "
                "cannot void a payment with active allocations"
            )
        return parse_and_quantize_amount(res_pay.amount, "payment_amount")

    # 5. ("journal_entry", "reverse")
    if e_type is WorkflowEntityType.JOURNAL_ENTRY and t_type is WorkflowTransition.REVERSE:
        stmt_je = (
            select(_JOURNAL_ENTRIES.c.id, _JOURNAL_ENTRIES.c.reversal_of_id)
            .where(
                _JOURNAL_ENTRIES.c.id == entity_id,
                _JOURNAL_ENTRIES.c.company_id == company_id,
            )
            .with_for_update()
        )
        res_je = (await session.execute(stmt_je)).first()
        if res_je is None:
            raise NotFoundError("JournalEntry", str(entity_id))
        if res_je.reversal_of_id is not None:
            raise ReversalNotAllowedError(
                f"JournalEntry {entity_id} is itself a reversal entry and cannot be reversed"
            )

        # Check if already reversed
        stmt_already = select(_JOURNAL_ENTRIES.c.id).where(
            _JOURNAL_ENTRIES.c.reversal_of_id == entity_id,
            _JOURNAL_ENTRIES.c.company_id == company_id,
        )
        if (await session.execute(stmt_already)).scalar_one_or_none() is not None:
            raise ReversalNotAllowedError(f"JournalEntry {entity_id} has already been reversed")

        # Check control accounts (ADR-008 R16 / fail-fast)
        stmt_ctrl = (
            select(_ACCOUNTS.c.code)
            .select_from(
                _JOURNAL_LINES.join(
                    _ACCOUNTS,
                    sa.and_(
                        _ACCOUNTS.c.id == _JOURNAL_LINES.c.account_id,
                        _ACCOUNTS.c.company_id == company_id,
                    ),
                )
            )
            .where(
                _JOURNAL_LINES.c.entry_id == entity_id,
                _JOURNAL_LINES.c.company_id == company_id,
                ((_ACCOUNTS.c.is_control.is_(True)) | (_ACCOUNTS.c.code.in_(["1100"]))),
            )
        )
        ctrl_code = (await session.execute(stmt_ctrl)).scalar_one_or_none()
        if ctrl_code is not None:
            raise DomainValidationError(
                f"JournalEntry {entity_id} touches control account '{ctrl_code}'; "
                "reversal is forbidden (ADR-008 R16)"
            )

        stmt_sum = select(
            sa.func.coalesce(sa.func.sum(_JOURNAL_LINES.c.debit), 0),
            sa.func.count(_JOURNAL_LINES.c.id),
        ).where(
            _JOURNAL_LINES.c.entry_id == entity_id,
            _JOURNAL_LINES.c.company_id == company_id,
        )
        res_sum = (await session.execute(stmt_sum)).first()
        total_debit = Decimal(str(res_sum[0])) if res_sum else Decimal("0")
        line_count = res_sum[1] if res_sum else 0
        if line_count == 0 or total_debit <= 0:
            raise DomainValidationError(
                f"JournalEntry {entity_id} has no lines or non-positive debit total ({total_debit})"
            )
        return parse_and_quantize_amount(total_debit, "total_debit")

    raise DomainValidationError(
        f"Unsupported entity transition target resolution: ({entity_type}, {transition})"
    )
