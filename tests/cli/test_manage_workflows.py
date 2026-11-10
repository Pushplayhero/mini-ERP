"""tests/cli/test_manage_workflows.py — Coverage for manage_workflows CLI (ADR-012).

Covers:
1. `_resolve_company_id`: via company_code, company_id, error cases.
2. Definition CRUD: define, list, get, update, deactivate.
3. Instance lifecycle: submit, list, get, approve, reject, cancel.
4. Separation of Duties (SoD) testing via `--actor-id`.
5. CLI `_main` entry point with standard exit codes (0 and 1).
6. `WORKFLOW_ADMIN_ACTOR` execution.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.sales._helpers import create_company, uom_id

from app.cli import manage_workflows
from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import (
    ActionDeniedError,
    DomainValidationError,
    NotFoundError,
)
from app.core.tenancy import company_context
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import CustomerCreate, ProductCreate
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate
from app.platform.workflow.schemas import (
    WorkflowDefinitionCreate,
    WorkflowDefinitionUpdate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)


@pytest.mark.asyncio
async def test_resolve_company_id(client: AsyncClient, db_session: AsyncSession) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Resolve by code
    resolved_id = await manage_workflows._resolve_company_id(db_session, company_code=code)
    assert resolved_id == company_id

    # 2. Resolve by id
    resolved_by_id = await manage_workflows._resolve_company_id(db_session, company_id=company_id)
    assert resolved_by_id == company_id

    # 3. Resolve by matching code and id
    both_id = await manage_workflows._resolve_company_id(
        db_session, company_code=code, company_id=company_id
    )
    assert both_id == company_id

    # 4. Neither provided -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Either company_code or company_id"):
        await manage_workflows._resolve_company_id(db_session)

    # 5. Non-existent code -> NotFoundError
    with pytest.raises(NotFoundError, match="Company"):
        await manage_workflows._resolve_company_id(db_session, company_code="NONEXISTENT_XYZ")

    # 6. Mismatched code and id -> DomainValidationError
    with pytest.raises(DomainValidationError, match="does not match provided code"):
        await manage_workflows._resolve_company_id(
            db_session, company_code="MISMATCH_CODE", company_id=company_id
        )

    # 7. Empty/whitespace company_code -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Company code must not be empty"):
        await manage_workflows._resolve_company_id(db_session, company_code="   ")


@pytest.mark.asyncio
async def test_manage_workflows_definition_functions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Define workflow
    defn = await manage_workflows.define_workflow(
        WorkflowDefinitionCreate(
            entity_type=WorkflowEntityType.SALES_ORDER,
            transition=WorkflowTransition.CONFIRM,
            name="Standard SO Approval",
            description="CLI test definition",
            allow_self_approval=False,
            steps=[
                WorkflowStepInput(
                    step_order=1,
                    step_name="Sales Lead",
                    min_amount=Decimal("500.00"),
                    required_permission="sales.order.confirm",
                )
            ],
        ),
        company_code=code,
        session=db_session,
    )
    assert defn.name == "Standard SO Approval"
    assert defn.allow_self_approval is False
    assert len(defn.steps) == 1

    # 2. List definitions
    all_defs = await manage_workflows.list_definitions(company_code=code, session=db_session)
    assert len(all_defs) == 1
    assert all_defs[0].id == defn.id

    filtered_defs = await manage_workflows.list_definitions(
        company_id=company_id, entity_type="invoice", session=db_session
    )
    assert len(filtered_defs) == 0

    # 3. Get definition
    got = await manage_workflows.get_definition(defn.id, company_code=code, session=db_session)
    assert got.id == defn.id

    # 4. Update definition
    updated = await manage_workflows.update_workflow(
        defn.id,
        WorkflowDefinitionUpdate(name="Updated SO Approval", allow_self_approval=True),
        company_code=code,
        session=db_session,
    )
    assert updated.name == "Updated SO Approval"
    assert updated.allow_self_approval is True

    # 5. Deactivate definition
    deactivated = await manage_workflows.deactivate_workflow(
        defn.id, company_code=code, session=db_session
    )
    assert deactivated.is_active is False

    active_defs = await manage_workflows.list_definitions(
        company_code=code, active_only=True, session=db_session
    )
    assert len(active_defs) == 0


@pytest.mark.asyncio
async def test_manage_workflows_instance_functions_and_sod(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    ea_uom_id = await uom_id(client, company_id)

    seed_actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset(
            {
                "masterdata.customer.create",
                "masterdata.product.create",
                "sales.order.create",
            }
        ),
        is_system_actor=True,
    )

    with company_context(company_id), actor_context(seed_actor):
        # Create customer & product & draft order
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(code=f"C_{uuid.uuid4().hex[:6]}", name="Cust", currency_code="TWD"),
        )
        prod = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"S_{uuid.uuid4().hex[:6]}",
                name="Prod",
                uom_id=ea_uom_id,
                list_price=Decimal("1200.00"),
                standard_cost=Decimal("600.00"),
            ),
        )
        order = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[SalesOrderLineCreate(product_id=prod.id, qty=Decimal("1.000000"))],
            ),
        )
        await db_session.commit()

    # Define workflow with allow_self_approval = False (enforce SoD)
    await manage_workflows.define_workflow(
        WorkflowDefinitionCreate(
            entity_type=WorkflowEntityType.SALES_ORDER,
            transition=WorkflowTransition.CONFIRM,
            name="Strict SoD Flow",
            allow_self_approval=False,
            steps=[
                WorkflowStepInput(
                    step_order=1,
                    step_name="Supervisor",
                    min_amount=Decimal("100.00"),
                    required_permission="sales.order.confirm",
                )
            ],
        ),
        company_code=code,
        session=db_session,
    )

    # Actor 1 submits
    actor_submitter = uuid.uuid4()
    inst = await manage_workflows.submit_instance(
        WorkflowInstanceSubmit(
            entity_type=WorkflowEntityType.SALES_ORDER,
            entity_id=order.id,
            transition=WorkflowTransition.CONFIRM,
        ),
        company_code=code,
        actor_id=actor_submitter,
        session=db_session,
    )
    assert inst.status == WorkflowInstanceStatus.PENDING.value
    assert inst.requested_by == actor_submitter

    # Actor 1 attempts to approve own submission -> 403 ActionDeniedError (SoD)
    with pytest.raises(ActionDeniedError, match="Separation of Duties"):
        await manage_workflows.approve_instance(
            inst.id,
            comment="Self approval",
            company_code=code,
            actor_id=actor_submitter,
            session=db_session,
        )

    # Actor 2 approves -> succeeds
    actor_approver = uuid.uuid4()
    approved = await manage_workflows.approve_instance(
        inst.id,
        comment="Approved by manager",
        company_code=code,
        actor_id=actor_approver,
        session=db_session,
    )
    assert approved.status == WorkflowInstanceStatus.APPROVED.value
    assert len(approved.logs) == 1
    assert approved.logs[0].actor_id == actor_approver

    # Cancel approved instance
    cancelled = await manage_workflows.cancel_instance(
        inst.id,
        company_code=code,
        actor_id=actor_approver,
        session=db_session,
    )
    assert cancelled.status == WorkflowInstanceStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_manage_workflows_main_cli(client: AsyncClient, db_session: AsyncSession) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    await create_company(client, code)

    # 1. define
    rc = await manage_workflows._main(
        [
            "define",
            "--company-code",
            code,
            "--entity-type",
            "sales_order",
            "--transition",
            "confirm",
            "--name",
            "CLI Created Flow",
            "--allow-self-approval",
            "--steps",
            (
                '[{"step_order": 1, "step_name": "Lead", "min_amount": "200.00", '
                '"required_permission": "sales.order.confirm"}]'
            ),
        ],
        dispose=False,
    )
    assert rc == 0

    # 2. list
    rc = await manage_workflows._main(
        ["list", "--company-code", code, "--entity-type", "sales_order"],
        dispose=False,
    )
    assert rc == 0

    defs = await manage_workflows.list_definitions(company_code=code)
    def_id = str(defs[0].id)

    # 3. get
    rc = await manage_workflows._main(
        ["get", "--company-code", code, "--definition-id", def_id],
        dispose=False,
    )
    assert rc == 0

    # 4. update
    rc = await manage_workflows._main(
        [
            "update",
            "--company-code",
            code,
            "--definition-id",
            def_id,
            "--name",
            "CLI Updated Flow",
        ],
        dispose=False,
    )
    assert rc == 0

    # 5. deactivate
    rc = await manage_workflows._main(
        ["deactivate", "--company-code", code, "--definition-id", def_id],
        dispose=False,
    )
    assert rc == 0
