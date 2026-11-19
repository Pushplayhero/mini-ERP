"""payables service layer — business logic + transaction boundary (ADR-015).

Flush-only cores mirror `app.modules.receivables.service`; router owns
commit + `IntegrityError -> 409` translation. Cross-module lookups read
`purchase_orders` and `suppliers` via Core `sqlalchemy.table()` with
explicit `company_id` predicates (import-linter compliant).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, literal, null, select, union_all
from sqlalchemy import cast as sa_cast
from sqlalchemy import column as sa_column
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date as SA_Date
from sqlalchemy.types import Integer

from app.core import events
from app.core.authorization import require_permission
from app.core.customfields import CustomFieldEntityType, validate_and_normalize_custom_data
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.tenancy import require_current_company_id
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowTransition,
    verify_workflow_approval,
)
from app.modules.payables.events import (
    PAYABLES_BILL_ISSUED_EVENT_TYPE,
    PAYABLES_BILL_VOIDED_EVENT_TYPE,
    PAYABLES_PAYMENT_ISSUED_EVENT_TYPE,
    PAYABLES_PAYMENT_VOIDED_EVENT_TYPE,
)
from app.modules.payables.models import (
    AMOUNT,
    PayablesSequence,
    VendorBill,
    VendorBillStatus,
    VendorPayment,
    VendorPaymentAllocation,
    VendorPaymentAllocationCommand,
    VendorPaymentStatus,
)
from app.modules.payables.schemas import (
    APAgingRow,
    VendorBillCreate,
    VendorPaymentAllocationIn,
    VendorPaymentCreate,
)

_PHASE_1_CURRENCY = "TWD"
_ZERO = Decimal("0.000000")

_PURCHASE_ORDERS = sa_table(
    "purchase_orders",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("order_no"),
    sa_column("supplier_id"),
    sa_column("status"),
    sa_column("currency_code"),
    sa_column("total", AMOUNT),
    sa_column("received_at"),
    sa_column("snapshot_supplier_code"),
    sa_column("snapshot_supplier_name"),
)

_SUPPLIERS = sa_table(
    "suppliers",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("code"),
    sa_column("name"),
    sa_column("currency_code"),
    sa_column("payment_terms_days"),
)


# ---------------------------------------------------------------------------
# Purchase Order / Supplier Core Lookups
# ---------------------------------------------------------------------------


async def _fetch_purchase_order(
    session: AsyncSession, company_id: uuid.UUID, order_id: uuid.UUID
) -> Any:
    result = await session.execute(
        select(
            _PURCHASE_ORDERS.c.id,
            _PURCHASE_ORDERS.c.order_no,
            _PURCHASE_ORDERS.c.supplier_id,
            _PURCHASE_ORDERS.c.status,
            _PURCHASE_ORDERS.c.currency_code,
            _PURCHASE_ORDERS.c.total,
            _PURCHASE_ORDERS.c.received_at,
            _PURCHASE_ORDERS.c.snapshot_supplier_code,
            _PURCHASE_ORDERS.c.snapshot_supplier_name,
        ).where(_PURCHASE_ORDERS.c.id == order_id, _PURCHASE_ORDERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"order_id {order_id} not found or does not belong to this company"
        )
    return row


async def _fetch_supplier(
    session: AsyncSession, company_id: uuid.UUID, supplier_id: uuid.UUID
) -> Any:
    result = await session.execute(
        select(
            _SUPPLIERS.c.id,
            _SUPPLIERS.c.code,
            _SUPPLIERS.c.name,
            _SUPPLIERS.c.currency_code,
            _SUPPLIERS.c.payment_terms_days,
        ).where(_SUPPLIERS.c.id == supplier_id, _SUPPLIERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"supplier_id {supplier_id} not found or does not belong to this company"
        )
    return row


async def _fetch_suppliers(
    session: AsyncSession, company_id: uuid.UUID, supplier_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not supplier_ids:
        return {}
    result = await session.execute(
        select(_SUPPLIERS.c.id, _SUPPLIERS.c.code, _SUPPLIERS.c.name).where(
            _SUPPLIERS.c.id.in_(supplier_ids), _SUPPLIERS.c.company_id == company_id
        )
    )
    return {row.id: row for row in result.all()}


# ---------------------------------------------------------------------------
# Document Numbering (not required to be gapless — ADR-015)
# ---------------------------------------------------------------------------


async def _allocate_doc_no(
    session: AsyncSession, company_id: uuid.UUID, year: int, doc_type: str
) -> str:
    await session.execute(
        pg_insert(PayablesSequence)
        .values(company_id=company_id, year=year, doc_type=doc_type, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year", "doc_type"])
    )
    result = await session.execute(
        select(PayablesSequence)
        .where(
            PayablesSequence.company_id == company_id,
            PayablesSequence.year == year,
            PayablesSequence.doc_type == doc_type,
        )
        .with_for_update()
    )
    seq = result.scalar_one()
    allocated_no = seq.next_no
    seq.next_no = allocated_no + 1
    prefix = "BILL" if doc_type == "bill" else "APAY"
    return f"{prefix}-{year:04d}-{allocated_no:06d}"


# ---------------------------------------------------------------------------
# Vendor Bills (ADR-015 Decision 1 / Decision 4)
# ---------------------------------------------------------------------------


def _validate_bill_dates(
    order_received_at: datetime,
    bill_date: date,
    due_date: date,
) -> None:
    received_utc_date = order_received_at.astimezone(timezone.utc).date()  # noqa: UP017
    if bill_date < received_utc_date:
        raise DomainValidationError(
            f"bill_date {bill_date} cannot precede the purchase order receipt date "
            f"{received_utc_date} (ADR-015 — GR/IR clearing requires prior receipt)"
        )
    if due_date < bill_date:
        raise DomainValidationError(f"due_date {due_date} cannot precede bill_date {bill_date}")


async def create_bill(session: AsyncSession, data: VendorBillCreate) -> VendorBill:
    """Issue a 1:1 vendor bill from a RECEIVED purchase order (flush-only)."""
    require_permission("payables.bill.create")
    company_id = require_current_company_id()
    order = await _fetch_purchase_order(session, company_id, data.order_id)

    if str(order.status).upper() != "RECEIVED" or order.received_at is None:
        raise DomainValidationError(
            f"PurchaseOrder {data.order_id} is not received (status={order.status}); "
            "cannot issue vendor bill (ADR-015 Decision 1)"
        )
    if order.total <= 0:
        raise DomainValidationError(
            f"PurchaseOrder {data.order_id} has non-positive total {order.total}; cannot bill"
        )
    if order.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"PurchaseOrder {data.order_id} is denominated in {order.currency_code!r}, "
            f"not {_PHASE_1_CURRENCY!r} — payables is TWD-only (ADR-015)"
        )

    supplier = await _fetch_supplier(session, company_id, order.supplier_id)
    if supplier.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"Supplier {order.supplier_id} is denominated in {supplier.currency_code!r}, "
            f"not {_PHASE_1_CURRENCY!r} — payables is TWD-only (ADR-015)"
        )

    order_received_at: datetime = order.received_at
    bill_date = data.bill_date or datetime.now(timezone.utc).date()  # noqa: UP017
    due_date = (
        data.due_date
        if data.due_date is not None
        else (bill_date + timedelta(days=supplier.payment_terms_days))
    )
    _validate_bill_dates(order_received_at, bill_date, due_date)

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.VENDOR_BILL,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    bill_no = await _allocate_doc_no(session, company_id, bill_date.year, "bill")
    bill = VendorBill(
        company_id=company_id,
        bill_no=bill_no,
        order_id=order.id,
        supplier_id=order.supplier_id,
        supplier_invoice_no=data.supplier_invoice_no,
        status=VendorBillStatus.OPEN,
        currency_code=order.currency_code,
        order_received_at=order_received_at,
        bill_date=bill_date,
        due_date=due_date,
        total=order.total,
        settled_amount=_ZERO,
        snapshot_supplier_code=order.snapshot_supplier_code,
        snapshot_supplier_name=order.snapshot_supplier_name,
        custom_data=normalized_custom_data,
    )
    session.add(bill)
    # Flush eagerly to enforce uq_vendor_bills_order_live & uq_vendor_bills_supplier_invoice_no_live
    await session.flush()

    await events.publish(
        session,
        PAYABLES_BILL_ISSUED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": bill.id,
            "event_date": bill_date,
            "bill_no": bill_no,
            "order_id": order.id,
            "supplier_id": order.supplier_id,
            "total": order.total,
        },
    )
    await session.flush()
    return bill


async def load_bill_unchecked(session: AsyncSession, bill_id: uuid.UUID) -> VendorBill:
    result = await session.execute(select(VendorBill).where(VendorBill.id == bill_id))
    bill = result.scalar_one_or_none()
    if bill is None:
        raise NotFoundError("VendorBill", bill_id)
    return bill


async def get_bill(session: AsyncSession, bill_id: uuid.UUID) -> VendorBill:
    require_permission("payables.bill.read")
    return await load_bill_unchecked(session, bill_id)


async def list_bills(session: AsyncSession) -> Sequence[VendorBill]:
    require_permission("payables.bill.read")
    result = await session.execute(select(VendorBill).order_by(VendorBill.bill_no))
    return result.scalars().all()


async def void_bill(session: AsyncSession, bill_id: uuid.UUID) -> VendorBill:
    """Void an unsettled vendor bill (flush-only, SELECT ... FOR UPDATE)."""
    require_permission("payables.bill.void")
    result = await session.execute(
        select(VendorBill).where(VendorBill.id == bill_id).with_for_update()
    )
    bill = result.scalar_one_or_none()
    if bill is None:
        raise NotFoundError("VendorBill", bill_id)
    if bill.status == VendorBillStatus.VOIDED:
        raise ConflictError(f"VendorBill {bill_id} is already voided")
    if bill.settled_amount != 0:
        raise ConflictError(
            f"VendorBill {bill_id} has settled_amount={bill.settled_amount} != 0; "
            "cannot void a partially or fully settled vendor bill (ADR-015)"
        )

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.VENDOR_BILL,
        entity_id=bill.id,
        transition=WorkflowTransition.VOID,
        amount=bill.total,
    )

    company_id = require_current_company_id()
    bill.status = VendorBillStatus.VOIDED
    bill.voided_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        PAYABLES_BILL_VOIDED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": bill.id,
            "event_date": bill.voided_at.date(),
            "bill_no": bill.bill_no,
            "total": bill.total,
        },
    )
    await session.flush()
    return bill


# ---------------------------------------------------------------------------
# Vendor Payments (ADR-015 Decision 2 / Decision 4)
# ---------------------------------------------------------------------------


async def create_payment(session: AsyncSession, data: VendorPaymentCreate) -> VendorPayment:
    """Create an outgoing vendor payment and optionally allocate inline (flush-only)."""
    require_permission("payables.payment.create")
    company_id = require_current_company_id()
    supplier = await _fetch_supplier(session, company_id, data.supplier_id)
    if supplier.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"Supplier {data.supplier_id} is denominated in {supplier.currency_code!r}, "
            f"not {_PHASE_1_CURRENCY!r} — payables is TWD-only (ADR-015)"
        )

    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.VENDOR_PAYMENT,
        raw_custom_data=data.custom_data,
        is_create=True,
    )

    paid_at = data.paid_at or datetime.now(timezone.utc)  # noqa: UP017
    payment_no = await _allocate_doc_no(session, company_id, paid_at.year, "payment")

    payment = VendorPayment(
        company_id=company_id,
        payment_no=payment_no,
        supplier_id=data.supplier_id,
        status=VendorPaymentStatus.ISSUED,
        external_ref=data.external_ref,
        currency_code=_PHASE_1_CURRENCY,
        amount=data.amount,
        allocated_amount=_ZERO,
        paid_at=paid_at,
        custom_data=normalized_custom_data,
    )
    session.add(payment)
    await session.flush()

    await events.publish(
        session,
        PAYABLES_PAYMENT_ISSUED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": payment.id,
            "event_date": paid_at.date(),
            "payment_no": payment_no,
            "supplier_id": data.supplier_id,
            "amount": data.amount,
        },
    )
    await session.flush()

    if data.allocations:
        require_permission("payables.payment.allocate")
        await allocate_payment_core(session, payment.id, data.external_ref, data.allocations)

    await session.flush()
    return payment


async def load_payment_unchecked(session: AsyncSession, payment_id: uuid.UUID) -> VendorPayment:
    result = await session.execute(select(VendorPayment).where(VendorPayment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("VendorPayment", payment_id)
    return payment


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> VendorPayment:
    require_permission("payables.payment.read")
    return await load_payment_unchecked(session, payment_id)


async def load_payment_by_external_ref_unchecked(
    session: AsyncSession, external_ref: str
) -> VendorPayment | None:
    result = await session.execute(
        select(VendorPayment).where(VendorPayment.external_ref == external_ref)
    )
    return result.scalar_one_or_none()


async def get_payment_by_external_ref(
    session: AsyncSession, external_ref: str
) -> VendorPayment | None:
    require_permission("payables.payment.read")
    return await load_payment_by_external_ref_unchecked(session, external_ref)


async def list_payments(session: AsyncSession) -> Sequence[VendorPayment]:
    require_permission("payables.payment.read")
    result = await session.execute(select(VendorPayment).order_by(VendorPayment.payment_no))
    return result.scalars().all()


async def void_payment(session: AsyncSession, payment_id: uuid.UUID) -> VendorPayment:
    """Void an unallocated outgoing vendor payment (flush-only, SELECT ... FOR UPDATE)."""
    require_permission("payables.payment.void")
    result = await session.execute(
        select(VendorPayment).where(VendorPayment.id == payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("VendorPayment", payment_id)
    if payment.status == VendorPaymentStatus.VOIDED:
        raise ConflictError(f"VendorPayment {payment_id} is already voided")
    if payment.allocated_amount != 0:
        raise ConflictError(
            f"VendorPayment {payment_id} has allocated_amount={payment.allocated_amount} != 0; "
            "cannot void a payment with active allocations (ADR-015)"
        )

    await verify_workflow_approval(
        session,
        entity_type=WorkflowEntityType.VENDOR_PAYMENT,
        entity_id=payment.id,
        transition=WorkflowTransition.VOID,
        amount=payment.amount,
    )

    company_id = require_current_company_id()
    payment.status = VendorPaymentStatus.VOIDED
    payment.voided_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        PAYABLES_PAYMENT_VOIDED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": payment.id,
            "event_date": payment.voided_at.date(),
            "payment_no": payment.payment_no,
            "amount": payment.amount,
        },
    )
    await session.flush()
    return payment


# ---------------------------------------------------------------------------
# Subledger Allocations (ADR-015 Decision 2/3, ADR-008 R14 pattern)
# ---------------------------------------------------------------------------


def _fingerprint_allocations(allocations: Sequence[VendorPaymentAllocationIn]) -> str:
    normalized = sorted((str(a.bill_id), str(a.amount)) for a in allocations)
    raw = "|".join(f"{bill_id}:{amount}" for bill_id, amount in normalized)
    return hashlib.sha256(raw.encode()).hexdigest()


def _validate_target_bills_for_payment(
    payment: VendorPayment,
    bills_by_id: dict[uuid.UUID, VendorBill],
    allocations_in: Sequence[VendorPaymentAllocationIn],
) -> Decimal:
    for bill in bills_by_id.values():
        if bill.status == VendorBillStatus.VOIDED:
            raise DomainValidationError(f"bill {bill.id} is voided; cannot allocate to it")
        if bill.supplier_id != payment.supplier_id:
            raise DomainValidationError(
                f"bill {bill.id} belongs to a different supplier than payment {payment.id}"
            )
        if bill.currency_code != payment.currency_code:
            raise DomainValidationError(
                f"bill {bill.id}'s currency ({bill.currency_code}) does not match payment "
                f"{payment.id}'s currency ({payment.currency_code})"
            )

    total_requested = sum((a.amount for a in allocations_in), Decimal("0"))
    if payment.allocated_amount + total_requested > payment.amount:
        raise ConflictError(
            f"allocation of {total_requested} would exceed payment {payment.id}'s remaining "
            f"capacity ({payment.amount - payment.allocated_amount})"
        )
    for item in allocations_in:
        bill = bills_by_id[item.bill_id]
        if bill.settled_amount + item.amount > bill.total:
            raise ConflictError(
                f"allocation of {item.amount} would exceed bill {bill.id}'s remaining balance "
                f"({bill.total - bill.settled_amount})"
            )
    return total_requested


async def allocate_payment_core(
    session: AsyncSession,
    payment_id: uuid.UUID,
    request_ref: str,
    allocations_in: Sequence[VendorPaymentAllocationIn],
) -> VendorPayment:
    company_id = require_current_company_id()
    if not allocations_in:
        raise DomainValidationError("allocations must be non-empty")

    bill_ids = [a.bill_id for a in allocations_in]
    if len(set(bill_ids)) != len(bill_ids):
        raise DomainValidationError(
            "duplicate bill_id targets within one allocation request are not allowed (ADR-015)"
        )

    result = await session.execute(
        select(VendorPayment).where(VendorPayment.id == payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("VendorPayment", payment_id)
    if payment.status == VendorPaymentStatus.VOIDED:
        raise ConflictError(f"VendorPayment {payment_id} is voided; cannot allocate")

    fingerprint = _fingerprint_allocations(allocations_in)
    existing_cmd = (
        await session.execute(
            select(VendorPaymentAllocationCommand).where(
                VendorPaymentAllocationCommand.payment_id == payment_id,
                VendorPaymentAllocationCommand.request_ref == request_ref,
            )
        )
    ).scalar_one_or_none()
    if existing_cmd is not None:
        if existing_cmd.request_fingerprint == fingerprint:
            return payment
        raise ConflictError(
            f"request_ref {request_ref!r} was already used for payment {payment_id} with a "
            "different allocation set — reusing a reference for a different command body is "
            "not allowed (ADR-015)"
        )

    sorted_ids = sorted(set(bill_ids), key=str)
    bills_result = await session.execute(
        select(VendorBill)
        .where(VendorBill.id.in_(sorted_ids))
        .order_by(VendorBill.id)
        .with_for_update()
    )
    bills_by_id = {bill.id: bill for bill in bills_result.scalars().all()}
    missing = set(sorted_ids) - bills_by_id.keys()
    if missing:
        missing_str = sorted(str(i) for i in missing)
        raise DomainValidationError(
            f"bill_id(s) not found or do not belong to this company: {missing_str}"
        )

    total_requested = _validate_target_bills_for_payment(payment, bills_by_id, allocations_in)

    command = VendorPaymentAllocationCommand(
        company_id=company_id,
        payment_id=payment_id,
        request_ref=request_ref,
        request_fingerprint=fingerprint,
    )
    session.add(command)
    await session.flush()

    for item in allocations_in:
        bill = bills_by_id[item.bill_id]
        session.add(
            VendorPaymentAllocation(
                company_id=company_id,
                payment_id=payment_id,
                bill_id=item.bill_id,
                command_id=command.id,
                amount=item.amount,
            )
        )
        bill.settled_amount += item.amount
        if bill.settled_amount >= bill.total:
            bill.status = VendorBillStatus.PAID
        elif bill.settled_amount > 0:
            bill.status = VendorBillStatus.PARTIAL

    payment.allocated_amount += total_requested
    await session.flush()
    return payment


async def allocate_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    request_ref: str,
    allocations_in: Sequence[VendorPaymentAllocationIn],
) -> VendorPayment:
    """Allocate an issued vendor payment across one or more vendor bills (flush-only)."""
    require_permission("payables.payment.allocate")
    return await allocate_payment_core(session, payment_id, request_ref, allocations_in)


# ---------------------------------------------------------------------------
# AP Aging Report (ADR-015 Decision 5)
# ---------------------------------------------------------------------------


async def get_ap_aging(
    session: AsyncSession, *, bucket_date: date | None = None
) -> list[APAgingRow]:
    """Current-state AP aging report in a single compound UNION ALL snapshot."""
    require_permission("payables.report.read")
    company_id = require_current_company_id()
    effective_bucket_date = bucket_date or datetime.now(timezone.utc).date()  # noqa: UP017

    bill_rows = select(
        VendorBill.supplier_id.label("supplier_id"),
        literal("bill").label("kind"),
        (VendorBill.total - VendorBill.settled_amount).label("amount"),
        VendorBill.due_date.label("due_date"),
    ).where(
        VendorBill.company_id == company_id,
        VendorBill.status != VendorBillStatus.VOIDED,
        (VendorBill.total - VendorBill.settled_amount) > 0,
    )
    payment_rows = select(
        VendorPayment.supplier_id.label("supplier_id"),
        literal("credit").label("kind"),
        (VendorPayment.amount - VendorPayment.allocated_amount).label("amount"),
        sa_cast(null(), SA_Date).label("due_date"),
    ).where(
        VendorPayment.company_id == company_id,
        VendorPayment.status != VendorPaymentStatus.VOIDED,
        (VendorPayment.amount - VendorPayment.allocated_amount) > 0,
    )
    inner = union_all(bill_rows, payment_rows).subquery("ap_aging_union")
    days_past_due = sa_cast(
        sa_cast(literal(effective_bucket_date), SA_Date) - inner.c.due_date, Integer
    )

    def _bucket_sum(*conditions: Any) -> Any:
        zero = sa_cast(0, AMOUNT)
        return func.coalesce(func.sum(case((and_(*conditions), inner.c.amount), else_=zero)), zero)

    outer_stmt = select(
        inner.c.supplier_id,
        _bucket_sum(inner.c.kind == "bill", days_past_due <= 0).label("current"),
        _bucket_sum(inner.c.kind == "bill", days_past_due > 0, days_past_due <= 30).label("d1_30"),
        _bucket_sum(inner.c.kind == "bill", days_past_due > 30, days_past_due <= 60).label(
            "d31_60"
        ),
        _bucket_sum(inner.c.kind == "bill", days_past_due > 60, days_past_due <= 90).label(
            "d61_90"
        ),
        _bucket_sum(inner.c.kind == "bill", days_past_due > 90).label("d90_plus"),
        _bucket_sum(inner.c.kind == "credit").label("unapplied"),
    ).group_by(inner.c.supplier_id)

    fetched = (await session.execute(outer_stmt)).all()
    suppliers = await _fetch_suppliers(session, company_id, {r.supplier_id for r in fetched})

    rows: list[APAgingRow] = []
    for r in fetched:
        supplier = suppliers[r.supplier_id]
        net_total = r.current + r.d1_30 + r.d31_60 + r.d61_90 + r.d90_plus - r.unapplied
        rows.append(
            APAgingRow(
                supplier_id=r.supplier_id,
                supplier_code=supplier.code,
                supplier_name=supplier.name,
                current=r.current,
                days_1_30=r.d1_30,
                days_31_60=r.d31_60,
                days_61_90=r.d61_90,
                days_90_plus=r.d90_plus,
                unapplied_payments=r.unapplied,
                net_total=net_total,
            )
        )
    rows.sort(key=lambda r: r.supplier_code)
    return rows
