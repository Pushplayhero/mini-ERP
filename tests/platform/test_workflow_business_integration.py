"""tests/platform/test_workflow_business_integration.py — End-to-end integration tests.

ADR-012 / Week 12 Slice C.

Covers cross-module state transitions gating and authoritative target resolution:
1. Sales order confirm:
   - Gated when order total >= step threshold.
   - Transparent pass when total < threshold.
   - Successful confirmation and instance status=CONSUMED after full approval.
2. Sales order cancel:
   - Gated when cancel definition exists.
   - Successful cancellation and instance status=CONSUMED after approval.
3. Invoice void:
   - Gated by invoice void workflow.
   - Authoritative snapshot amount from invoice.total.
   - Successful voiding after approval.
4. Payment void:
   - Gated by payment void workflow.
   - Authoritative snapshot amount from payment.amount.
   - Successful voiding after approval.
5. Journal entry reverse:
   - Gated by journal reversal workflow.
   - Authoritative amount calculated as SUM(lines.debit).
   - Successful reversal after approval.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure app.main is imported to register approval gate & target resolver
from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import DomainValidationError
from app.core.tenancy import company_context
from app.core.workflow import (
    WorkflowApprovalRequiredError,
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
)
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import StockMoveType
from app.modules.ledger import service as ledger_service
from app.modules.ledger.schemas import (
    AccountingPeriodCreate,
    JournalEntryCreate,
    JournalLineCreate,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import AccountCreate, CustomerCreate, ProductCreate
from app.modules.receivables import service as receivables_service
from app.modules.receivables.schemas import InvoiceCreate, PaymentCreate
from app.modules.sales import service as sales_service
from app.modules.sales.models import SalesOrderStatus
from app.modules.sales.schemas import SalesOrderCreate, SalesOrderLineCreate
from app.platform.workflow import service as wf_service
from app.platform.workflow.schemas import (
    WorkflowDefinitionCreate,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)
from tests.sales._helpers import create_company, uom_id


@pytest.fixture
def super_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset(
            {
                "platform.workflow.read",
                "platform.workflow.manage",
                "platform.workflow.approve",
                "sales.order.create",
                "sales.order.read",
                "sales.order.update",
                "sales.order.confirm",
                "sales.order.ship",
                "sales.order.cancel",
                "receivables.invoice.create",
                "receivables.invoice.read",
                "receivables.invoice.void",
                "receivables.payment.create",
                "receivables.payment.read",
                "receivables.payment.void",
                "receivables.payment.allocate",
                "ledger.period.create",
                "ledger.period.read",
                "ledger.period.close",
                "ledger.journal_entry.create",
                "ledger.journal_entry.read",
                "ledger.journal_entry.reverse",
                "masterdata.customer.create",
                "masterdata.product.create",
                "masterdata.account.create",
            }
        ),
        is_system_actor=True,
    )


async def _setup_standard_chart(db_session: AsyncSession) -> dict[str, Any]:
    created = {}
    for code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
        ("1300", "Inventory", "asset"),
    ):
        acc = await masterdata_service.create_account(
            db_session,
            AccountCreate(
                code=code,
                name=name,
                type=acct_type,
                is_control=False,
            ),
        )
        created[code] = acc
    return created


@pytest.mark.asyncio
async def test_sales_order_confirm_workflow_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    super_actor: ActorContext,
) -> None:
    code = f"WFBZ_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    ea_uom_id = await uom_id(client, company_id)

    with company_context(company_id), actor_context(super_actor):
        # 1. Setup masterdata customer & product
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(
                code=f"CUST_{uuid.uuid4().hex[:6]}",
                name="Test Customer",
                currency_code="TWD",
                payment_terms_days=30,
            ),
        )
        prod = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_{uuid.uuid4().hex[:6]}",
                name="Widget",
                uom_id=ea_uom_id,
                list_price=Decimal("1000.00"),
                standard_cost=Decimal("600.00"),
            ),
        )

        # 2. Configure SO confirm workflow: min_amount = 500
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CONFIRM,
                name="SO High Value Approval",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Sales Director",
                        min_amount=Decimal("500.00"),
                        required_permission="sales.order.confirm",
                    )
                ],
            ),
        )

        # 3. Create Draft SO with amount 1000 (meets threshold 500)
        order = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[SalesOrderLineCreate(product_id=prod.id, qty=Decimal("1.000000"))],
            ),
        )
        assert order.total == Decimal("1000.000000")

        # 4. Attempt confirm directly -> 409 Conflict (approval required)
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await sales_service.confirm_order(db_session, order.id)

        # 5. Submit workflow instance
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order.id,
                transition=WorkflowTransition.CONFIRM,
            ),
        )
        assert inst.status == WorkflowInstanceStatus.PENDING.value
        assert inst.target_amount == Decimal("1000.000000")

        # 6. Attempt confirm while PENDING -> 409 Conflict
        with pytest.raises(
            WorkflowApprovalRequiredError, match="Workflow approval is still PENDING"
        ):
            await sales_service.confirm_order(db_session, order.id)

        # 7. Approve instance
        approved_inst = await wf_service.approve_workflow_instance(db_session, inst.id)
        assert approved_inst.status == WorkflowInstanceStatus.APPROVED.value

        # 8. Confirm succeeds -> order becomes CONFIRMED, instance becomes CONSUMED
        confirmed_order = await sales_service.confirm_order(db_session, order.id)
        assert confirmed_order.status is SalesOrderStatus.CONFIRMED

        reloaded_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert reloaded_inst.status == WorkflowInstanceStatus.CONSUMED.value
        assert reloaded_inst.consumed_at is not None

        # 9. Low-value order test: amount 100 < 500 -> passes transparently without workflow
        prod_cheap = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_CH_{uuid.uuid4().hex[:6]}",
                name="Cheap Widget",
                uom_id=ea_uom_id,
                list_price=Decimal("100.00"),
                standard_cost=Decimal("50.00"),
            ),
        )
        order_cheap = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[SalesOrderLineCreate(product_id=prod_cheap.id, qty=Decimal("1.000000"))],
            ),
        )
        confirmed_cheap = await sales_service.confirm_order(db_session, order_cheap.id)
        assert confirmed_cheap.status is SalesOrderStatus.CONFIRMED


@pytest.mark.asyncio
async def test_sales_order_cancel_workflow_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    super_actor: ActorContext,
) -> None:
    code = f"WFBZ_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    ea_uom_id = await uom_id(client, company_id)

    with company_context(company_id), actor_context(super_actor):
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(
                code=f"CUST_{uuid.uuid4().hex[:6]}",
                name="Test Customer",
                currency_code="TWD",
                payment_terms_days=30,
            ),
        )
        prod = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_{uuid.uuid4().hex[:6]}",
                name="Widget",
                uom_id=ea_uom_id,
                list_price=Decimal("800.00"),
                standard_cost=Decimal("400.00"),
            ),
        )

        # Configure SO cancel workflow: min_amount = 0
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.SALES_ORDER,
                transition=WorkflowTransition.CANCEL,
                name="SO Cancel Approval",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Sales Admin",
                        min_amount=Decimal("0.00"),
                        required_permission="sales.order.cancel",
                    )
                ],
            ),
        )

        order = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[SalesOrderLineCreate(product_id=prod.id, qty=Decimal("1.000000"))],
            ),
        )

        # Attempt cancel without approval -> 409
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await sales_service.cancel_order(db_session, order.id)

        # Submit and approve cancel
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.SALES_ORDER,
                entity_id=order.id,
                transition=WorkflowTransition.CANCEL,
            ),
        )
        assert inst.target_amount == Decimal("800.000000")
        await wf_service.approve_workflow_instance(db_session, inst.id)

        # Cancel succeeds
        cancelled_order = await sales_service.cancel_order(db_session, order.id)
        assert cancelled_order.status is SalesOrderStatus.CANCELLED

        reloaded_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert reloaded_inst.status == WorkflowInstanceStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_invoice_void_workflow_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    super_actor: ActorContext,
) -> None:
    code = f"WFBZ_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    ea_uom_id = await uom_id(client, company_id)

    with company_context(company_id), actor_context(super_actor):
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(
                code=f"CUST_{uuid.uuid4().hex[:6]}",
                name="Test Customer",
                currency_code="TWD",
                payment_terms_days=30,
            ),
        )
        prod = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_{uuid.uuid4().hex[:6]}",
                name="Widget",
                uom_id=ea_uom_id,
                list_price=Decimal("2000.00"),
                standard_cost=Decimal("1200.00"),
            ),
        )

        # Setup standard chart & inventory stock
        await _setup_standard_chart(db_session)
        await inventory_service.apply_stock_move(
            db_session,
            company_id=company_id,
            product_id=prod.id,
            qty_delta=Decimal("100.000000"),
            move_type=StockMoveType.ADJUSTMENT,
        )

        # Ship an order to enable invoicing
        order = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[SalesOrderLineCreate(product_id=prod.id, qty=Decimal("1.000000"))],
            ),
        )
        # Create period before ship
        await ledger_service.create_period(
            db_session, AccountingPeriodCreate(year=date.today().year, month=date.today().month)
        )
        await sales_service.confirm_order(db_session, order.id)
        await sales_service.ship_order(db_session, order.id)
        inv = await receivables_service.create_invoice(db_session, InvoiceCreate(order_id=order.id))
        assert inv.total == Decimal("2000.000000")

        # Configure invoice void workflow
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.INVOICE,
                transition=WorkflowTransition.VOID,
                name="Invoice Void Approval",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Billing Manager",
                        min_amount=Decimal("0.00"),
                        required_permission="receivables.invoice.void",
                    )
                ],
            ),
        )

        # Attempt void invoice directly -> 409
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await receivables_service.void_invoice(db_session, inv.id)

        # Submit and approve
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.INVOICE,
                entity_id=inv.id,
                transition=WorkflowTransition.VOID,
            ),
        )
        assert inst.target_amount == Decimal("2000.000000")
        await wf_service.approve_workflow_instance(db_session, inst.id)

        # Void succeeds
        voided_inv = await receivables_service.void_invoice(db_session, inv.id)
        assert voided_inv.status.value == "voided"

        reloaded_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert reloaded_inst.status == WorkflowInstanceStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_payment_void_workflow_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    super_actor: ActorContext,
) -> None:
    code = f"WFBZ_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    with company_context(company_id), actor_context(super_actor):
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(
                code=f"CUST_{uuid.uuid4().hex[:6]}",
                name="Test Customer",
                currency_code="TWD",
                payment_terms_days=30,
            ),
        )
        await _setup_standard_chart(db_session)
        await ledger_service.create_period(
            db_session, AccountingPeriodCreate(year=date.today().year, month=date.today().month)
        )

        payment = await receivables_service.create_payment(
            db_session,
            PaymentCreate(
                customer_id=cust.id,
                amount=Decimal("3500.000000"),
                external_ref=f"EXT_{uuid.uuid4().hex[:8]}",
            ),
        )

        # Configure payment void workflow
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.PAYMENT,
                transition=WorkflowTransition.VOID,
                name="Payment Void Approval",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Treasury Head",
                        min_amount=Decimal("1000.00"),
                        required_permission="receivables.payment.void",
                    )
                ],
            ),
        )

        # Direct void fails
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await receivables_service.void_payment(db_session, payment.id)

        # Submit and approve
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.PAYMENT,
                entity_id=payment.id,
                transition=WorkflowTransition.VOID,
            ),
        )
        assert inst.target_amount == Decimal("3500.000000")
        await wf_service.approve_workflow_instance(db_session, inst.id)

        # Void payment succeeds
        voided_payment = await receivables_service.void_payment(db_session, payment.id)
        assert voided_payment.status.value == "voided"

        reloaded_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert reloaded_inst.status == WorkflowInstanceStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_journal_entry_reverse_workflow_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    super_actor: ActorContext,
) -> None:
    code = f"WFBZ_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    with company_context(company_id), actor_context(super_actor):
        # Setup period & accounts
        today = date.today()
        await ledger_service.create_period(
            db_session, AccountingPeriodCreate(year=today.year, month=today.month)
        )
        acc_cash = await masterdata_service.create_account(
            db_session,
            AccountCreate(
                code=f"111_{uuid.uuid4().hex[:4]}",
                name="Bank Account",
                type="asset",
                is_control=False,
            ),
        )
        acc_exp = await masterdata_service.create_account(
            db_session,
            AccountCreate(
                code=f"511_{uuid.uuid4().hex[:4]}",
                name="Operating Expenses",
                type="expense",
                is_control=False,
            ),
        )

        # Post manual journal entry
        entry = await ledger_service.post_journal_entry(
            db_session,
            JournalEntryCreate(
                entry_date=today,
                lines=[
                    JournalLineCreate(
                        account_id=acc_exp.id,
                        currency_code="TWD",
                        txn_debit=Decimal("15000.000000"),
                        txn_credit=Decimal("0.000000"),
                        debit=Decimal("15000.000000"),
                        credit=Decimal("0.000000"),
                        exchange_rate=Decimal("1.0000000000"),
                    ),
                    JournalLineCreate(
                        account_id=acc_cash.id,
                        currency_code="TWD",
                        txn_debit=Decimal("0.000000"),
                        txn_credit=Decimal("15000.000000"),
                        debit=Decimal("0.000000"),
                        credit=Decimal("15000.000000"),
                        exchange_rate=Decimal("1.0000000000"),
                    ),
                ],
            ),
        )
        await db_session.commit()

        # Configure reversal workflow: threshold 10000
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                transition=WorkflowTransition.REVERSE,
                name="High Value Journal Reversal Flow",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Controller",
                        min_amount=Decimal("10000.00"),
                        required_permission="ledger.journal_entry.reverse",
                    )
                ],
            ),
        )

        # Attempt reverse directly -> 409
        with pytest.raises(WorkflowApprovalRequiredError, match="no approval instance exists"):
            await ledger_service.reverse_journal_entry(db_session, entry.id)

        # Submit instance -> target_amount resolved from SUM(lines.debit) == 15000
        inst = await wf_service.submit_workflow_instance(
            db_session,
            WorkflowInstanceSubmit(
                entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                entity_id=entry.id,
                transition=WorkflowTransition.REVERSE,
            ),
        )
        assert inst.target_amount == Decimal("15000.000000")
        assert inst.status == WorkflowInstanceStatus.PENDING.value

        # Approve instance
        await wf_service.approve_workflow_instance(db_session, inst.id)

        # Reverse journal entry succeeds
        reversal = await ledger_service.reverse_journal_entry(db_session, entry.id)
        assert reversal.reversal_of_id == entry.id

        reloaded_inst = await wf_service.get_workflow_instance(db_session, inst.id)
        assert reloaded_inst.status == WorkflowInstanceStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_journal_entry_reverse_control_account_defense(
    client: AsyncClient, db_session: AsyncSession, super_actor: ActorContext
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    with company_context(company_id), actor_context(super_actor):
        accs = await _setup_standard_chart(db_session)
        acc_ar = accs["1100"]  # Control account (in KERNEL_CONTROL_ACCOUNT_CODES)
        acc_rev = accs["4000"]

        today = date.today()
        await ledger_service.create_period(
            db_session, AccountingPeriodCreate(year=today.year, month=today.month)
        )

        # Post entry with source_type (simulating rule-driven posting touching control account)
        entry = await ledger_service.post_journal_entry(
            db_session,
            JournalEntryCreate(
                entry_date=today,
                description="Rule-driven invoice posting",
                source_type="receivables.invoice",
                source_id=uuid.uuid4(),
                lines=[
                    JournalLineCreate(
                        account_id=acc_ar.id,
                        currency_code="TWD",
                        txn_debit=Decimal("5000.000000"),
                        txn_credit=Decimal("0.000000"),
                        debit=Decimal("5000.000000"),
                        credit=Decimal("0.000000"),
                        exchange_rate=Decimal("1.0000000000"),
                    ),
                    JournalLineCreate(
                        account_id=acc_rev.id,
                        currency_code="TWD",
                        txn_debit=Decimal("0.000000"),
                        txn_credit=Decimal("5000.000000"),
                        debit=Decimal("0.000000"),
                        credit=Decimal("5000.000000"),
                        exchange_rate=Decimal("1.0000000000"),
                    ),
                ],
            ),
        )
        await db_session.commit()

        # Configure reversal workflow definition
        await wf_service.create_workflow_definition(
            db_session,
            WorkflowDefinitionCreate(
                entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                transition=WorkflowTransition.REVERSE,
                name="JE Reversal Gate",
                allow_self_approval=True,
                steps=[
                    WorkflowStepInput(
                        step_order=1,
                        step_name="Lead",
                        min_amount=Decimal("1000.00"),
                        required_permission="ledger.journal_entry.reverse",
                    )
                ],
            ),
        )

        # Attempting to submit workflow instance must fail-fast with DomainValidationError
        with pytest.raises(
            DomainValidationError,
            match="touches control account '1100'; reversal is forbidden \\(ADR-008 R16\\)",
        ):
            await wf_service.submit_workflow_instance(
                db_session,
                WorkflowInstanceSubmit(
                    entity_type=WorkflowEntityType.JOURNAL_ENTRY,
                    entity_id=entry.id,
                    transition=WorkflowTransition.REVERSE,
                ),
            )


@pytest.mark.asyncio
async def test_sales_order_confirm_missing_product_defense(
    client: AsyncClient, db_session: AsyncSession, super_actor: ActorContext
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    ea_uom_id = await uom_id(client, company_id)

    with company_context(company_id), actor_context(super_actor):
        cust = await masterdata_service.create_customer(
            db_session,
            CustomerCreate(
                code=f"CUST_{uuid.uuid4().hex[:6]}",
                name="Defense Customer",
                currency_code="TWD",
                payment_terms_days=30,
            ),
        )
        prod = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_{uuid.uuid4().hex[:6]}",
                name="Defense Product",
                uom_id=ea_uom_id,
                list_price=Decimal("150.000000"),
                standard_cost=Decimal("70.000000"),
            ),
        )

        order = await sales_service.create_order(
            db_session,
            SalesOrderCreate(
                customer_id=cust.id,
                lines=[
                    SalesOrderLineCreate(
                        product_id=prod.id,
                        qty=Decimal("2.000000"),
                        unit_price=Decimal("150.000000"),
                    )
                ],
            ),
        )
        await db_session.commit()

    # Create a product in a second company (simulating cross-company or missing tenant product)
    code_other = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_other_id = await create_company(client, code_other)
    ea_other_uom = await uom_id(client, company_other_id)
    with company_context(company_other_id), actor_context(super_actor):
        prod_other = await masterdata_service.create_product(
            db_session,
            ProductCreate(
                sku=f"SKU_{uuid.uuid4().hex[:6]}",
                name="Other Company Product",
                uom_id=ea_other_uom,
                list_price=Decimal("999.000000"),
                standard_cost=Decimal("500.000000"),
            ),
        )
        await db_session.commit()

    with company_context(company_id), actor_context(super_actor):
        import sqlalchemy as sa

        from app.platform.workflow.service import (
            _SALES_ORDER_LINES,
            resolve_workflow_target_unchecked,
        )

        # Update line product_id to the other company's product
        # (satisfies DB FK, but invalid for this tenant)
        await db_session.execute(
            sa.update(_SALES_ORDER_LINES)
            .where(_SALES_ORDER_LINES.c.order_id == order.id)
            .values(product_id=prod_other.id)
        )
        await db_session.commit()

        # Target resolver must fail-fast instead of silently omitting the line
        with pytest.raises(
            DomainValidationError,
            match="line references invalid or missing product",
        ):
            await resolve_workflow_target_unchecked(
                db_session,
                entity_type=WorkflowEntityType.SALES_ORDER.value,
                entity_id=order.id,
                transition=WorkflowTransition.CONFIRM.value,
            )
