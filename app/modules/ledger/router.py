"""ledger FastAPI routes — thin, same convention as masterdata.router.

No UPDATE/DELETE endpoints for journal entries or lines exist, and none ever
will (ADR-005 Decision 3: immutability is policy at this layer, physics at
the DB layer via triggers — see the migration). Corrections go through
`POST /journal-entries/{id}/reverse` (ADR-005 R3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_session
from app.modules.ledger import service
from app.modules.ledger.schemas import (
    AccountingPeriodCreate,
    AccountingPeriodRead,
    JournalEntryCreate,
    JournalEntryRead,
    TrialBalanceLine,
)

router = APIRouter(tags=["ledger"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# ---------------------------------------------------------------------------
# Accounting periods
# ---------------------------------------------------------------------------


@router.post("/periods", response_model=AccountingPeriodRead, status_code=status.HTTP_201_CREATED)
async def create_period(
    payload: AccountingPeriodCreate, session: DbSession
) -> AccountingPeriodRead:
    period = await service.create_period(session, payload)
    return AccountingPeriodRead.model_validate(period)


@router.get("/periods", response_model=list[AccountingPeriodRead])
async def list_periods(session: DbSession) -> list[AccountingPeriodRead]:
    periods = await service.list_periods(session)
    return [AccountingPeriodRead.model_validate(p) for p in periods]


@router.post("/periods/{period_id}/close", response_model=AccountingPeriodRead)
async def close_period(period_id: uuid.UUID, session: DbSession) -> AccountingPeriodRead:
    period = await service.close_period(session, period_id)
    return AccountingPeriodRead.model_validate(period)


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------


@router.post(
    "/journal-entries", response_model=JournalEntryRead, status_code=status.HTTP_201_CREATED
)
async def create_journal_entry(payload: JournalEntryCreate, session: DbSession) -> JournalEntryRead:
    entry = await service.create_journal_entry(session, payload)
    return JournalEntryRead.model_validate(entry)


@router.get("/journal-entries", response_model=list[JournalEntryRead])
async def list_journal_entries(session: DbSession) -> list[JournalEntryRead]:
    entries = await service.list_journal_entries(session)
    return [JournalEntryRead.model_validate(e) for e in entries]


@router.get("/journal-entries/{entry_id}", response_model=JournalEntryRead)
async def get_journal_entry(entry_id: uuid.UUID, session: DbSession) -> JournalEntryRead:
    entry = await service.get_journal_entry(session, entry_id)
    return JournalEntryRead.model_validate(entry)


@router.post(
    "/journal-entries/{entry_id}/reverse",
    response_model=JournalEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def reverse_journal_entry(entry_id: uuid.UUID, session: DbSession) -> JournalEntryRead:
    reversal = await service.reverse_journal_entry(session, entry_id)
    return JournalEntryRead.model_validate(reversal)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@router.get("/reports/trial-balance", response_model=list[TrialBalanceLine])
async def get_trial_balance(
    session: DbSession, period_id: uuid.UUID | None = None
) -> list[TrialBalanceLine]:
    return await service.get_trial_balance(session, period_id=period_id)
