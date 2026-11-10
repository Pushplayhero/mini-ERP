"""tests/platform/test_workflow_service.py — Service layer tests for workflow platform.

ADR-012 / Week 12 Slice B.

Covers:
1. Workflow definition CRUD:
   - Unknown required_permission code rejected with DomainValidationError (422).
   - Duplicate (company_id, entity_type, transition) rejected with ConflictError (409).
   - Non-empty steps sequence 1..N validated.
   - Definition update with steps.clear() + flush() two-phase write pattern.
   - Soft deactivation (is_active = False).
2. Workflow instance submission:
   - Dynamic target amount lookup via resolve_workflow_target.
   - Amount-threshold routing into immutable steps_snapshot.
   - When amount meets 0 steps: automatic status=APPROVED, steps_snapshot=[].
   - Duplicate PENDING rejected with ConflictError (409).
   - Re-submitting supersedes prior unconsumed APPROVED instance (cancelled).
3. Sequential approval and rejection:
   - Separation of Duties (SoD) enforcement (requester cannot approve own request
     unless allow_self_approval=True).
   - Required permission for active step checked via require_permission.
   - Advance through multi-step chain to APPROVED.
   - Reject immediately marks REJECTED.
   - Cancel marks CANCELLED.
4. Runtime approval gate (`verify_workflow_approval_unchecked`):
   - No definition -> transparent pass.
   - Amount < all thresholds -> transparent pass (and consumes existing approved instance).
   - Missing/pending instance -> raises WorkflowApprovalRequiredError (409).
   - Amount mismatch -> raises WorkflowApprovalRequiredError (409).
   - Approved matching instance -> atomically transitions to CONSUMED.
   - Consumed instance cannot be reused.
5. Multi-tenant company isolation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.tenancy import company_context
from app.core.workflow import (
    WorkflowApprovalRequiredError,
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
    get_workflow_target_resolver,
    set_workflow_target_resolver,
)
from app.platform.workflow import service as wf_service
from app.platform.workflow.schemas import (
    WorkflowDecisionInput,
    WorkflowDefinitionCreate,
    WorkflowDefinitionUpdate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)
from tests.platform.permissions._helpers import create_company


@pytest.fixture
def target_resolver_mock() -> Any:
    """Mock target resolver returning a configured amount."""
    orig_resolver = get_workflow_target_resolver()
    mock_store: dict[uuid.UUID, Decimal] = {}

    async def _mock_resolver(
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        transition: str,
    ) -> Decimal:
        return mock_store.get(entity_id, Decimal("500.000000"))

    set_workflow_target_resolver(_mock_resolver)
    yield mock_store
    set_workflow_target_resolver(orig_resolver)


@pytest.fixture
async def sample_company(db_session: AsyncSession) -> uuid.UUID:
    return await create_company(db_session, "WFCO1")


@pytest.fixture
async def other_company(db_session: AsyncSession) -> uuid.UUID:
    return await create_company(db_session, "WFCO2")


@pytest.fixture
def admin_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset(
            {
                "platform.workflow.read",
                "platform.workflow.manage",
                "platform.workflow.approve",
                "sales.order.confirm",
                "ledger.journal_entry.reverse",
            }
        ),
    )


@pytest.fixture
def manager_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset(
            {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"}
        ),
    )


@pytest.fixture
def requester_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.workflow.read", "platform.workflow.approve"}),
    )


# ---------------------------------------------------------------------------
# 1. Definition CRUD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workflow_definition_validation(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    with company_context(sample_company), actor_context(admin_actor):
        # Unknown required_permission -> 422 DomainValidationError
        with pytest.raises(DomainValidationError, match="do not exist in the permissions catalog"):
            await wf_service.create_workflow_definition(
                db_session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="SO Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Step 1",
                            min_amount=Decimal("100"),
                            required_permission="nonexistent.permission.code",
                        )
                    ],
                ),
            )

        # Valid create
        created = await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CONFIRM,
                name="Sales Order Approval",
                description="Threshold approvals for sales orders",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Manager Review",
                        min_amount=Decimal("1000.00"),
                        required_permission="sales.order.confirm",
                    ),
                    WorkflowStepInput(
                        step_order=2,
                        step_name="CFO Signoff",
                        min_amount=Decimal("50000.00"),
                        required_permission="platform.workflow.approve",
                    ),
                ],
            ),
        )
        assert created.name == "Sales Order Approval"
        assert len(created.steps) == 2
        assert created.steps[0].step_order == 1
        assert created.steps[1].step_order == 2
        assert created.steps[0].min_amount == Decimal("1000.000000")

        # Duplicate (entity_type, transition) -> 409 ConflictError
        with pytest.raises(ConflictError, match="already exists"):
            await wf_service.create_workflow_definition(
                db_session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="Duplicate Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Step 1",
                            min_amount=Decimal("0"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )


@pytest.mark.asyncio
async def test_update_and_deactivate_workflow_definition(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    with company_context(sample_company), actor_context(admin_actor):
        created = await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.INVOICE,
                transition=WorkflowTransition.VOID,
                name="Invoice Void Flow",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Lead Review",
                        min_amount=Decimal("0"),
                        required_permission="platform.workflow.approve",
                    )
                ],
            ),
        )

        # Update steps using two-phase flush pattern
        updated = await wf_service.update_workflow_definition(
            db_session,
            created.id,
            WorkflowDefinitionUpdate(
                name="Updated Invoice Void Flow",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Review A",
                        min_amount=Decimal("10.00"),
                        required_permission="platform.workflow.approve",
                    ),
                    WorkflowStepInput(
                        step_order=2,
                        step_name="Review B",
                        min_amount=Decimal("20.00"),
                        required_permission="platform.workflow.approve",
                    ),
                ],
            ),
        )
        assert updated.name == "Updated Invoice Void Flow"
        assert len(updated.steps) == 2
        assert updated.steps[1].step_order == 2

        # Soft deactivation
        deactivated = await wf_service.deactivate_workflow_definition(db_session, created.id)
        assert deactivated.is_active is False


# ---------------------------------------------------------------------------
# 2. Instance Submission & Routing Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_workflow_instance_threshold_routing(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    target_resolver_mock: dict[uuid.UUID, Decimal],
) -> None:
    with company_context(sample_company), actor_context(admin_actor):
        # Create 2-tier definition: tier 1 >= 1000, tier 2 >= 50000
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CONFIRM,
                name="SO High-Value Flow",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Tier 1 Manager",
                        min_amount=Decimal("1000.00"),
                        required_permission="sales.order.confirm",
                    ),
                    WorkflowStepInput(
                        step_order=2,
                        step_name="Tier 2 VP",
                        min_amount=Decimal("50000.00"),
                        required_permission="platform.workflow.approve",
                    ),
                ],
            ),
        )

        # Case A: Amount 500 (below all thresholds) -> auto-approved, 0 steps in snapshot
        order_low_id = uuid.uuid4()
        target_resolver_mock[order_low_id] = Decimal("500.000000")
        inst_low = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order_low_id,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        assert inst_low.status == WorkflowInstanceStatus.APPROVED.value
        assert inst_low.current_step_order is None
        assert len(inst_low.steps_snapshot) == 0

        # Case B: Amount 5,000 (qualifies only tier 1) -> PENDING, current_step_order=1
        order_mid_id = uuid.uuid4()
        target_resolver_mock[order_mid_id] = Decimal("5000.000000")
        inst_mid = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order_mid_id,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        assert inst_mid.status == WorkflowInstanceStatus.PENDING.value
        assert inst_mid.current_step_order == 1
        assert len(inst_mid.steps_snapshot) == 1
        assert inst_mid.steps_snapshot[0]["step_name"] == "Tier 1 Manager"

        # Case C: Duplicate submit on PENDING -> ConflictError 409
        with pytest.raises(ConflictError, match="already PENDING"):
            await wf_service.submit_workflow_instance(
                db_session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_mid_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )

        # Case D: Re-submitting on order_low (which is APPROVED) -> supersedes prior
        # approved instance (sets status=CANCELLED)
        target_resolver_mock[order_low_id] = Decimal("60000.000000")  # Price raised!
        inst_superseded = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order_low_id,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        # New instance qualifies both tiers
        assert inst_superseded.status == WorkflowInstanceStatus.PENDING.value
        assert len(inst_superseded.steps_snapshot) == 2
        # Prior instance is now CANCELLED
        old_inst = await wf_service.get_workflow_instance(db_session, inst_low.id)
        assert old_inst.status == WorkflowInstanceStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# 3. Approval Lifecycle, SoD & Step Permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_approval_sod_and_rejection(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    requester_actor: ActorContext,
    manager_actor: ActorContext,
    target_resolver_mock: dict[uuid.UUID, Decimal],
) -> None:
    with company_context(sample_company):
        # 1. Create definition with allow_self_approval=False (default)
        with actor_context(admin_actor):
            await wf_service.create_workflow_definition(
                db_session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                    transition=WorkflowTransition.REVERSE,
                    name="Journal Reverse Flow",
                    allow_self_approval=False,
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Manager Approval",
                            min_amount=Decimal("0.00"),
                            required_permission="sales.order.confirm",
                        ),
                        WorkflowStepInput(
                            step_order=2,
                            step_name="Controller Signoff",
                            min_amount=Decimal("100.00"),
                            required_permission="ledger.journal_entry.reverse",
                        ),
                    ],
                ),
            )

        # 2. Requester submits
        journal_id = uuid.uuid4()
        target_resolver_mock[journal_id] = Decimal("500.000000")
        with actor_context(requester_actor):
            inst = await wf_service.submit_workflow_instance(
                db_session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                    entity_id=journal_id,
                    transition=WorkflowTransition.REVERSE,
                ),
            )
            assert inst.status == WorkflowInstanceStatus.PENDING.value
            assert inst.current_step_order == 1

            # SoD violation: Requester attempts to approve own request -> 403
            with pytest.raises(ActionDeniedError, match="Separation of Duties"):
                await wf_service.approve_workflow_instance(
                    db_session,
                    inst.id,
                    WorkflowDecisionInput(comment="Self approval attempt"),
                )

        # 3. Manager has sales.order.confirm but lacks accounting.journal.post
        with actor_context(manager_actor):
            # Step 1 succeeds
            step1_res = await wf_service.approve_workflow_instance(
                db_session,
                inst.id,
                WorkflowDecisionInput(comment="Looks good to me"),
            )
            assert step1_res.status == WorkflowInstanceStatus.PENDING.value
            assert step1_res.current_step_order == 2

            # Step 2 requires ledger.journal_entry.reverse -> manager denied
            with pytest.raises(
                ActionDeniedError, match="lacks permission 'ledger.journal_entry.reverse'"
            ):
                await wf_service.approve_workflow_instance(db_session, inst.id)

        # 4. Admin has all permissions -> completes Step 2 -> APPROVED
        with actor_context(admin_actor):
            step2_res = await wf_service.approve_workflow_instance(
                db_session,
                inst.id,
                WorkflowDecisionInput(comment="Controller approved"),
            )
            assert step2_res.status == WorkflowInstanceStatus.APPROVED.value
            assert step2_res.current_step_order is None
            assert len(step2_res.logs) == 2


@pytest.mark.asyncio
async def test_workflow_rejection_and_cancellation(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    target_resolver_mock: dict[uuid.UUID, Decimal],
) -> None:
    with company_context(sample_company), actor_context(admin_actor):
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.PAYMENT,
                transition=WorkflowTransition.VOID,
                name="Payment Void Flow",
                allow_self_approval=True,  # Allows admin to test all paths
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Treasury Check",
                        min_amount=Decimal("0.00"),
                        required_permission="platform.workflow.approve",
                    )
                ],
            ),
        )

        # Rejection path
        pay_id_1 = uuid.uuid4()
        target_resolver_mock[pay_id_1] = Decimal("100.000000")
        inst_1 = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.PAYMENT,
                entity_id=pay_id_1,
                transition=WorkflowTransition.VOID,
            ),
        )
        rejected = await wf_service.reject_workflow_instance(
            db_session, inst_1.id, WorkflowDecisionInput(comment="Invalid void reason")
        )
        assert rejected.status == WorkflowInstanceStatus.REJECTED.value
        assert rejected.current_step_order is None
        assert len(rejected.logs) == 1
        assert rejected.logs[0].decision == "REJECT"

        # Cancellation path
        pay_id_2 = uuid.uuid4()
        target_resolver_mock[pay_id_2] = Decimal("200.000000")
        inst_2 = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.PAYMENT,
                entity_id=pay_id_2,
                transition=WorkflowTransition.VOID,
            ),
        )
        cancelled = await wf_service.cancel_workflow_instance(db_session, inst_2.id)
        assert cancelled.status == WorkflowInstanceStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# 4. Runtime Approval Gate Tests (verify_workflow_approval_unchecked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_workflow_approval_gate_lifecycle(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    target_resolver_mock: dict[uuid.UUID, Decimal],
) -> None:
    with company_context(sample_company), actor_context(admin_actor):
        order_id = uuid.uuid4()

        # 1. No workflow definition configured -> gate is transparent no-op
        await wf_service.verify_workflow_approval_unchecked(
            db_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("1000.000000"),
        )

        # 2. Configure workflow: threshold min_amount = 500
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CONFIRM,
                name="SO Confirm Flow",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Step 1",
                        min_amount=Decimal("500.00"),
                        required_permission="platform.workflow.approve",
                    )
                ],
            ),
        )

        # 3. Amount 100 < 500 -> direct transition allowed
        await wf_service.verify_workflow_approval_unchecked(
            db_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("100.000000"),
        )

        # 4. Amount 600 meets threshold, but no approval instance exists -> 409 Conflict
        target_resolver_mock[order_id] = Decimal("600.000000")
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await wf_service.verify_workflow_approval_unchecked(
                db_session,
                entity_type="sales_order",
                entity_id=order_id,
                transition="confirm",
                amount=Decimal("600.000000"),
            )

        # 5. Submit instance -> status=PENDING -> gate still raises 409 Conflict
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order_id,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        with pytest.raises(
            WorkflowApprovalRequiredError, match="Workflow approval is still PENDING"
        ):
            await wf_service.verify_workflow_approval_unchecked(
                db_session,
                entity_type="sales_order",
                entity_id=order_id,
                transition="confirm",
                amount=Decimal("600.000000"),
            )

        # 6. Approve instance for 600, but document amount changed to 700
        # -> raises 409 Conflict (amount mismatch tampering protection)
        await wf_service.approve_workflow_instance(db_session, inst.id)
        with pytest.raises(
            WorkflowApprovalRequiredError,
            match="Workflow approval amount mismatch.*Document must be re-submitted",
        ):
            await wf_service.verify_workflow_approval_unchecked(
                db_session,
                entity_type="sales_order",
                entity_id=order_id,
                transition="confirm",
                amount=Decimal("700.000000"),
            )

        # 7. Exact match amount 600 -> successfully consumed!
        await wf_service.verify_workflow_approval_unchecked(
            db_session,
            entity_type="sales_order",
            entity_id=order_id,
            transition="confirm",
            amount=Decimal("600.000000"),
        )
        consumed_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert consumed_inst.status == WorkflowInstanceStatus.CONSUMED.value
        assert consumed_inst.consumed_at is not None

        # 8. Anti-replay: Calling gate again immediately raises 409 because instance is CONSUMED!
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await wf_service.verify_workflow_approval_unchecked(
                db_session,
                entity_type="sales_order",
                entity_id=order_id,
                transition="confirm",
                amount=Decimal("600.000000"),
            )

        # 9. Stale PENDING instance cleanup when amount drops below all step thresholds:
        order_id_2 = uuid.uuid4()
        target_resolver_mock[order_id_2] = Decimal("600.000000")
        stale_inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order_id_2,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        assert stale_inst.status == WorkflowInstanceStatus.PENDING.value
        await wf_service.verify_workflow_approval_unchecked(
            db_session,
            entity_type="sales_order",
            entity_id=order_id_2,
            transition="confirm",
            amount=Decimal("100.000000"),
        )
        cleaned_inst = await wf_service.get_workflow_instance(db_session, stale_inst.id)
        assert cleaned_inst.status == WorkflowInstanceStatus.CANCELLED.value
        assert cleaned_inst.current_step_order is None


@pytest.mark.asyncio
async def test_write_operations_succeed_without_read_permission(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    """Verify actors with manage/approve permission (without read) succeed on post-commit reload."""
    manage_only_actor = ActorContext(
        user_id=admin_actor.user_id,
        permission_codes=frozenset({"platform.workflow.manage"}),
    )
    with company_context(sample_company), actor_context(manage_only_actor):
        created = await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.INVOICE,
                transition=WorkflowTransition.VOID,
                name="Manage Only Flow",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Step 1",
                        min_amount=Decimal("100.00"),
                        required_permission="platform.workflow.approve",
                    )
                ],
            ),
        )
        updated = await wf_service.update_workflow_definition(
            db_session,
            created.id,
            WorkflowDefinitionUpdate(name="Manage Only Flow Updated"),
        )
        assert updated.name == "Manage Only Flow Updated"
        deactivated = await wf_service.deactivate_workflow_definition(db_session, created.id)
        assert deactivated.is_active is False


# ---------------------------------------------------------------------------
# 5. Multi-Tenant Company Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_multi_tenant_isolation(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    other_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    # Create definition in Company A
    with company_context(sample_company), actor_context(admin_actor):
        def_a = await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CONFIRM,
                name="Company A Flow",
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Step A",
                        min_amount=Decimal("0.00"),
                        required_permission="platform.workflow.approve",
                    )
                ],
            ),
        )

    # In Company B, definition from Company A is invisible
    with company_context(other_company), actor_context(admin_actor):
        defs_b = await wf_service.list_workflow_definitions(db_session)
        assert len(defs_b) == 0

        with pytest.raises(NotFoundError):
            await wf_service.get_workflow_definition(db_session, def_a.id)
