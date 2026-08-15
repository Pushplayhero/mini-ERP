"""receivables service layer — business logic + transaction boundary (ADR-008).

Routers stay thin, same convention as every other module. Every write to a
tenant-scoped table stamps `company_id` from
`app.core.tenancy.require_current_company_id()`, never from the DTO.

Independence-boundary note (import-linter): this module must never import
`app.modules.sales`, `app.modules.masterdata`, or `app.modules.ledger`.
`sales_orders`/`customers` are resolved via lightweight
`sqlalchemy.table()` Core references — same pattern `sales.service`/
`ledger.service`/`ledger.posting` already use for `accounts`/`products`;
see those modules' docstrings for the full rationale. A DB foreign key
alone proves a referenced row exists, not that it belongs to the
requesting company, which is why every Core-table lookup below carries an
explicit `company_id` predicate.

**Transaction ownership (ADR-003 R1, applied here)**: every write function
in this module (`create_invoice`, `void_invoice`, `create_payment`,
`void_payment`, `allocate_payment`) is flush-only — none of them commit or
roll back. `receivables.router` owns the commit boundary and the
`IntegrityError` -> 409 translation for it, wrapping the *core call itself*
(not just the later `commit()`), matching the fix-#2 pattern this project
applies everywhere a flush-only core can raise `IntegrityError` on its own
(a duplicate `external_ref`, the `uq_invoices_order_live` partial index, a
reused `request_ref`, etc.).

**Money in this module never posts by accident**: `create_invoice`/
`void_invoice`/`create_payment`/`void_payment` each publish exactly one
event (`app.core.events.publish`), which is the only thing that ever turns
into a journal entry (`ledger.posting`'s registered handler for each
`event_type`). `allocate_payment` publishes nothing — allocation is
subledger bookkeeping (ADR-008 Decision 2), it moves no value between
accounts.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import cast as sa_cast
from sqlalchemy import column as sa_column
from sqlalchemy import literal, null, select, union_all
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date as SA_Date

from app.core import events
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.tenancy import require_current_company_id
from app.modules.receivables.events import (
    RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE,
    RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE,
    RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE,
    RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE,
)
from app.modules.receivables.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentAllocationCommand,
    PaymentStatus,
    ReceivablesSequence,
)
from app.modules.receivables.schemas import (
    ARAgingRow,
    InvoiceCreate,
    PaymentAllocationIn,
    PaymentCreate,
)

# ADR-008 R12: Phase 1 is TWD-only in practice, same hard constraint
# `ledger.service.FUNCTIONAL_CURRENCY` enforces — duplicated here (not
# imported: this module must never import `app.modules.ledger`) as the
# defense-in-depth half of R12; `masterdata.schemas.CustomerCreate/Update`
# is the primary enforcement point.
_PHASE_1_CURRENCY = "TWD"

# NUMERIC(20,6) formatting for freshly-constructed (not-yet-DB-round-tripped)
# money fields: a bare `Decimal("0")` serializes as `"0"`, not `"0.000000"`
# — cosmetically inconsistent with every other money field in this API,
# which always shows 6 decimal places once it has passed through a
# NUMERIC(20,6) column (or this module's own round-half-even schema
# validators). Used everywhere a zero `Decimal` is constructed directly in
# Python rather than read back from the DB.
_ZERO = Decimal("0.000000")

_SALES_ORDERS = sa_table(
    "sales_orders",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("order_no"),
    sa_column("customer_id"),
    sa_column("status"),
    sa_column("currency_code"),
    sa_column("total"),
    sa_column("shipped_at"),
    sa_column("snapshot_customer_code"),
    sa_column("snapshot_customer_name"),
)

_CUSTOMERS = sa_table(
    "customers",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("code"),
    sa_column("name"),
    sa_column("currency_code"),
    sa_column("payment_terms_days"),
)


# ---------------------------------------------------------------------------
# Order / customer resolution (Core-level references — see module docstring)
# ---------------------------------------------------------------------------


async def _fetch_order(session: AsyncSession, company_id: uuid.UUID, order_id: uuid.UUID) -> Any:
    result = await session.execute(
        select(
            _SALES_ORDERS.c.id,
            _SALES_ORDERS.c.order_no,
            _SALES_ORDERS.c.customer_id,
            _SALES_ORDERS.c.status,
            _SALES_ORDERS.c.currency_code,
            _SALES_ORDERS.c.total,
            _SALES_ORDERS.c.shipped_at,
            _SALES_ORDERS.c.snapshot_customer_code,
            _SALES_ORDERS.c.snapshot_customer_name,
        ).where(_SALES_ORDERS.c.id == order_id, _SALES_ORDERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"order_id {order_id} not found or does not belong to this company"
        )
    return row


async def _fetch_customer(
    session: AsyncSession, company_id: uuid.UUID, customer_id: uuid.UUID
) -> Any:
    result = await session.execute(
        select(
            _CUSTOMERS.c.id,
            _CUSTOMERS.c.code,
            _CUSTOMERS.c.name,
            _CUSTOMERS.c.currency_code,
            _CUSTOMERS.c.payment_terms_days,
        ).where(_CUSTOMERS.c.id == customer_id, _CUSTOMERS.c.company_id == company_id)
    )
    row = result.first()
    if row is None:
        raise DomainValidationError(
            f"customer_id {customer_id} not found or does not belong to this company"
        )
    return row


async def _fetch_customers(
    session: AsyncSession, company_id: uuid.UUID, customer_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not customer_ids:
        return {}
    result = await session.execute(
        select(_CUSTOMERS.c.id, _CUSTOMERS.c.code, _CUSTOMERS.c.name).where(
            _CUSTOMERS.c.id.in_(customer_ids), _CUSTOMERS.c.company_id == company_id
        )
    )
    return {row.id: row for row in result.all()}


# ---------------------------------------------------------------------------
# Document numbering (ADR-008 Decision 1 — not required to be gapless)
# ---------------------------------------------------------------------------


async def _allocate_doc_no(
    session: AsyncSession, company_id: uuid.UUID, year: int, doc_type: str
) -> str:
    """`(company_id, year, doc_type) -> next_no` allocation. Same

    get-or-create + `FOR UPDATE` pattern as `sales.service._allocate_order_no`
    / `ledger.service._allocate_entry_no` — see either's docstring for the
    full concurrency argument. Gaplessness is explicitly NOT required
    (ADR-008 Decision 1), same doctrine as `order_no`.
    """
    await session.execute(
        pg_insert(ReceivablesSequence)
        .values(company_id=company_id, year=year, doc_type=doc_type, next_no=1)
        .on_conflict_do_nothing(index_elements=["company_id", "year", "doc_type"])
    )
    result = await session.execute(
        select(ReceivablesSequence)
        .where(
            ReceivablesSequence.company_id == company_id,
            ReceivablesSequence.year == year,
            ReceivablesSequence.doc_type == doc_type,
        )
        .with_for_update()
    )
    seq = result.scalar_one()
    allocated_no = seq.next_no
    seq.next_no = allocated_no + 1
    prefix = "INV" if doc_type == "invoice" else "PAY"
    return f"{prefix}-{year:04d}-{allocated_no:06d}"


# ---------------------------------------------------------------------------
# Invoices (ADR-008 Decision 1, R13)
# ---------------------------------------------------------------------------


async def create_invoice(session: AsyncSession, data: InvoiceCreate) -> Invoice:
    """Non-committing core of invoice issue. Flush-only — see module

    docstring's transaction-ownership note.

    Flow: order must be SHIPPED (422) -> TWD-only defense in depth (422,
    R12) -> `invoice_date` cannot predate `order.shipped_at` (422, R13) ->
    `due_date` defaults from `customer.payment_terms_days` and cannot
    precede `invoice_date` (422) -> allocate `invoice_no` -> insert (the
    `uq_invoices_order_live` partial unique index is the double-invoice
    race's whole story — no order-row lock needed, see ADR-008 Decision 1)
    -> publish `receivables.invoice_issued` -> flush.
    """
    company_id = require_current_company_id()
    order = await _fetch_order(session, company_id, data.order_id)

    if order.status != "SHIPPED":
        raise DomainValidationError(
            f"SalesOrder {data.order_id} is not shipped (status={order.status}); cannot invoice "
            "(ADR-008 Decision 1)"
        )

    # R12 defense in depth: the primary enforcement point is
    # `masterdata.schemas.CustomerCreate/Update`'s validator; this is a
    # second, independent check against whatever the order/customer
    # actually carry, so a pre-R12 customer (or any bypass writer) still
    # cannot reach receivables' posting path with a non-TWD amount.
    if order.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"SalesOrder {data.order_id} is denominated in {order.currency_code!r}, not "
            f"{_PHASE_1_CURRENCY!r} — receivables is TWD-only in Phase 1 (ADR-008 R12)"
        )
    customer = await _fetch_customer(session, company_id, order.customer_id)
    if customer.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"Customer {order.customer_id} is denominated in {customer.currency_code!r}, not "
            f"{_PHASE_1_CURRENCY!r} — receivables is TWD-only in Phase 1 (ADR-008 R12)"
        )

    order_shipped_at: datetime = order.shipped_at
    invoice_date = data.invoice_date or date.today()
    if invoice_date < order_shipped_at.date():
        raise DomainValidationError(
            f"invoice_date {invoice_date} cannot precede the order's shipment date "
            f"{order_shipped_at.date()} (ADR-008 R13 — revenue is recognized at/after delivery)"
        )

    due_date = data.due_date or (invoice_date + timedelta(days=customer.payment_terms_days))
    if due_date < invoice_date:
        raise DomainValidationError(
            f"due_date {due_date} cannot precede invoice_date {invoice_date}"
        )

    invoice_no = await _allocate_doc_no(session, company_id, invoice_date.year, "invoice")

    invoice = Invoice(
        company_id=company_id,
        invoice_no=invoice_no,
        order_id=order.id,
        customer_id=order.customer_id,
        status=InvoiceStatus.OPEN,
        currency_code=order.currency_code,
        order_shipped_at=order_shipped_at,
        invoice_date=invoice_date,
        due_date=due_date,
        total=order.total,
        settled_amount=_ZERO,
        snapshot_customer_code=order.snapshot_customer_code,
        snapshot_customer_name=order.snapshot_customer_name,
    )
    session.add(invoice)
    # Flush now (not just at the end): a concurrent double-issue hits
    # `uq_invoices_order_live` right here, before anything else in this
    # function runs — the whole point of the partial unique index doing
    # the race-detection work instead of an order-row lock.
    await session.flush()

    await events.publish(
        session,
        RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": invoice.id,
            "event_date": invoice_date,
            "invoice_no": invoice_no,
            "order_id": order.id,
            "customer_id": order.customer_id,
            "total": order.total,
        },
    )

    await session.flush()
    return invoice


async def get_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    result = await session.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise NotFoundError("Invoice", invoice_id)
    return invoice


async def list_invoices(session: AsyncSession) -> Sequence[Invoice]:
    result = await session.execute(select(Invoice).order_by(Invoice.invoice_no))
    return result.scalars().all()


async def void_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    """Non-committing core (ADR-008 Decision 4). `SELECT ... FOR UPDATE`

    (R1 doctrine, applied for real here unlike `create_invoice`) then
    re-check `status`/`settled_amount` under the lock — a concurrent void
    and a concurrent allocation on the same invoice serialize on this row
    lock.
    """
    result = await session.execute(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise NotFoundError("Invoice", invoice_id)
    if invoice.status == InvoiceStatus.VOIDED:
        raise ConflictError(f"Invoice {invoice_id} is already voided")
    if invoice.settled_amount != 0:
        raise ConflictError(
            f"Invoice {invoice_id} has settled_amount={invoice.settled_amount} != 0; "
            "cannot void a partially or fully settled invoice (ADR-008 Decision 4 — "
            "un-allocation is deferred to Phase 2)"
        )

    company_id = require_current_company_id()
    invoice.status = InvoiceStatus.VOIDED
    invoice.voided_at = datetime.now(timezone.utc)  # noqa: UP017 — see sales.service's comment

    await events.publish(
        session,
        RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": invoice.id,
            "event_date": invoice.voided_at.date(),
            "invoice_no": invoice.invoice_no,
            "total": invoice.total,
        },
    )

    await session.flush()
    return invoice


# ---------------------------------------------------------------------------
# Payments (ADR-008 Decision 2, R2)
# ---------------------------------------------------------------------------


async def create_payment(session: AsyncSession, data: PaymentCreate) -> Payment:
    """Non-committing core of payment receipt + optional inline allocation.

    `external_ref`'s `UNIQUE(company_id, external_ref)` constraint is the
    whole retry-safety story (R2) — a retried `POST /payments` hits it at
    `flush()` and 409s instead of double-posting Cash/AR; no pre-check
    needed here.
    """
    company_id = require_current_company_id()
    customer = await _fetch_customer(session, company_id, data.customer_id)
    if customer.currency_code != _PHASE_1_CURRENCY:
        raise DomainValidationError(
            f"Customer {data.customer_id} is denominated in {customer.currency_code!r}, not "
            f"{_PHASE_1_CURRENCY!r} — receivables is TWD-only in Phase 1 (ADR-008 R12)"
        )

    received_at = data.received_at or datetime.now(timezone.utc)  # noqa: UP017
    payment_no = await _allocate_doc_no(session, company_id, received_at.year, "payment")

    payment = Payment(
        company_id=company_id,
        payment_no=payment_no,
        customer_id=data.customer_id,
        status=PaymentStatus.RECEIVED,
        external_ref=data.external_ref,
        currency_code=_PHASE_1_CURRENCY,
        amount=data.amount,
        allocated_amount=_ZERO,
        received_at=received_at,
    )
    session.add(payment)
    await session.flush()

    await events.publish(
        session,
        RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE,
        {
            "company_id": company_id,
            "source_id": payment.id,
            "event_date": received_at.date(),
            "payment_no": payment_no,
            "customer_id": data.customer_id,
            "amount": data.amount,
        },
    )
    await session.flush()

    if data.allocations:
        # Inline batch inherits the payment's own `external_ref` as its
        # allocation-command `request_ref` (documented in
        # `PaymentCreate.allocations`'s docstring) — a retried
        # `POST /payments` (same external_ref, same body) therefore never
        # reaches this far anyway (it 409s at the payment insert above),
        # so this call only ever runs once per genuinely new payment.
        await allocate_payment(session, payment.id, data.external_ref, data.allocations)

    await session.flush()
    return payment


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("Payment", payment_id)
    return payment


async def get_payment_by_external_ref(session: AsyncSession, external_ref: str) -> Payment | None:
    """Tenant-scoped lookup used by `receivables.router` to identify the

    existing payment behind a `uq_payments_company_external_ref` conflict
    (ADR-008 R2: "the response body of the 409 identifies the existing
    payment"), so a client retrying an uncertain `POST /payments` can
    reconcile instead of just seeing a generic 409. `Payment` is
    `TenantScopedMixin` — auto-filtered to the active company, no explicit
    predicate needed here (unlike this module's `sqlalchemy.table()` Core
    references to other modules' tables).
    """
    result = await session.execute(select(Payment).where(Payment.external_ref == external_ref))
    return result.scalar_one_or_none()


async def list_payments(session: AsyncSession) -> Sequence[Payment]:
    result = await session.execute(select(Payment).order_by(Payment.payment_no))
    return result.scalars().all()


async def void_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    """Non-committing core (ADR-008 Decision 4) — mirrors `void_invoice`

    exactly, with the accounts swapped at the posting layer (see
    `ledger.posting.POSTING_RULES`).
    """
    result = await session.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("Payment", payment_id)
    if payment.status == PaymentStatus.VOIDED:
        raise ConflictError(f"Payment {payment_id} is already voided")
    if payment.allocated_amount != 0:
        raise ConflictError(
            f"Payment {payment_id} has allocated_amount={payment.allocated_amount} != 0; "
            "cannot void a payment with active allocations (ADR-008 Decision 4 — "
            "un-allocation is deferred to Phase 2)"
        )

    company_id = require_current_company_id()
    payment.status = PaymentStatus.VOIDED
    payment.voided_at = datetime.now(timezone.utc)  # noqa: UP017

    await events.publish(
        session,
        RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE,
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
# Allocation (沖帳) — ADR-008 Decision 3, R7, R14
# ---------------------------------------------------------------------------


def _fingerprint_allocations(allocations: Sequence[PaymentAllocationIn]) -> str:
    """Deterministic hash of a normalized `{invoice_id, amount}` set (R14).

    Order-independent (sorted before hashing) so the same logical command
    submitted with its lines in a different order still fingerprints
    identically — an exact retry must be recognized as such regardless of
    line ordering.
    """
    normalized = sorted((str(a.invoice_id), str(a.amount)) for a in allocations)
    raw = "|".join(f"{invoice_id}:{amount}" for invoice_id, amount in normalized)
    return hashlib.sha256(raw.encode()).hexdigest()


async def allocate_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    request_ref: str,
    allocations_in: Sequence[PaymentAllocationIn],
) -> Payment:
    """Non-committing core of one allocation command (ADR-008 Decision 3, R7, R14).

    Flow: lock the payment row first (R1 doctrine) -> check it is not
    voided -> compute this command's fingerprint and look up
    `PaymentAllocationCommand` by `(payment_id, request_ref)`: a match with
    the same fingerprint is an exact retry, replayed idempotently (return
    the payment unchanged, no-op); a match with a different fingerprint is
    the reused-reference contract violation (409) -> lock target invoices,
    sorted by id (deterministic, deadlock-avoiding order — payment row
    first, then invoices by id) -> validate every target (same company via
    the automatic tenant filter, same customer, not voided) -> re-check
    capacity on both sides under the locks -> insert the command row, then
    one `PaymentAllocation` row per line, updating both maintained columns
    and deriving each invoice's `status` -> flush.
    """
    company_id = require_current_company_id()
    if not allocations_in:
        raise DomainValidationError("allocations must be non-empty")

    invoice_ids = [a.invoice_id for a in allocations_in]
    if len(set(invoice_ids)) != len(invoice_ids):
        raise DomainValidationError(
            "duplicate invoice_id targets within one allocation request are not allowed "
            "(ADR-008 R7)"
        )

    result = await session.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise NotFoundError("Payment", payment_id)
    if payment.status == PaymentStatus.VOIDED:
        raise ConflictError(f"Payment {payment_id} is voided; cannot allocate")

    fingerprint = _fingerprint_allocations(allocations_in)
    existing_cmd_result = await session.execute(
        select(PaymentAllocationCommand).where(
            PaymentAllocationCommand.payment_id == payment_id,
            PaymentAllocationCommand.request_ref == request_ref,
        )
    )
    existing_cmd = existing_cmd_result.scalar_one_or_none()
    if existing_cmd is not None:
        if existing_cmd.request_fingerprint == fingerprint:
            # Exact retry (R14) — idempotent replay, no-op.
            return payment
        raise ConflictError(
            f"request_ref {request_ref!r} was already used for payment {payment_id} with a "
            "different allocation set — reusing a reference for a different command body is "
            "not allowed (ADR-008 R14)"
        )

    sorted_ids = sorted(set(invoice_ids), key=str)
    invoices_result = await session.execute(
        select(Invoice).where(Invoice.id.in_(sorted_ids)).order_by(Invoice.id).with_for_update()
    )
    invoices_by_id = {inv.id: inv for inv in invoices_result.scalars().all()}
    missing = set(sorted_ids) - invoices_by_id.keys()
    if missing:
        missing_str = sorted(str(i) for i in missing)
        raise DomainValidationError(
            f"invoice_id(s) not found or do not belong to this company: {missing_str}"
        )

    for inv in invoices_by_id.values():
        if inv.status == InvoiceStatus.VOIDED:
            raise DomainValidationError(f"invoice {inv.id} is voided; cannot allocate to it")
        if inv.customer_id != payment.customer_id:
            raise DomainValidationError(
                f"invoice {inv.id} belongs to a different customer than payment {payment_id}"
            )
        if inv.currency_code != payment.currency_code:
            raise DomainValidationError(
                f"invoice {inv.id}'s currency ({inv.currency_code}) does not match payment "
                f"{payment_id}'s currency ({payment.currency_code})"
            )

    total_requested = sum((a.amount for a in allocations_in), Decimal("0"))
    if payment.allocated_amount + total_requested > payment.amount:
        raise ConflictError(
            f"allocation of {total_requested} would exceed payment {payment_id}'s remaining "
            f"capacity ({payment.amount - payment.allocated_amount})"
        )
    for a in allocations_in:
        inv = invoices_by_id[a.invoice_id]
        if inv.settled_amount + a.amount > inv.total:
            raise ConflictError(
                f"allocation of {a.amount} would exceed invoice {inv.id}'s remaining balance "
                f"({inv.total - inv.settled_amount})"
            )

    command = PaymentAllocationCommand(
        company_id=company_id,
        payment_id=payment_id,
        request_ref=request_ref,
        request_fingerprint=fingerprint,
    )
    session.add(command)
    await session.flush()

    for a in allocations_in:
        inv = invoices_by_id[a.invoice_id]
        session.add(
            PaymentAllocation(
                company_id=company_id,
                payment_id=payment_id,
                invoice_id=a.invoice_id,
                command_id=command.id,
                amount=a.amount,
            )
        )
        inv.settled_amount += a.amount
        if inv.settled_amount >= inv.total:
            inv.status = InvoiceStatus.PAID
        elif inv.settled_amount > 0:
            inv.status = InvoiceStatus.PARTIAL

    payment.allocated_amount += total_requested

    await session.flush()
    return payment


# ---------------------------------------------------------------------------
# AR aging (ADR-008 Decision 5, R4, R15)
# ---------------------------------------------------------------------------


async def get_ar_aging(
    session: AsyncSession, *, bucket_date: date | None = None
) -> list[ARAgingRow]:
    """Current-state AR aging report (ADR-008 Decision 5, R4, R15).

    **This is NOT a historical as-of report** — it reads today's
    `settled_amount`/`allocated_amount` and today's set of open invoices;
    `bucket_date` (default today) only moves the days-past-due boundary
    that classifies buckets, never a historical cutoff. Population is the
    UNION (R15) of customers with a qualifying open invoice and customers
    with nonzero unapplied credit — not an invoice-anchored query, which
    would silently drop payment-only customers and break the tie-out
    property for exactly the customers who most need to appear in it.
    """
    company_id = require_current_company_id()
    effective_bucket_date = bucket_date or date.today()

    # Codex diff review (2026-08-15, finding 1): the invoice-balance side and
    # unapplied-credit side of this report MUST reflect one consistent
    # instant for Decision 5's tie-out property to hold — two separate
    # `session.execute()` calls (the original implementation) each get their
    # own READ COMMITTED statement snapshot, so an `allocate_payment` that
    # commits between them (e.g. moving 60 from unapplied credit into
    # settled_amount) is only half-visible to this report, producing a
    # stale total that does not tie to the ledger. A single compound SELECT
    # (UNION ALL) is one statement to PostgreSQL and therefore one snapshot
    # for both sides.
    invoice_rows = select(
        Invoice.customer_id.label("customer_id"),
        literal("invoice").label("kind"),
        (Invoice.total - Invoice.settled_amount).label("amount"),
        Invoice.due_date.label("due_date"),
    ).where(
        Invoice.company_id == company_id,
        Invoice.status != InvoiceStatus.VOIDED,
        (Invoice.total - Invoice.settled_amount) > 0,
    )
    payment_rows = select(
        Payment.customer_id.label("customer_id"),
        literal("credit").label("kind"),
        (Payment.amount - Payment.allocated_amount).label("amount"),
        sa_cast(null(), SA_Date).label("due_date"),
    ).where(
        Payment.company_id == company_id,
        Payment.status != PaymentStatus.VOIDED,
        (Payment.amount - Payment.allocated_amount) > 0,
    )
    fetched = (await session.execute(union_all(invoice_rows, payment_rows))).all()

    customer_ids = {row.customer_id for row in fetched}
    customers = await _fetch_customers(session, company_id, customer_ids)

    buckets: dict[uuid.UUID, dict[str, Decimal]] = {}

    def _row(customer_id: uuid.UUID) -> dict[str, Decimal]:
        return buckets.setdefault(
            customer_id,
            {
                "current": _ZERO,
                "d1_30": _ZERO,
                "d31_60": _ZERO,
                "d61_90": _ZERO,
                "d90_plus": _ZERO,
                "unapplied": _ZERO,
            },
        )

    for r in fetched:
        if r.kind == "invoice":
            balance = r.amount
            days_past_due = (effective_bucket_date - r.due_date).days
            row = _row(r.customer_id)
            if days_past_due <= 0:
                row["current"] += balance
            elif days_past_due <= 30:
                row["d1_30"] += balance
            elif days_past_due <= 60:
                row["d31_60"] += balance
            elif days_past_due <= 90:
                row["d61_90"] += balance
            else:
                row["d90_plus"] += balance
        else:
            _row(r.customer_id)["unapplied"] += r.amount

    rows: list[ARAgingRow] = []
    for customer_id, b in buckets.items():
        customer = customers[customer_id]
        net_total = (
            b["current"] + b["d1_30"] + b["d31_60"] + b["d61_90"] + b["d90_plus"] - b["unapplied"]
        )
        rows.append(
            ARAgingRow(
                customer_id=customer_id,
                customer_code=customer.code,
                customer_name=customer.name,
                current=b["current"],
                days_1_30=b["d1_30"],
                days_31_60=b["d31_60"],
                days_61_90=b["d61_90"],
                days_90_plus=b["d90_plus"],
                unapplied_credits=b["unapplied"],
                net_total=net_total,
            )
        )
    rows.sort(key=lambda r: r.customer_code)
    return rows
