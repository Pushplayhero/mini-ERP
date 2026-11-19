"""payables FastAPI routes (ADR-015) — commit + IntegrityError -> 409 boundary."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.modules.payables import service
from app.modules.payables.schemas import (
    APAgingRow,
    VendorAllocationRequest,
    VendorBillCreate,
    VendorBillRead,
    VendorPaymentCreate,
    VendorPaymentRead,
)

router = APIRouter(prefix="/payables", tags=["payables"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _matches_constraint(exc: IntegrityError, constraint_name: str) -> bool:
    orig = getattr(exc, "orig", None)
    actual = getattr(orig, "constraint_name", None)
    if actual:
        return str(actual) == constraint_name
    return constraint_name in str(exc)


# ---------------------------------------------------------------------------
# Vendor Bills
# ---------------------------------------------------------------------------


@router.post(
    "/bills",
    response_model=VendorBillRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("payables.bill.create"))],
)
async def issue_bill(payload: VendorBillCreate, session: DbSession) -> VendorBillRead:
    try:
        bill = await service.create_bill(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _matches_constraint(exc, "uq_vendor_bills_order_live"):
            raise ConflictError(
                f"PurchaseOrder {payload.order_id} already has a live (non-voided) vendor bill"
            ) from exc
        if _matches_constraint(exc, "uq_vendor_bills_supplier_invoice_no_live"):
            raise ConflictError(
                f"supplier_invoice_no {payload.supplier_invoice_no!r} is already used by another "
                "live vendor bill for this supplier"
            ) from exc
        raise ConflictError(
            "VendorBill violates a uniqueness or reference constraint "
            "(e.g. purchase order or supplier_invoice_no already billed)"
        ) from exc
    bill = await service.load_bill_unchecked(session, bill.id)
    return VendorBillRead.model_validate(bill)


@router.get(
    "/bills",
    response_model=list[VendorBillRead],
    dependencies=[Depends(RequiresPermission("payables.bill.read"))],
)
async def list_bills(session: DbSession) -> list[VendorBillRead]:
    bills = await service.list_bills(session)
    return [VendorBillRead.model_validate(b) for b in bills]


@router.get(
    "/bills/{bill_id}",
    response_model=VendorBillRead,
    dependencies=[Depends(RequiresPermission("payables.bill.read"))],
)
async def get_bill(bill_id: uuid.UUID, session: DbSession) -> VendorBillRead:
    bill = await service.get_bill(session, bill_id)
    return VendorBillRead.model_validate(bill)


@router.post(
    "/bills/{bill_id}/void",
    response_model=VendorBillRead,
    dependencies=[Depends(RequiresPermission("payables.bill.void"))],
)
async def void_bill(bill_id: uuid.UUID, session: DbSession) -> VendorBillRead:
    try:
        await service.void_bill(session, bill_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"VendorBill {bill_id} violates a uniqueness or reference constraint"
        ) from exc
    bill = await service.load_bill_unchecked(session, bill_id)
    return VendorBillRead.model_validate(bill)


# ---------------------------------------------------------------------------
# Vendor Payments + Allocations
# ---------------------------------------------------------------------------


@router.post(
    "/payments",
    response_model=VendorPaymentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("payables.payment.create"))],
)
async def create_payment(payload: VendorPaymentCreate, session: DbSession) -> VendorPaymentRead:
    try:
        payment = await service.create_payment(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _matches_constraint(exc, "uq_vendor_payments_company_external_ref"):
            existing = await service.load_payment_by_external_ref_unchecked(
                session, payload.external_ref
            )
            if existing is not None:
                raise ConflictError(
                    f"external_ref {payload.external_ref!r} is already used by vendor payment "
                    f"{existing.id} ({existing.payment_no})"
                ) from exc
        raise ConflictError(
            "VendorPayment violates a uniqueness or reference constraint "
            "(e.g. external_ref already used)"
        ) from exc
    payment = await service.load_payment_unchecked(session, payment.id)
    return VendorPaymentRead.model_validate(payment)


@router.get(
    "/payments",
    response_model=list[VendorPaymentRead],
    dependencies=[Depends(RequiresPermission("payables.payment.read"))],
)
async def list_payments(session: DbSession) -> list[VendorPaymentRead]:
    payments = await service.list_payments(session)
    return [VendorPaymentRead.model_validate(p) for p in payments]


@router.get(
    "/payments/{payment_id}",
    response_model=VendorPaymentRead,
    dependencies=[Depends(RequiresPermission("payables.payment.read"))],
)
async def get_payment(payment_id: uuid.UUID, session: DbSession) -> VendorPaymentRead:
    payment = await service.get_payment(session, payment_id)
    return VendorPaymentRead.model_validate(payment)


@router.post(
    "/payments/{payment_id}/void",
    response_model=VendorPaymentRead,
    dependencies=[Depends(RequiresPermission("payables.payment.void"))],
)
async def void_payment(payment_id: uuid.UUID, session: DbSession) -> VendorPaymentRead:
    try:
        await service.void_payment(session, payment_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"VendorPayment {payment_id} violates a uniqueness or reference constraint"
        ) from exc
    payment = await service.load_payment_unchecked(session, payment_id)
    return VendorPaymentRead.model_validate(payment)


@router.post(
    "/payments/{payment_id}/allocations",
    response_model=VendorPaymentRead,
    dependencies=[Depends(RequiresPermission("payables.payment.allocate"))],
)
async def allocate_payment(
    payment_id: uuid.UUID, payload: VendorAllocationRequest, session: DbSession
) -> VendorPaymentRead:
    try:
        await service.allocate_payment(
            session, payment_id, payload.request_ref, payload.allocations
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Allocation for vendor payment {payment_id} violates a uniqueness or reference "
            "constraint"
        ) from exc
    payment = await service.load_payment_unchecked(session, payment_id)
    return VendorPaymentRead.model_validate(payment)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@router.get(
    "/reports/ap-aging",
    response_model=list[APAgingRow],
    dependencies=[Depends(RequiresPermission("payables.report.read"))],
)
async def get_ap_aging(session: DbSession, bucket_date: date | None = None) -> list[APAgingRow]:
    return await service.get_ap_aging(session, bucket_date=bucket_date)
