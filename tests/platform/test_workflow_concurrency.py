"""tests/platform/test_workflow_concurrency.py — Concurrency proof, anti-replay,

and E2E lifecycle testing for platform.workflow (ADR-012 Slice D / Week 12).

Covers:
1. Definition mutation serialization (`masterdata_service.lock_company_unchecked`):
   - Create-Create serialization: Concurrent creation of the same `(entity_type, transition)`
     definition is strictly serialized by the tenant root lock, yielding clean `ConflictError`.
   - Update-Deactivate serialization: Concurrent `update_workflow_definition` (two-phase
     `steps.clear()` + `flush()`) and `deactivate_workflow_definition` serialize cleanly without
     unique constraint collisions.
   - Cross-tenant lock independence: Locking Company A does not block definition mutations
     in Company B.
2. Duplicate submission serialization & DB partial unique index guard:
   - Concurrent `submit_workflow_instance` calls for the same target entity serialize on the
     target document's `FOR UPDATE` row lock; the second submission fails with `ConflictError`.
   - Direct DB-level `IntegrityError` proof for `uq_workflow_instance_active`.
3. Dual-manager approval race serialization (`WorkflowInstance` `SELECT ... FOR UPDATE`):
   - Single-step race: First manager moves instance to `APPROVED`; blocked second manager sees
     `APPROVED` upon lock release and receives `ConflictError`, writing only 1 audit log row.
   - Multi-step race: First manager advances Step 1 -> Step 2; blocked second manager (lacking
     Step 2 permission) sees Step 2 upon lock release and fails with `ActionDeniedError (403)`.
4. Post-approval price tampering & policy-tightening guards:
   - Modifying product `list_price` after `APPROVED` triggers `WorkflowApprovalRequiredError`
     (409 amount mismatch) on `sales.service.confirm_order`; re-submission supersedes prior
     `APPROVED` instance to `CANCELLED`.
   - Tightening threshold on a definition blocks consumption of a prior zero-step auto-approved
     instance (`steps_snapshot == []`) with `WorkflowApprovalRequiredError`.
5. Anti-replay & concurrent consumption proof (`verify_workflow_approval_unchecked`):
   - Concurrent execution against the same `APPROVED` instance allows exactly one session to
     transition the instance to `CONSUMED`; the second session receives
     `WorkflowApprovalRequiredError`, and `CONSUMED` instances cannot be approved, rejected,
     cancelled, or re-consumed.
   - Full HTTP E2E lifecycle test across `/api/v1/workflows/*` and `/api/v1/sales-orders/*`.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import ActionDeniedError, ConflictError
from app.core.tenancy import company_context
from app.core.workflow import (
    WorkflowApprovalRequiredError,
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import (
    CustomerCreate,
    ProductCreate,
    ProductUpdate,
)
from app.modules.sales import service as sales_service
from app.modules.sales.models import SalesOrderStatus
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate
from app.platform.permissions import service as permissions_service
from app.platform.workflow import service as wf_service
from app.platform.workflow.models import (
    WorkflowApprovalLog,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.platform.workflow.schemas import (
    WorkflowDecisionInput,
    WorkflowDefinitionCreate,
    WorkflowDefinitionUpdate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)
from tests._auth_helpers import login_user, setup_company_with_admin
from tests.conftest import TEST_ADMIN_ACTOR
from tests.sales._helpers import (
    confirm_order,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
    uom_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workflow_actor(
    user_id: uuid.UUID,
    permissions: set[str],
) -> ActorContext:
    return ActorContext(
        user_id=user_id,
        permission_codes=frozenset(permissions),
    )


async def _setup_order_fixture(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    code_prefix: str,
    list_price: Decimal = Decimal("1000.000000"),
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a company, customer, product, and a draft sales order."""
    co = await setup_company_with_admin(
        client, code=f"{code_prefix}_{uuid.uuid4().hex[:5].upper()}"
    )
    ea_uom = await uom_id(client, co.company_id)

    async with session_factory() as session:
        with company_context(co.company_id), actor_context(TEST_ADMIN_ACTOR):
            cust = await masterdata_service.create_customer(
                session,
                CustomerCreate(
                    code=f"CUST_{uuid.uuid4().hex[:6]}",
                    name="Concurrency Customer",
                    currency_code="TWD",
                    payment_terms_days=30,
                ),
            )
            prod = await masterdata_service.create_product(
                session,
                ProductCreate(
                    sku=f"SKU_{uuid.uuid4().hex[:6]}",
                    name="Concurrency Product",
                    uom_id=ea_uom,
                    list_price=list_price,
                    standard_cost=Decimal("100.000000"),
                ),
            )
            order = await sales_service.create_order(
                session,
                SalesOrderCreate(
                    customer_id=cust.id,
                    lines=[SalesOrderLineCreate(product_id=prod.id, qty=Decimal("1.000000"))],
                ),
            )
            return co.company_id, prod.id, order.id


# ---------------------------------------------------------------------------
# 1. Concurrency: Definition Mutations & Tenant Root Lock
# ---------------------------------------------------------------------------


def _make_company_lock_barrier(
    orig_lock: Any,
) -> tuple[Any, asyncio.Event, asyncio.Event, asyncio.Event, asyncio.Event]:
    """Return an instrumented `lock_company_unchecked` wrapper and coordination events."""
    call_count = 0
    first_locked, second_entering, second_acquired, release_first = (
        asyncio.Event() for _ in range(4)
    )

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            row = await orig_lock(session, cid)
            first_locked.set()
            await release_first.wait()
            return row
        second_entering.set()
        row = await orig_lock(session, cid)
        second_acquired.set()
        return row

    return instrumented_lock, first_locked, second_entering, second_acquired, release_first


def _pause_before_commit(
    session: AsyncSession,
    holding_evt: asyncio.Event,
    release_evt: asyncio.Event,
) -> None:
    """Pause `session.commit()` right after SQL flush so DB row locks stay held."""
    orig_commit = session.commit

    async def paused_commit() -> None:
        holding_evt.set()
        await release_evt.wait()
        await orig_commit()

    session.commit = paused_commit  # type: ignore[method-assign]


def _latch_on_execute_call(
    session: AsyncSession,
    entering_evt: asyncio.Event,
    acquired_evt: asyncio.Event,
    *,
    trigger_call: int = 1,
) -> None:
    """Signal before and after the Nth `session.execute()` call blocks/completes."""
    orig_execute = session.execute
    calls = 0

    async def instrumented_execute(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == trigger_call:
            entering_evt.set()
            res = await orig_execute(*args, **kwargs)
            acquired_evt.set()
            return res
        return await orig_execute(*args, **kwargs)

    session.execute = instrumented_execute  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_concurrent_create_definition_serialized_by_company_lock(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-012 §6: concurrent creation of identical `(entity_type, transition)`
    workflow definitions is strictly serialized by `masterdata_service.lock_company_unchecked`.
    """
    co = await setup_company_with_admin(client, code="WFCCREATE")
    company_id = co.company_id
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    orig_lock = masterdata_service.lock_company_unchecked
    (
        instrumented_lock,
        first_locked,
        second_entering_lock,
        second_acquired_lock,
        release_first,
    ) = _make_company_lock_barrier(orig_lock)

    data = WorkflowDefinitionCreate(
        entity_type=WorkflowEntityType.SALES_ORDER,
        transition=WorkflowTransition.CONFIRM,
        name="Concurrent SO Confirm Flow",
        steps=[
            WorkflowStepInput(
                step_order=1,
                step_name="Manager Review",
                min_amount=Decimal("500.00"),
                required_permission="sales.order.confirm",
            )
        ],
    )
    results: dict[str, str] = {}

    async def run_create(label: str, *, wait_first: bool) -> None:
        if wait_first:
            await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await wf_service.create_workflow_definition(session, data)
                    results[label] = "success"
                except ConflictError as exc:
                    await session.rollback()
                    results[label] = f"ConflictError: {exc}"

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_1 = asyncio.create_task(run_create("first", wait_first=False))
        await first_locked.wait()
        t_2 = asyncio.create_task(run_create("second", wait_first=True))
        await second_entering_lock.wait()
        await asyncio.sleep(0.05)
        assert not second_acquired_lock.is_set()
        assert not t_2.done()
        release_first.set()
        await asyncio.gather(t_1, t_2)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    assert results["first"] == "success"
    assert "ConflictError" in results["second"]
    assert "already exists" in results["second"]

    async with session_factory() as session:
        with company_context(company_id):
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowDefinition)
                    .where(
                        WorkflowDefinition.company_id == company_id,
                        WorkflowDefinition.entity_type == WorkflowEntityType.SALES_ORDER.value,
                        WorkflowDefinition.transition == WorkflowTransition.CONFIRM.value,
                    )
                )
            ).scalar_one()
            assert count == 1


@pytest.mark.asyncio
async def test_concurrent_update_steps_and_deactivate_serialized_by_company_lock(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves two-phase steps update (`steps.clear()` + `flush()`) and soft deactivation
    serialize cleanly via `lock_company_unchecked`.
    """
    co = await setup_company_with_admin(client, code="WFCUPDD")
    company_id = co.company_id
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
            created = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.INVOICE,
                    transition=WorkflowTransition.VOID,
                    name="Invoice Void Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Initial Lead",
                            min_amount=Decimal("0.00"),
                            required_permission="receivables.invoice.void",
                        )
                    ],
                ),
            )
            def_id = created.id

    orig_lock = masterdata_service.lock_company_unchecked
    (
        instrumented_lock,
        first_locked,
        second_entering_lock,
        second_acquired_lock,
        release_first,
    ) = _make_company_lock_barrier(orig_lock)

    async def run_deactivate() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                await wf_service.deactivate_workflow_definition(session, def_id)

    async def run_update_steps() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                await wf_service.update_workflow_definition(
                    session,
                    def_id,
                    WorkflowDefinitionUpdate(
                        name="Updated Invoice Void Flow",
                        steps=[
                            WorkflowStepInput(
                                step_order=1,
                                step_name="Manager",
                                min_amount=Decimal("100.00"),
                                required_permission="receivables.invoice.void",
                            ),
                            WorkflowStepInput(
                                step_order=2,
                                step_name="CFO",
                                min_amount=Decimal("10000.00"),
                                required_permission="platform.workflow.approve",
                            ),
                        ],
                    ),
                )

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_deact = asyncio.create_task(run_deactivate())
        await first_locked.wait()
        t_upd = asyncio.create_task(run_update_steps())
        await second_entering_lock.wait()
        await asyncio.sleep(0.05)
        assert not second_acquired_lock.is_set()
        assert not t_upd.done()
        release_first.set()
        await asyncio.gather(t_deact, t_upd)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
            reloaded = await wf_service.get_workflow_definition(session, def_id)
            assert reloaded.is_active is False
            assert reloaded.name == "Updated Invoice Void Flow"
            assert [s.step_order for s in reloaded.steps] == [1, 2]


@pytest.mark.asyncio
async def test_cross_tenant_workflow_definition_locks_are_independent(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves holding `lock_company_unchecked` for Company A does NOT block
    workflow definition creation in Company B.
    """
    co_a = await setup_company_with_admin(client, code="WFTENA")
    co_b = await setup_company_with_admin(client, code="WFTENB")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    orig_lock = masterdata_service.lock_company_unchecked
    co_a_locked = asyncio.Event()
    release_co_a = asyncio.Event()

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        row = await orig_lock(session, cid)
        if cid == co_a.company_id:
            co_a_locked.set()
            await release_co_a.wait()
        return row

    co_b_completed = False

    async def run_co_a() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co_a.company_id):
                await wf_service.create_workflow_definition(
                    session,
                    WorkflowDefinitionCreate(
                        entity_type=WorkflowEntityType.SALES_ORDER,
                        transition=WorkflowTransition.CONFIRM,
                        name="Flow A",
                        steps=[
                            WorkflowStepInput(
                                step_order=1,
                                step_name="Step A",
                                min_amount=Decimal("0.00"),
                                required_permission="sales.order.confirm",
                            )
                        ],
                    ),
                )

    async def run_co_b() -> None:
        nonlocal co_b_completed
        await co_a_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co_b.company_id):
                await wf_service.create_workflow_definition(
                    session,
                    WorkflowDefinitionCreate(
                        entity_type=WorkflowEntityType.SALES_ORDER,
                        transition=WorkflowTransition.CONFIRM,
                        name="Flow B",
                        steps=[
                            WorkflowStepInput(
                                step_order=1,
                                step_name="Step B",
                                min_amount=Decimal("0.00"),
                                required_permission="sales.order.confirm",
                            )
                        ],
                    ),
                )
                co_b_completed = True

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_a = asyncio.create_task(run_co_a())
        await co_a_locked.wait()

        t_b = asyncio.create_task(run_co_b())
        await asyncio.wait_for(t_b, timeout=2.0)
        assert co_b_completed is True

        release_co_a.set()
        await t_a
    finally:
        masterdata_service.lock_company_unchecked = orig_lock


# ---------------------------------------------------------------------------
# 2. Concurrency: Duplicate Instance Submission & Partial Unique Index Guard
# ---------------------------------------------------------------------------


async def _assert_duplicate_pending_row_blocked_by_ddl(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    defn_id: uuid.UUID,
    order_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Verify DDL partial unique index `uq_workflow_instance_active` blocks duplicate rows."""
    async with session_factory() as session:
        with company_context(company_id):
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowInstance)
                    .where(
                        WorkflowInstance.company_id == company_id,
                        WorkflowInstance.entity_type == WorkflowEntityType.SALES_ORDER.value,
                        WorkflowInstance.entity_id == order_id,
                        WorkflowInstance.transition == WorkflowTransition.CONFIRM.value,
                        WorkflowInstance.status.in_(["PENDING", "APPROVED"]),
                    )
                )
            ).scalar_one()
            assert count == 1

            duplicate_row = WorkflowInstance(
                id=uuid.uuid4(),
                company_id=company_id,
                definition_id=defn_id,
                entity_type=WorkflowEntityType.SALES_ORDER.value,
                entity_id=order_id,
                transition=WorkflowTransition.CONFIRM.value,
                status=WorkflowInstanceStatus.PENDING.value,
                target_amount=Decimal("1000.000000"),
                current_step_order=1,
                steps_snapshot=[],
                requested_by=requester_id,
            )
            session.add(duplicate_row)
            with pytest.raises(IntegrityError, match="uq_workflow_instance_active"):
                await session.flush()
            await session.rollback()


@pytest.mark.asyncio
async def test_concurrent_duplicate_instance_submission_serialized_and_rejected(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves concurrent `submit_workflow_instance` calls for the same target entity
    serialize on the target document's `SELECT ... FOR UPDATE` lock and reject the
    second request with `ConflictError`, plus direct DDL `uq_workflow_instance_active` proof.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    company_id, _prod_id, order_id = await _setup_order_fixture(
        client, session_factory, code_prefix="WFSUB"
    )

    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
            defn = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="SO Submit Lock Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Manager",
                            min_amount=Decimal("500.00"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )
            defn_id = defn.id

    requester_1 = _workflow_actor(
        uuid.uuid4(), {"platform.workflow.read", "platform.workflow.approve"}
    )
    requester_2 = _workflow_actor(
        uuid.uuid4(), {"platform.workflow.read", "platform.workflow.approve"}
    )
    tx1_holding, release_tx1, tx2_entering, tx2_acquired = (asyncio.Event() for _ in range(4))
    results: dict[str, str] = {}
    submit_payload = WorkflowInstanceSubmit(
        entity_type=WorkflowEntityType.SALES_ORDER,
        entity_id=order_id,
        transition=WorkflowTransition.CONFIRM,
    )

    async def run_first() -> None:
        async with session_factory() as session:
            _pause_before_commit(session, tx1_holding, release_tx1)
            with company_context(company_id), actor_context(requester_1):
                await wf_service.submit_workflow_instance(session, submit_payload)
                results["first"] = "success"

    async def run_second() -> None:
        await tx1_holding.wait()
        async with session_factory() as session:
            _latch_on_execute_call(session, tx2_entering, tx2_acquired, trigger_call=1)
            with company_context(company_id), actor_context(requester_2):
                try:
                    await wf_service.submit_workflow_instance(session, submit_payload)
                    results["second"] = "success"
                except ConflictError as exc:
                    await session.rollback()
                    results["second"] = f"ConflictError: {exc}"

    t_1 = asyncio.create_task(run_first())
    await tx1_holding.wait()
    t_2 = asyncio.create_task(run_second())
    await tx2_entering.wait()
    await asyncio.sleep(0.05)
    assert not tx2_acquired.is_set()
    assert not t_2.done()
    release_tx1.set()
    await asyncio.gather(t_1, t_2)
    assert tx2_acquired.is_set()

    assert results["first"] == "success"
    assert "ConflictError" in results["second"]
    assert "already PENDING" in results["second"]
    await _assert_duplicate_pending_row_blocked_by_ddl(
        session_factory, company_id, defn_id, order_id, requester_2.user_id
    )


# ---------------------------------------------------------------------------
# 3. Concurrency: Dual-Manager Approval Race Serialization
# ---------------------------------------------------------------------------


async def _run_serialized_approval_race(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    inst_id: uuid.UUID,
    mgr_1: ActorContext,
    mgr_2: ActorContext,
) -> dict[str, str]:
    """Run two concurrent approval calls against the same instance, returning outcomes."""
    tx1_locked, release_tx1, tx2_entering, tx2_acquired = (asyncio.Event() for _ in range(4))
    outcomes: dict[str, str] = {}

    async def approve_1() -> None:
        async with session_factory() as session:
            _pause_before_commit(session, tx1_locked, release_tx1)
            with company_context(company_id), actor_context(mgr_1):
                await wf_service.approve_workflow_instance(
                    session, inst_id, WorkflowDecisionInput(comment="Manager 1")
                )
                outcomes["m1"] = "success"

    async def approve_2() -> None:
        await tx1_locked.wait()
        async with session_factory() as session:
            _latch_on_execute_call(session, tx2_entering, tx2_acquired, trigger_call=1)
            with company_context(company_id), actor_context(mgr_2):
                try:
                    await wf_service.approve_workflow_instance(
                        session, inst_id, WorkflowDecisionInput(comment="Manager 2")
                    )
                    outcomes["m2"] = "success"
                except (ConflictError, ActionDeniedError) as exc:
                    await session.rollback()
                    outcomes["m2"] = f"{type(exc).__name__}: {exc}"

    t1 = asyncio.create_task(approve_1())
    await tx1_locked.wait()
    t2 = asyncio.create_task(approve_2())
    await tx2_entering.wait()
    await asyncio.sleep(0.05)
    assert not tx2_acquired.is_set()
    assert not t2.done()
    release_tx1.set()
    await asyncio.gather(t1, t2)
    assert tx2_acquired.is_set()
    return outcomes


async def _assert_single_step_approval_race(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    company_a, _pa, order_a = await _setup_order_fixture(
        client, session_factory, code_prefix="WFDM1"
    )
    requester_a = _workflow_actor(
        uuid.uuid4(), {"platform.workflow.read", "platform.workflow.approve"}
    )
    mgr_perms = {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"}
    mgr_a1 = _workflow_actor(uuid.uuid4(), mgr_perms)
    mgr_a2 = _workflow_actor(uuid.uuid4(), mgr_perms)

    async with session_factory() as session:
        with company_context(company_a), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="Single-Step Race Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Manager Signoff",
                            min_amount=Decimal("500.00"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )
        with company_context(company_a), actor_context(requester_a):
            inst_a = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_a,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )

    res_a = await _run_serialized_approval_race(
        session_factory, company_a, inst_a.id, mgr_a1, mgr_a2
    )
    assert res_a["m1"] == "success"
    assert "ConflictError" in res_a["m2"]
    assert "APPROVED" in res_a["m2"]

    async with session_factory() as session:
        with company_context(company_a), actor_context(TEST_ADMIN_ACTOR):
            reloaded_a = await wf_service.get_workflow_instance(session, inst_a.id)
            assert reloaded_a.status == WorkflowInstanceStatus.APPROVED.value
            assert len(reloaded_a.logs) == 1
            assert reloaded_a.logs[0].actor_id == mgr_a1.user_id


async def _assert_multi_step_approval_race(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    company_b, _pb, order_b = await _setup_order_fixture(
        client, session_factory, code_prefix="WFDM2"
    )
    requester_b = _workflow_actor(
        uuid.uuid4(), {"platform.workflow.read", "platform.workflow.approve"}
    )
    mgr_perms = {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"}
    mgr_b1 = _workflow_actor(uuid.uuid4(), mgr_perms)
    mgr_b2 = _workflow_actor(uuid.uuid4(), mgr_perms)

    async with session_factory() as session:
        with company_context(company_b), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="Two-Step Race Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Sales Manager",
                            min_amount=Decimal("0.00"),
                            required_permission="sales.order.confirm",
                        ),
                        WorkflowStepInput(
                            step_order=2,
                            step_name="Finance Controller",
                            min_amount=Decimal("500.00"),
                            required_permission="ledger.journal_entry.reverse",
                        ),
                    ],
                ),
            )
        with company_context(company_b), actor_context(requester_b):
            inst_b = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_b,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )

    res_b = await _run_serialized_approval_race(
        session_factory, company_b, inst_b.id, mgr_b1, mgr_b2
    )
    assert res_b["m1"] == "success"
    assert "ActionDeniedError" in res_b["m2"]
    assert "ledger.journal_entry.reverse" in res_b["m2"]

    async with session_factory() as session:
        with company_context(company_b), actor_context(TEST_ADMIN_ACTOR):
            reloaded_b = await wf_service.get_workflow_instance(session, inst_b.id)
            assert reloaded_b.status == WorkflowInstanceStatus.PENDING.value
            assert reloaded_b.current_step_order == 2
            assert len(reloaded_b.logs) == 1


@pytest.mark.asyncio
async def test_concurrent_dual_manager_approval_race_serialized_by_for_update(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-012 §6: concurrent approval decisions on the same `WorkflowInstance`
    serialize cleanly on PostgreSQL `SELECT ... FOR UPDATE`.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _assert_single_step_approval_race(client, session_factory)
    await _assert_multi_step_approval_race(client, session_factory)


# ---------------------------------------------------------------------------
# 4. Tampering & Policy-Tightening Defenses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_approval_price_tampering_and_policy_tightening_rejected(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves:
    1. Post-approval price change invalidates cached approval (amount mismatch -> 409)
       and re-submission supersedes prior APPROVED instance to CANCELLED.
    2. Policy tightening (lowering threshold after a zero-step auto-approved instance)
       cannot bypass review because empty `steps_snapshot` is rejected with 409.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    company_id, prod_id, order_id = await _setup_order_fixture(
        client, session_factory, code_prefix="WFTAMP", list_price=Decimal("1000.000000")
    )

    requester = _workflow_actor(
        uuid.uuid4(),
        {
            "platform.workflow.read",
            "platform.workflow.approve",
            "sales.order.confirm",
            "sales.order.read",
        },
    )
    approver = _workflow_actor(
        uuid.uuid4(),
        {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"},
    )

    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            defn = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="Tamper Guard Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Sales Lead",
                            min_amount=Decimal("500.00"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )
            defn_id = defn.id

        # 1. Submit at 1000 and approve
        with company_context(company_id), actor_context(requester):
            inst_1 = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            assert inst_1.target_amount == Decimal("1000.000000")

        with company_context(company_id), actor_context(approver):
            approved_1 = await wf_service.approve_workflow_instance(session, inst_1.id)
            assert approved_1.status == WorkflowInstanceStatus.APPROVED.value

        # 2. Mutate product list_price upward to 1500.000000
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await masterdata_service.update_product(
                session, prod_id, ProductUpdate(list_price=Decimal("1500.000000"))
            )
            await session.commit()

        # 3. confirm_order must reject due to amount mismatch (1500 != 1000)
        with company_context(company_id), actor_context(requester):
            with pytest.raises(WorkflowApprovalRequiredError, match="amount mismatch"):
                await sales_service.confirm_order(session, order_id)
            await session.rollback()

            # 4. Re-submit at authoritative 1500 -> supersedes inst_1 to CANCELLED
            inst_2 = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            assert inst_2.target_amount == Decimal("1500.000000")
            assert inst_2.status == WorkflowInstanceStatus.PENDING.value

            old_inst = await wf_service.get_workflow_instance(session, inst_1.id)
            assert old_inst.status == WorkflowInstanceStatus.CANCELLED.value

        with company_context(company_id), actor_context(approver):
            await wf_service.approve_workflow_instance(session, inst_2.id)

        with company_context(company_id), actor_context(requester):
            confirmed = await sales_service.confirm_order(session, order_id)
            assert confirmed.status is SalesOrderStatus.CONFIRMED
            await session.commit()

        # 5. Policy-tightening scenario on a second order (list_price=1500):
        # Set threshold to 2000 -> zero-step instance (APPROVED, snapshot=[]),
        # then tighten threshold down to 1000 -> gate MUST reject empty snapshot!
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            _cust_tbl = sa.table("customers", sa.column("id"), sa.column("company_id"))
            cust = (
                await session.execute(
                    select(_cust_tbl.c.id).where(_cust_tbl.c.company_id == company_id)
                )
            ).scalar_one()
            order_2 = await sales_service.create_order(
                session,
                SalesOrderCreate(
                    customer_id=cust,
                    lines=[SalesOrderLineCreate(product_id=prod_id, qty=Decimal("1.000000"))],
                ),
            )
            await wf_service.update_workflow_definition(
                session,
                defn_id,
                WorkflowDefinitionUpdate(
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="High Bar",
                            min_amount=Decimal("2000.00"),
                            required_permission="sales.order.confirm",
                        )
                    ]
                ),
            )

        with company_context(company_id), actor_context(requester):
            auto_inst = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_2.id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            assert auto_inst.status == WorkflowInstanceStatus.APPROVED.value
            assert auto_inst.steps_snapshot == []

        # Tighten definition threshold down to 1000.00
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.update_workflow_definition(
                session,
                defn_id,
                WorkflowDefinitionUpdate(
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Tightened Bar",
                            min_amount=Decimal("1000.00"),
                            required_permission="sales.order.confirm",
                        )
                    ]
                ),
            )

        # Gate must refuse consuming the zero-step auto-approved instance!
        with company_context(company_id), actor_context(requester):
            with pytest.raises(
                WorkflowApprovalRequiredError,
                match=(
                    "auto-approved with zero steps, "
                    "but current workflow definition requires approval steps"
                ),
            ):
                await sales_service.confirm_order(session, order_2.id)
            await session.rollback()


# ---------------------------------------------------------------------------
# 5. Anti-Replay & E2E HTTP Lifecycle
# ---------------------------------------------------------------------------


async def _assert_concurrent_consumption_anti_replay(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify two concurrent `verify_workflow_approval_unchecked` calls serialize via
    `FOR UPDATE`, leaving the consumed instance immutable.
    """
    company_id, _prod_id, order_id = await _setup_order_fixture(
        client, session_factory, code_prefix="WFANTI"
    )
    perms = {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"}
    requester = _workflow_actor(uuid.uuid4(), perms)
    approver = _workflow_actor(uuid.uuid4(), perms)

    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="Anti-Replay Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Lead",
                            min_amount=Decimal("500.00"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )
        with company_context(company_id), actor_context(requester):
            inst = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
        with company_context(company_id), actor_context(approver):
            approved = await wf_service.approve_workflow_instance(session, inst.id)
            assert approved.status == WorkflowInstanceStatus.APPROVED.value

    tx1_consumed, release_tx1, tx2_entering, tx2_acquired = (asyncio.Event() for _ in range(4))
    gate_results: dict[str, str] = {}

    async def gate_call_1() -> None:
        async with session_factory() as session:
            with company_context(company_id), actor_context(requester):
                await wf_service.verify_workflow_approval_unchecked(
                    session,
                    entity_type=WorkflowEntityType.SALES_ORDER.value,
                    entity_id=order_id,
                    transition=WorkflowTransition.CONFIRM.value,
                    amount=Decimal("1000.000000"),
                )
                tx1_consumed.set()
                await release_tx1.wait()
                await session.commit()
                gate_results["tx1"] = "consumed"

    async def gate_call_2() -> None:
        await tx1_consumed.wait()
        async with session_factory() as session:
            # Call 1 loads WorkflowDefinition; Call 2 runs WorkflowInstance ... FOR UPDATE
            _latch_on_execute_call(session, tx2_entering, tx2_acquired, trigger_call=2)
            with company_context(company_id), actor_context(requester):
                try:
                    await wf_service.verify_workflow_approval_unchecked(
                        session,
                        entity_type=WorkflowEntityType.SALES_ORDER.value,
                        entity_id=order_id,
                        transition=WorkflowTransition.CONFIRM.value,
                        amount=Decimal("1000.000000"),
                    )
                except WorkflowApprovalRequiredError as exc:
                    await session.rollback()
                    gate_results["tx2"] = f"WorkflowApprovalRequiredError: {exc}"
                else:
                    await session.commit()
                    gate_results["tx2"] = "unexpected_consumed"

    t1 = asyncio.create_task(gate_call_1())
    await tx1_consumed.wait()
    t2 = asyncio.create_task(gate_call_2())
    await tx2_entering.wait()
    await asyncio.sleep(0.05)
    assert not tx2_acquired.is_set()
    assert not t2.done()
    release_tx1.set()
    await asyncio.gather(t1, t2)
    assert tx2_acquired.is_set()

    assert gate_results["tx1"] == "consumed"
    assert "no approval instance exists" in gate_results["tx2"]
    await _assert_consumed_instance_immutable(
        session_factory, company_id, inst.id, approver, requester.user_id
    )


async def _assert_consumed_instance_immutable(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    inst_id: uuid.UUID,
    approver: ActorContext,
    expected_consumer_id: uuid.UUID,
) -> None:
    """Verify terminal CONSUMED workflow instances reject subsequent transitions."""
    async with session_factory() as session:
        with company_context(company_id), actor_context(approver):
            reloaded = await wf_service.get_workflow_instance(session, inst_id)
            assert reloaded.status == WorkflowInstanceStatus.CONSUMED.value
            assert reloaded.consumed_at is not None
            assert reloaded.consumed_by == expected_consumer_id
            for action in (
                lambda: wf_service.approve_workflow_instance(session, inst_id),
                lambda: wf_service.reject_workflow_instance(session, inst_id),
                lambda: wf_service.cancel_workflow_instance(session, inst_id),
            ):
                with pytest.raises(ConflictError, match="CONSUMED"):
                    await action()
                await session.rollback()
            log_count = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowApprovalLog)
                    .where(WorkflowApprovalLog.instance_id == inst_id)
                )
            ).scalar_one()
            assert log_count == 1


async def _assert_full_http_workflow_lifecycle(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify full HTTP workflow definition, SoD, consumption, deactivate, and reactivate flow."""
    co_http = await setup_company_with_admin(
        client,
        code=f"WFHTTP_{uuid.uuid4().hex[:5].upper()}",
        email=f"wfadmin_{uuid.uuid4().hex[:6]}@example.com",
    )
    cust_id = await create_customer(client, co_http.company_id, "CUST-E2E")
    prod_id_http = await create_product(client, co_http.company_id, "SKU-E2E", list_price="800.00")

    resp_def = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "sales_order",
            "transition": "confirm",
            "name": "HTTP E2E SO Confirm",
            "allow_self_approval": False,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Director Approval",
                    "min_amount": "500.00",
                    "required_permission": "sales.order.confirm",
                }
            ],
        },
        headers=co_http.headers,
    )
    assert resp_def.status_code == 201, resp_def.text
    http_def_id = resp_def.json()["id"]

    so_1 = await create_draft_order(
        client, co_http.company_id, cust_id, [order_line(prod_id_http, "1")]
    )
    assert (await confirm_order(client, co_http.company_id, so_1["id"])).status_code == 409

    resp_sub = await client.post(
        "/api/v1/workflows/instances",
        json={"entity_type": "sales_order", "entity_id": so_1["id"], "transition": "confirm"},
        headers=co_http.headers,
    )
    assert resp_sub.status_code == 201, resp_sub.text
    http_inst_id = resp_sub.json()["id"]
    assert resp_sub.json()["status"] == "PENDING"

    resp_self = await client.post(
        f"/api/v1/workflows/instances/{http_inst_id}/approve",
        json={"comment": "Self"},
        headers=co_http.headers,
    )
    assert resp_self.status_code == 403

    mgr_email = f"wfmgr_{uuid.uuid4().hex[:6]}@example.com"
    mgr_pw = "managerpassword123"
    async with session_factory() as session:
        await permissions_service.add_user_with_grant(
            session,
            email=mgr_email,
            password=mgr_pw,
            company_id=co_http.company_id,
            role_code="admin",
        )
        await session.commit()
    mgr_token = await login_user(client, mgr_email, mgr_pw, company_id=co_http.company_id)
    mgr_headers = {"Authorization": f"Bearer {mgr_token}"}

    resp_mgr = await client.post(
        f"/api/v1/workflows/instances/{http_inst_id}/approve",
        json={"comment": "Approved by second manager"},
        headers=mgr_headers,
    )
    assert resp_mgr.status_code == 200, resp_mgr.text
    assert resp_mgr.json()["status"] == "APPROVED"

    resp_ok = await confirm_order(client, co_http.company_id, so_1["id"])
    assert resp_ok.status_code == 200, resp_ok.text
    assert resp_ok.json()["status"] == SalesOrderStatus.CONFIRMED.value

    resp_check = await client.get(
        f"/api/v1/workflows/instances/{http_inst_id}", headers=co_http.headers
    )
    assert resp_check.status_code == 200
    assert resp_check.json()["status"] == "CONSUMED"
    assert resp_check.json()["consumed_at"] is not None

    resp_del = await client.delete(
        f"/api/v1/workflows/definitions/{http_def_id}", headers=co_http.headers
    )
    assert resp_del.status_code == 200
    assert resp_del.json()["is_active"] is False

    so_2 = await create_draft_order(
        client, co_http.company_id, cust_id, [order_line(prod_id_http, "1")]
    )
    resp_so2 = await confirm_order(client, co_http.company_id, so_2["id"])
    assert resp_so2.status_code == 200, resp_so2.text
    assert resp_so2.json()["status"] == SalesOrderStatus.CONFIRMED.value

    resp_react = await client.patch(
        f"/api/v1/workflows/definitions/{http_def_id}",
        json={"is_active": True},
        headers=co_http.headers,
    )
    assert resp_react.status_code == 200
    assert resp_react.json()["is_active"] is True

    so_3 = await create_draft_order(
        client, co_http.company_id, cust_id, [order_line(prod_id_http, "1")]
    )
    assert (await confirm_order(client, co_http.company_id, so_3["id"])).status_code == 409


@pytest.mark.asyncio
async def test_concurrent_consumption_anti_replay_and_e2e_http_lifecycle(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves concurrent consumption anti-replay serialization and full HTTP E2E lifecycle."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _assert_concurrent_consumption_anti_replay(client, session_factory)
    await _assert_full_http_workflow_lifecycle(client, session_factory)


# ---------------------------------------------------------------------------
# 6. Adversarial Hardening Regression Proofs (H-1, M-1, M-2, L-1)
# ---------------------------------------------------------------------------


async def _setup_approved_order_context(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    code_prefix: str,
    allow_self_approval: bool = False,
    required_permission: str = "sales.order.confirm",
    self_approve: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, ActorContext, ActorContext]:
    """Create order + active 1-step definition and advance instance to APPROVED."""
    company_id, _prod_id, order_id = await _setup_order_fixture(
        client, session_factory, code_prefix=code_prefix, list_price=Decimal("1000.000000")
    )
    requester = _workflow_actor(
        uuid.uuid4(),
        {
            "platform.workflow.read",
            "platform.workflow.approve",
            "sales.order.confirm",
            "sales.order.read",
        },
    )
    approver = _workflow_actor(
        uuid.uuid4(),
        {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"},
    )
    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            defn = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name=f"{code_prefix} Flow",
                    allow_self_approval=allow_self_approval,
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Stage 1",
                            min_amount=Decimal("500.00"),
                            required_permission=required_permission,
                        )
                    ],
                ),
            )
        with company_context(company_id), actor_context(requester):
            inst = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
        decider = requester if self_approve else approver
        with company_context(company_id), actor_context(decider):
            approved = await wf_service.approve_workflow_instance(session, inst.id)
            assert approved.status == WorkflowInstanceStatus.APPROVED.value
    return company_id, order_id, defn.id, inst.id, requester, approver


async def _assert_step_and_permission_tightening(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reject stale APPROVED instance when definition adds a step or tightens permission."""
    (
        company_id,
        order_id,
        defn_id,
        _inst_id,
        requester,
        _approver,
    ) = await _setup_approved_order_context(client, session_factory, code_prefix="WFH1P")
    step1 = WorkflowStepInput(
        step_order=1,
        step_name="Stage 1",
        min_amount=Decimal("500.00"),
        required_permission="sales.order.confirm",
    )
    step2 = WorkflowStepInput(
        step_order=2,
        step_name="Finance Controller",
        min_amount=Decimal("500.00"),
        required_permission="platform.workflow.approve",
    )
    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.update_workflow_definition(
                session, defn_id, WorkflowDefinitionUpdate(steps=[step1, step2])
            )
        with company_context(company_id), actor_context(requester):
            with pytest.raises(WorkflowApprovalRequiredError):
                await sales_service.confirm_order(session, order_id)
            await session.rollback()

        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.update_workflow_definition(
                session,
                defn_id,
                WorkflowDefinitionUpdate(
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Tightened Stage 1",
                            min_amount=Decimal("500.00"),
                            required_permission="platform.workflow.approve",
                        )
                    ]
                ),
            )
        with company_context(company_id), actor_context(requester):
            with pytest.raises(WorkflowApprovalRequiredError):
                await sales_service.confirm_order(session, order_id)
            await session.rollback()


async def _assert_sod_policy_tightening(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reject self-approved instance once definition sets allow_self_approval=False."""
    (
        company_id,
        order_id,
        defn_id,
        _inst_id,
        requester,
        _approver,
    ) = await _setup_approved_order_context(
        client,
        session_factory,
        code_prefix="WFH1S",
        allow_self_approval=True,
        required_permission="platform.workflow.approve",
        self_approve=True,
    )
    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.update_workflow_definition(
                session, defn_id, WorkflowDefinitionUpdate(allow_self_approval=False)
            )
        with company_context(company_id), actor_context(requester):
            with pytest.raises(WorkflowApprovalRequiredError):
                await sales_service.confirm_order(session, order_id)
            await session.rollback()


@pytest.mark.asyncio
async def test_policy_tightening_steps_permissions_and_sod_rejected(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves H-1: adding steps, tightening perms, or enabling SoD invalidates prior approval."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _assert_step_and_permission_tightening(client, session_factory)
    await _assert_sod_policy_tightening(client, session_factory)


@pytest.mark.asyncio
async def test_definition_deactivation_cancels_active_instances_and_blocks_decisions(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves M-1: deactivating definition cancels PENDING/APPROVED instances cleanly."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    company_id, prod_id, order_pending_id = await _setup_order_fixture(
        client, session_factory, code_prefix="WFM1"
    )
    requester = _workflow_actor(
        uuid.uuid4(),
        {
            "platform.workflow.read",
            "platform.workflow.approve",
            "sales.order.confirm",
            "sales.order.read",
        },
    )
    approver = _workflow_actor(
        uuid.uuid4(),
        {"platform.workflow.read", "platform.workflow.approve", "sales.order.confirm"},
    )
    _cust_tbl = sa.table("customers", sa.column("id"), sa.column("company_id"))

    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            cust_id = (
                await session.execute(
                    select(_cust_tbl.c.id).where(_cust_tbl.c.company_id == company_id)
                )
            ).scalar_one()
            order_approved = await sales_service.create_order(
                session,
                SalesOrderCreate(
                    customer_id=cust_id,
                    lines=[SalesOrderLineCreate(product_id=prod_id, qty=Decimal("1.000000"))],
                ),
            )
            order_approved_id = order_approved.id
            defn = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="M1 Deactivation Guard",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="Lead",
                            min_amount=Decimal("500.00"),
                            required_permission="sales.order.confirm",
                        )
                    ],
                ),
            )
            defn_id = defn.id
        with company_context(company_id), actor_context(requester):
            inst_pending = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_pending_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            inst_pending_id = inst_pending.id
            inst_approved = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_approved_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            inst_approved_id = inst_approved.id
        with company_context(company_id), actor_context(approver):
            await wf_service.approve_workflow_instance(session, inst_approved_id)

        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.deactivate_workflow_definition(session, defn_id)
            assert (
                await wf_service.get_workflow_instance(session, inst_pending_id)
            ).status == WorkflowInstanceStatus.CANCELLED.value
            assert (
                await wf_service.get_workflow_instance(session, inst_approved_id)
            ).status == WorkflowInstanceStatus.CANCELLED.value

        with company_context(company_id), actor_context(approver):
            for fn in (
                lambda: wf_service.approve_workflow_instance(session, inst_pending_id),
                lambda: wf_service.reject_workflow_instance(
                    session, inst_pending_id, WorkflowDecisionInput(comment="too late")
                ),
            ):
                with pytest.raises(ConflictError):
                    await fn()
                await session.rollback()

        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            await wf_service.update_workflow_definition(
                session, defn_id, WorkflowDefinitionUpdate(is_active=True)
            )
        with company_context(company_id), actor_context(requester):
            with pytest.raises(WorkflowApprovalRequiredError):
                await sales_service.confirm_order(session, order_approved_id)
            await session.rollback()
            resubmitted = await wf_service.submit_workflow_instance(
                session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    entity_id=order_approved_id,
                    transition=WorkflowTransition.CONFIRM,
                ),
            )
            assert resubmitted.status == WorkflowInstanceStatus.PENDING.value


@pytest.mark.asyncio
async def test_concurrent_verify_waits_on_definition_for_share_lock(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves M-2: verify gate waits on WorkflowDefinition FOR SHARE until update commits."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    company_id, _prod_id, order_id = await _setup_order_fixture(
        client, session_factory, code_prefix="WFM2", list_price=Decimal("1000.000000")
    )
    requester = _workflow_actor(uuid.uuid4(), {"sales.order.confirm"})

    async with session_factory() as session:
        with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
            defn = await wf_service.create_workflow_definition(
                session,
                WorkflowDefinitionCreate(
                    entity_type=WorkflowEntityType.SALES_ORDER,
                    transition=WorkflowTransition.CONFIRM,
                    name="FOR SHARE Lock Flow",
                    steps=[
                        WorkflowStepInput(
                            step_order=1,
                            step_name="High Threshold",
                            min_amount=Decimal("2000.00"),
                            required_permission="platform.workflow.approve",
                        )
                    ],
                ),
            )
            defn_id = defn.id

    tx1_holding, tx1_release = asyncio.Event(), asyncio.Event()
    tx2_entering, tx2_acquired = asyncio.Event(), asyncio.Event()
    gate_outcome: dict[str, str] = {}

    async def tx1_tighten_definition() -> None:
        async with session_factory() as session:
            _pause_before_commit(session, tx1_holding, tx1_release)
            with company_context(company_id), actor_context(TEST_ADMIN_ACTOR):
                await wf_service.update_workflow_definition(
                    session,
                    defn_id,
                    WorkflowDefinitionUpdate(
                        steps=[
                            WorkflowStepInput(
                                step_order=1,
                                step_name="Lowered Threshold",
                                min_amount=Decimal("500.00"),
                                required_permission="platform.workflow.approve",
                            )
                        ]
                    ),
                )

    async def tx2_verify_gate() -> None:
        await tx1_holding.wait()
        async with session_factory() as session:
            _latch_on_execute_call(session, tx2_entering, tx2_acquired, trigger_call=1)
            with company_context(company_id), actor_context(requester):
                try:
                    await wf_service.verify_workflow_approval_unchecked(
                        session,
                        entity_type=WorkflowEntityType.SALES_ORDER.value,
                        entity_id=order_id,
                        transition=WorkflowTransition.CONFIRM.value,
                        amount=Decimal("1000.000000"),
                    )
                except WorkflowApprovalRequiredError as exc:
                    await session.rollback()
                    gate_outcome["result"] = f"rejected:{exc}"
                else:
                    await session.commit()
                    gate_outcome["result"] = "bypassed"

    t1 = asyncio.create_task(tx1_tighten_definition())
    await tx1_holding.wait()
    t2 = asyncio.create_task(tx2_verify_gate())
    await tx2_entering.wait()
    await asyncio.sleep(0.05)
    assert not tx2_acquired.is_set()
    assert not t2.done()

    tx1_release.set()
    await asyncio.gather(t1, t2)
    assert tx2_acquired.is_set()
    assert gate_outcome.get("result", "").startswith("rejected:")


@pytest.mark.asyncio
async def test_sales_qty_6dp_bankers_rounding_and_resolver_parity(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves L-1: SalesOrderLineCreate.qty rounds 6dp HALF_EVEN and matches resolver."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    co = await setup_company_with_admin(
        client,
        code=f"WFL1_{uuid.uuid4().hex[:5].upper()}",
        email=f"wfl1_{uuid.uuid4().hex[:6]}@example.com",
    )
    cust_id = await create_customer(client, co.company_id, "CUST-L1")
    prod_id = await create_product(client, co.company_id, "SKU-L1", list_price="0.000003")

    resp_zero = await client.post(
        "/api/v1/sales-orders",
        json={"customer_id": str(cust_id), "lines": [order_line(prod_id, "0.0000004")]},
        headers=co.headers,
    )
    assert resp_zero.status_code == 422

    so_tie_qty = await create_draft_order(
        client, co.company_id, cust_id, [order_line(prod_id, "1.0000005")]
    )
    assert Decimal(so_tie_qty["total"]) == Decimal("0.000003")

    so_at_bar = await create_draft_order(
        client, co.company_id, cust_id, [order_line(prod_id, "1.5")]
    )
    assert Decimal(so_at_bar["total"]) == Decimal("0.000004")

    resp_def = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "sales_order",
            "transition": "confirm",
            "name": "Micro Threshold Flow",
            "allow_self_approval": False,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Micro Reviewer",
                    "min_amount": "0.000004",
                    "required_permission": "sales.order.confirm",
                }
            ],
        },
        headers=co.headers,
    )
    assert resp_def.status_code == 201, resp_def.text

    assert (await confirm_order(client, co.company_id, so_tie_qty["id"])).status_code == 200
    assert (await confirm_order(client, co.company_id, so_at_bar["id"])).status_code == 409

    resp_sub = await client.post(
        "/api/v1/workflows/instances",
        json={"entity_type": "sales_order", "entity_id": so_at_bar["id"], "transition": "confirm"},
        headers=co.headers,
    )
    assert resp_sub.status_code == 201, resp_sub.text
    assert Decimal(resp_sub.json()["target_amount"]) == Decimal("0.000004")

    mgr_email = f"wfl1mgr_{uuid.uuid4().hex[:6]}@example.com"
    mgr_pw = "managerpassword123"
    async with session_factory() as session:
        await permissions_service.add_user_with_grant(
            session, email=mgr_email, password=mgr_pw, company_id=co.company_id, role_code="admin"
        )
        await session.commit()
    mgr_token = await login_user(client, mgr_email, mgr_pw, company_id=co.company_id)
    inst_id = resp_sub.json()["id"]
    resp_app = await client.post(
        f"/api/v1/workflows/instances/{inst_id}/approve",
        json={"comment": "Approve micro amount"},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert resp_app.status_code == 200, resp_app.text

    resp_ok = await confirm_order(client, co.company_id, so_at_bar["id"])
    assert resp_ok.status_code == 200, resp_ok.text
    assert resp_ok.json()["status"] == SalesOrderStatus.CONFIRMED.value
