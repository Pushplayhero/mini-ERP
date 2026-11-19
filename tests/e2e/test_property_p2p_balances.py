"""tests/e2e/test_property_p2p_balances.py — Hypothesis property test over P2P sequences."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.main  # noqa: F401  (side effect: composition-root event wiring)
from app.core.authorization import actor_context
from app.core.tenancy import company_context
from app.modules.inventory import service as inventory_service
from app.modules.ledger import service as ledger_service
from app.modules.ledger.schemas import AccountingPeriodCreate
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import AccountType
from app.modules.masterdata.schemas import (
    AccountCreate,
    CompanyCreate,
    ProductCreate,
    SupplierCreate,
)
from app.modules.payables import service as payables_service
from app.modules.payables.models import (
    VendorBill,
    VendorBillStatus,
    VendorPayment,
    VendorPaymentAllocation,
)
from app.modules.payables.schemas import (
    VendorBillCreate,
    VendorPaymentAllocationIn,
    VendorPaymentCreate,
)
from app.modules.purchase import service as purchase_service
from app.modules.purchase.models import PurchaseOrder, PurchaseOrderStatus
from app.modules.purchase.schemas import PurchaseOrderCreate, PurchaseOrderLineCreate
from tests.conftest import TEST_ADMIN_ACTOR

_ZERO = Decimal("0")
_STANDARD_COST = Decimal("25")
_STANDARD_CHART = (
    ("1000", "Cash", AccountType.ASSET),
    ("1300", "Inventory", AccountType.ASSET),
    ("2000", "Accounts Payable", AccountType.LIABILITY),
    ("2110", "Accrued Purchases", AccountType.LIABILITY),
)

_MAX_ORDERS = 4
_MAX_PAYMENTS = 3
_MAX_EXTRA_STEPS = 10
_MAX_QTY = 5
_MAX_PAY_AMOUNT = 300


@dataclass(frozen=True)
class NewPO:
    qty: int


@dataclass(frozen=True)
class ConfirmPO:
    order_idx: int


@dataclass(frozen=True)
class ReceivePO:
    order_idx: int


@dataclass(frozen=True)
class IssueBill:
    order_idx: int


@dataclass(frozen=True)
class VoidBill:
    bill_idx: int


@dataclass(frozen=True)
class PayVendor:
    amount: int


@dataclass(frozen=True)
class VoidVendorPay:
    payment_idx: int


@dataclass(frozen=True)
class AllocateVendorPay:
    payment_idx: int
    bill_idx: int
    amount: int


P2POp = (
    NewPO
    | ConfirmPO
    | ReceivePO
    | IssueBill
    | VoidBill
    | PayVendor
    | VoidVendorPay
    | AllocateVendorPay
)


@dataclass
class _POState:
    status: str  # "draft" | "confirmed" | "received"
    qty: int
    live_bill_idx: int | None = None


@dataclass
class _BillState:
    order_idx: int
    open_balance: int
    settled: int = 0
    voided: bool = False


@dataclass
class _PayState:
    unapplied: int
    allocated: int = 0
    voided: bool = False


def _draw_plan(data: st.DataObject) -> list[P2POp]:
    orders: list[_POState] = []
    bills: list[_BillState] = []
    payments: list[_PayState] = []
    plan: list[P2POp] = []

    seed_qty = data.draw(st.integers(min_value=1, max_value=_MAX_QTY), label="seed_qty")
    plan.extend([NewPO(seed_qty), ConfirmPO(0), ReceivePO(0)])
    orders.append(_POState(status="received", qty=seed_qty))

    extra_steps = data.draw(st.integers(min_value=0, max_value=_MAX_EXTRA_STEPS), label="steps")
    for _ in range(extra_steps):
        draft_idxs = [i for i, o in enumerate(orders) if o.status == "draft"]
        confirmed_idxs = [i for i, o in enumerate(orders) if o.status == "confirmed"]
        billable_idxs = [
            i for i, o in enumerate(orders) if o.status == "received" and o.live_bill_idx is None
        ]
        voidable_bill_idxs = [i for i, b in enumerate(bills) if not b.voided and b.settled == 0]
        voidable_pay_idxs = [i for i, p in enumerate(payments) if not p.voided and p.allocated == 0]
        alloc_pay_idxs = [i for i, p in enumerate(payments) if not p.voided and p.unapplied > 0]
        open_bill_idxs = [i for i, b in enumerate(bills) if not b.voided and b.open_balance > 0]

        actions: list[str] = []
        if len(orders) < _MAX_ORDERS:
            actions.append("new_po")
        if draft_idxs:
            actions.append("confirm_po")
        if confirmed_idxs:
            actions.append("receive_po")
        if billable_idxs:
            actions.append("issue_bill")
        if voidable_bill_idxs:
            actions.append("void_bill")
        if len(payments) < _MAX_PAYMENTS:
            actions.append("pay")
        if voidable_pay_idxs:
            actions.append("void_pay")
        if alloc_pay_idxs and open_bill_idxs:
            actions.append("allocate")

        if not actions:
            break

        action = data.draw(st.sampled_from(actions), label="action")
        if action == "new_po":
            qty = data.draw(st.integers(min_value=1, max_value=_MAX_QTY), label="qty")
            plan.append(NewPO(qty))
            orders.append(_POState(status="draft", qty=qty))
        elif action == "confirm_po":
            idx = data.draw(st.sampled_from(draft_idxs), label="confirm_idx")
            plan.append(ConfirmPO(idx))
            orders[idx].status = "confirmed"
        elif action == "receive_po":
            idx = data.draw(st.sampled_from(confirmed_idxs), label="receive_idx")
            plan.append(ReceivePO(idx))
            orders[idx].status = "received"
        elif action == "issue_bill":
            idx = data.draw(st.sampled_from(billable_idxs), label="bill_order_idx")
            plan.append(IssueBill(idx))
            total = orders[idx].qty * int(_STANDARD_COST)
            bills.append(_BillState(order_idx=idx, open_balance=total))
            orders[idx].live_bill_idx = len(bills) - 1
        elif action == "void_bill":
            b_idx = data.draw(st.sampled_from(voidable_bill_idxs), label="void_bill_idx")
            plan.append(VoidBill(b_idx))
            bills[b_idx].voided = True
            orders[bills[b_idx].order_idx].live_bill_idx = None
        elif action == "pay":
            amt = data.draw(st.integers(min_value=1, max_value=_MAX_PAY_AMOUNT), label="pay_amt")
            plan.append(PayVendor(amt))
            payments.append(_PayState(unapplied=amt))
        elif action == "void_pay":
            p_idx = data.draw(st.sampled_from(voidable_pay_idxs), label="void_pay_idx")
            plan.append(VoidVendorPay(p_idx))
            payments[p_idx].voided = True
        elif action == "allocate":
            p_idx = data.draw(st.sampled_from(alloc_pay_idxs), label="alloc_pay_idx")
            b_idx = data.draw(st.sampled_from(open_bill_idxs), label="alloc_bill_idx")
            max_amt = min(payments[p_idx].unapplied, bills[b_idx].open_balance)
            amt = data.draw(st.integers(min_value=1, max_value=max_amt), label="alloc_amt")
            plan.append(AllocateVendorPay(p_idx, b_idx, amt))
            payments[p_idx].unapplied -= amt
            payments[p_idx].allocated += amt
            bills[b_idx].open_balance -= amt
            bills[b_idx].settled += amt

    return plan


async def _execute_plan(
    session: AsyncSession, plan: list[P2POp], supplier_id: uuid.UUID, product_id: uuid.UUID
) -> Decimal:
    order_ids: list[uuid.UUID] = []
    bill_ids: list[uuid.UUID] = []
    payment_ids: list[uuid.UUID] = []
    received_utc_dates: dict[int, date] = {}
    expected_received_qty = Decimal("0")

    for step, op in enumerate(plan):
        if isinstance(op, NewPO):
            po = await purchase_service.create_order(
                session,
                PurchaseOrderCreate(
                    supplier_id=supplier_id,
                    lines=[PurchaseOrderLineCreate(product_id=product_id, qty=Decimal(op.qty))],
                ),
            )
            order_ids.append(po.id)
        elif isinstance(op, ConfirmPO):
            await purchase_service.confirm_order(session, order_ids[op.order_idx])
            await session.commit()
        elif isinstance(op, ReceivePO):
            po = await purchase_service.receive_order(session, order_ids[op.order_idx])
            await session.commit()
            assert po.received_at is not None
            received_utc_dates[op.order_idx] = po.received_at.astimezone(timezone.utc).date()  # noqa: UP017
            expected_received_qty += po.total / _STANDARD_COST
        elif isinstance(op, IssueBill):
            bill = await payables_service.create_bill(
                session,
                VendorBillCreate(
                    order_id=order_ids[op.order_idx],
                    bill_date=received_utc_dates[op.order_idx],
                ),
            )
            await session.commit()
            bill_ids.append(bill.id)
        elif isinstance(op, VoidBill):
            await payables_service.void_bill(session, bill_ids[op.bill_idx])
            await session.commit()
        elif isinstance(op, PayVendor):
            pay = await payables_service.create_payment(
                session,
                VendorPaymentCreate(
                    supplier_id=supplier_id,
                    external_ref=f"hyp-apay-{step}",
                    amount=Decimal(op.amount),
                ),
            )
            await session.commit()
            payment_ids.append(pay.id)
        elif isinstance(op, VoidVendorPay):
            await payables_service.void_payment(session, payment_ids[op.payment_idx])
            await session.commit()
        elif isinstance(op, AllocateVendorPay):
            await payables_service.allocate_payment(
                session,
                payment_ids[op.payment_idx],
                f"hyp-aalloc-{step}",
                [
                    VendorPaymentAllocationIn(
                        bill_id=bill_ids[op.bill_idx], amount=Decimal(op.amount)
                    )
                ],
            )
            await session.commit()

    return expected_received_qty


async def _assert_invariants(
    session: AsyncSession, product_id: uuid.UUID, expected_qty: Decimal
) -> None:
    trial_balance = await ledger_service.get_trial_balance(session)
    total_debit = sum((line.total_debit for line in trial_balance), _ZERO)
    total_credit = sum((line.total_credit for line in trial_balance), _ZERO)
    assert total_debit == total_credit

    # 1. AP Aging tie-out to 2000 control account (Credit - Debit)
    line_2000 = next((line for line in trial_balance if line.account_code == "2000"), None)
    ledger_2000_net = (
        (line_2000.total_credit - line_2000.total_debit) if line_2000 is not None else _ZERO
    )
    aging_rows = await payables_service.get_ap_aging(session)
    aging_net = sum((row.net_total for row in aging_rows), _ZERO)
    assert aging_net == ledger_2000_net

    # 2. Unbilled RECEIVED PO tie-out to 2110 GR/IR clearing account (Credit - Debit)
    line_2110 = next((line for line in trial_balance if line.account_code == "2110"), None)
    ledger_2110_net = (
        (line_2110.total_credit - line_2110.total_debit) if line_2110 is not None else _ZERO
    )
    unbilled_po_total = (
        await session.scalar(
            select(func.coalesce(func.sum(PurchaseOrder.total), _ZERO)).where(
                PurchaseOrder.status == PurchaseOrderStatus.RECEIVED,
                ~PurchaseOrder.id.in_(
                    select(VendorBill.order_id).where(VendorBill.status != VendorBillStatus.VOIDED)
                ),
            )
        )
        or _ZERO
    )
    assert unbilled_po_total == ledger_2110_net

    # 3. Warehouse stock equals exact received qty
    stock_rows = await inventory_service.list_stock_summary(session, product_id=product_id)
    assert len(stock_rows) == 1
    assert stock_rows[0].on_hand == expected_qty

    # 4. Subledger maintained projections tie to SUM(vendor_payment_allocations.amount)
    alloc_sum = (
        await session.scalar(select(func.coalesce(func.sum(VendorPaymentAllocation.amount), _ZERO)))
        or _ZERO
    )
    bills_settled_sum = (
        await session.scalar(select(func.coalesce(func.sum(VendorBill.settled_amount), _ZERO)))
        or _ZERO
    )
    pays_allocated_sum = (
        await session.scalar(select(func.coalesce(func.sum(VendorPayment.allocated_amount), _ZERO)))
        or _ZERO
    )
    assert alloc_sum == bills_settled_sum == pays_allocated_sum


async def _run_example(dsn: str, plan: list[P2POp]) -> None:
    engine = create_async_engine(dsn)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        with actor_context(TEST_ADMIN_ACTOR):
            async with session_factory() as session:
                company = await masterdata_service.create_company(
                    session,
                    CompanyCreate(
                        code=f"HYPP2P{uuid.uuid4().hex[:10].upper()}",
                        name="Hypothesis P2P Co",
                        functional_currency_code="TWD",
                    ),
                )
                with company_context(company.id):
                    for code, name, acct_type in _STANDARD_CHART:
                        await masterdata_service.create_account(
                            session, AccountCreate(code=code, name=name, type=acct_type)
                        )
                    local_today = date.today()
                    utc_today = datetime.now(timezone.utc).date()  # noqa: UP017
                    periods_needed: set[tuple[int, int]] = set()
                    for basis in (local_today, utc_today):
                        periods_needed.add((basis.year, basis.month))
                        next_month = (basis.replace(day=28) + timedelta(days=4)).replace(day=1)
                        periods_needed.add((next_month.year, next_month.month))
                    for year, month in periods_needed:
                        await ledger_service.create_period(
                            session, AccountingPeriodCreate(year=year, month=month)
                        )
                    supplier = await masterdata_service.create_supplier(
                        session,
                        SupplierCreate(
                            code=f"HYPSUP{uuid.uuid4().hex[:8].upper()}",
                            name="Hypothesis Supplier",
                            currency_code="TWD",
                            payment_terms_days=30,
                        ),
                    )
                    uoms = await masterdata_service.list_uoms(session)
                    ea_id = next(u.id for u in uoms if u.code == "EA")
                    product = await masterdata_service.create_product(
                        session,
                        ProductCreate(
                            sku=f"HYPP2P{uuid.uuid4().hex[:8].upper()}",
                            name="Hypothesis P2P Material",
                            uom_id=ea_id,
                            list_price=Decimal("100"),
                            standard_cost=_STANDARD_COST,
                        ),
                    )
                    expected_qty = await _execute_plan(session, plan, supplier.id, product.id)
                    await _assert_invariants(session, product.id, expected_qty)
    finally:
        await engine.dispose()


def test_property_p2p_balances_hold_under_any_legal_operation_sequence(
    postgres_dsn: str,
) -> None:
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(data=st.data())
    def run_example(data: st.DataObject) -> None:
        plan = _draw_plan(data)
        asyncio.run(_run_example(postgres_dsn, plan))

    run_example()
