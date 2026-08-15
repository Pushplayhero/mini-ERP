"""receivables FastAPI routes — thin, same convention as every other module.

Every write endpoint wraps its service-layer flush-only core with a
commit + `IntegrityError` -> 409 translation, per this project's
established fix-#2 pattern (`sales.router`/`ledger.router` apply the same
shape to `confirm`/`ship`/`reverse` for the identical reason: the core's
own `flush()` can raise `IntegrityError` on its own — a duplicate
`external_ref`, the `uq_invoices_order_live` partial index, a reused
`request_ref` — and that must not bypass this translation and surface as
an unhandled 500).

**Re-select before serializing, every time** (same fix
`sales.router.create_sales_order`/`confirm_sales_order`/`ship_sales_order`
apply and document): `Invoice`/`Payment` both carry `TimestampAuditMixin
.updated_at` (`onupdate=func.now()`, server-side). Any UPDATE that touches
the row (void, an allocation's `settled_amount`/`allocated_amount` write)
expires that attribute rather than repopulating it in the already-loaded
Python object, and reading it afterward inside `model_validate` — outside
any `await` — raises `MissingGreenlet` instead of a clean response. A
plain re-select via `service.get_invoice`/`get_payment` after commit
avoids this for every write endpoint, including `issue_invoice` (a pure
INSERT today, but re-selecting uniformly means this can never regress if
that ever changes).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.receivables import service
from app.modules.receivables.schemas import (
    AllocationRequest,
    ARAgingRow,
    InvoiceCreate,
    InvoiceRead,
    PaymentCreate,
    PaymentRead,
)

router = APIRouter(prefix="/receivables", tags=["receivables"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _is_external_ref_conflict(exc: IntegrityError) -> bool:
    """True iff `exc` is the `uq_payments_company_external_ref` conflict.

    Same idiom as `ledger.posting._is_source_conflict` /
    `inventory.service`'s stock-move check: prefer the DBAPI exception's
    own `constraint_name` diagnostic (asyncpg populates it), fall back to a
    substring match on the rendered error for other drivers/wrapping.
    """
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name:
        return str(constraint_name) == "uq_payments_company_external_ref"
    return "uq_payments_company_external_ref" in str(exc)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.post("/invoices", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
async def issue_invoice(payload: InvoiceCreate, session: DbSession) -> InvoiceRead:
    try:
        invoice = await service.create_invoice(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "Invoice violates a uniqueness or reference constraint "
            "(e.g. this order already has a live invoice)"
        ) from exc
    invoice = await service.get_invoice(session, invoice.id)
    return InvoiceRead.model_validate(invoice)


@router.get("/invoices", response_model=list[InvoiceRead])
async def list_invoices(session: DbSession) -> list[InvoiceRead]:
    invoices = await service.list_invoices(session)
    return [InvoiceRead.model_validate(i) for i in invoices]


@router.get("/invoices/{invoice_id}", response_model=InvoiceRead)
async def get_invoice(invoice_id: uuid.UUID, session: DbSession) -> InvoiceRead:
    invoice = await service.get_invoice(session, invoice_id)
    return InvoiceRead.model_validate(invoice)


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceRead)
async def void_invoice(invoice_id: uuid.UUID, session: DbSession) -> InvoiceRead:
    try:
        await service.void_invoice(session, invoice_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Invoice {invoice_id} violates a uniqueness or reference constraint"
        ) from exc
    invoice = await service.get_invoice(session, invoice_id)
    return InvoiceRead.model_validate(invoice)


# ---------------------------------------------------------------------------
# Payments + allocations
# ---------------------------------------------------------------------------


@router.post("/payments", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
async def create_payment(payload: PaymentCreate, session: DbSession) -> PaymentRead:
    try:
        payment = await service.create_payment(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # ADR-008 R2: "the response body of the 409 identifies the existing
        # payment" — a client retrying an uncertain POST must be able to
        # reconcile against the payment that actually landed, not just see
        # a generic conflict. Only the external_ref constraint gets this
        # treatment; any other IntegrityError (e.g. a race on an inline
        # allocation) falls through to the generic message below.
        if _is_external_ref_conflict(exc):
            existing = await service.get_payment_by_external_ref(session, payload.external_ref)
            if existing is not None:
                raise ConflictError(
                    f"external_ref {payload.external_ref!r} is already used by payment "
                    f"{existing.id} ({existing.payment_no})"
                ) from exc
        raise ConflictError(
            "Payment violates a uniqueness or reference constraint "
            "(e.g. external_ref already used)"
        ) from exc
    payment = await service.get_payment(session, payment.id)
    return PaymentRead.model_validate(payment)


@router.get("/payments", response_model=list[PaymentRead])
async def list_payments(session: DbSession) -> list[PaymentRead]:
    payments = await service.list_payments(session)
    return [PaymentRead.model_validate(p) for p in payments]


@router.get("/payments/{payment_id}", response_model=PaymentRead)
async def get_payment(payment_id: uuid.UUID, session: DbSession) -> PaymentRead:
    payment = await service.get_payment(session, payment_id)
    return PaymentRead.model_validate(payment)


@router.post("/payments/{payment_id}/void", response_model=PaymentRead)
async def void_payment(payment_id: uuid.UUID, session: DbSession) -> PaymentRead:
    try:
        await service.void_payment(session, payment_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Payment {payment_id} violates a uniqueness or reference constraint"
        ) from exc
    payment = await service.get_payment(session, payment_id)
    return PaymentRead.model_validate(payment)


@router.post("/payments/{payment_id}/allocations", response_model=PaymentRead)
async def allocate_payment(
    payment_id: uuid.UUID, payload: AllocationRequest, session: DbSession
) -> PaymentRead:
    try:
        await service.allocate_payment(
            session, payment_id, payload.request_ref, payload.allocations
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Allocation for payment {payment_id} violates a uniqueness or reference constraint"
        ) from exc
    payment = await service.get_payment(session, payment_id)
    return PaymentRead.model_validate(payment)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@router.get("/reports/ar-aging", response_model=list[ARAgingRow])
async def get_ar_aging(session: DbSession, bucket_date: date | None = None) -> list[ARAgingRow]:
    return await service.get_ar_aging(session, bucket_date=bucket_date)
