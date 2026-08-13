"""Posting engine — turns business events into balanced journal entries (ADR-003).

Lives in `ledger` (not `core`) per ADR-003 Decision 1: `core` must never
import business modules (import-linter contract), and turning events into
entries is ledger's own domain competence — it already owns entry creation,
periods, and numbering. Publishers (future `sales`/`inventory`/
`receivables`) know nothing about this module; they just call
`app.core.events.publish`. `app.core.events` itself knows nothing about
`ledger` either — it dispatches to whatever registered `event_type ->
handler` mapping exists, which is exactly the decoupling ADR-004 Decision 3
is built on.

Rule format (ADR-003 Decision 2): declarative, in code, referencing accounts
by `code` — resolved to this posting company's actual `accounts.id` at
posting time via a lightweight `sqlalchemy.table()` Core reference (same
pattern as `ledger.service._ACCOUNTS`; not an import of
`masterdata.models.Account`, which would cross the module-independence
boundary import-linter enforces).

Idempotency (ADR-003 Decision 3 / R2): duplicate delivery of the same
`(company_id, source_type, source_id)` is a DB-enforced no-op via migration
0003's partial unique index (`uq_journal_entries_source`), caught here
inside a `SAVEPOINT` so only the duplicate insert rolls back — never the
publisher's whole transaction.

Week 3 self-validation event — NOT the final rule set
-------------------------------------------------------
There is no real `sales`/`inventory`/`receivables` module yet (Week 4+).
`POSTING_RULES["test.synthetic_sale"]` and `SyntheticSalePayload` below are
a synthetic, self-contained stand-in whose only job is to prove the full
pipeline works end to end: publish -> outbox write -> handler dispatch ->
rule lookup -> per-company account resolution -> balanced journal entry ->
idempotent re-delivery. When Week 4 lands the real `sales.goods_shipped` /
`receivables.invoice_issued` events (see ADR-003 Decision 2's example rule
set), this synthetic entry is deleted/replaced, not extended.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import column as sa_column
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventHandler
from app.core.exceptions import DomainValidationError
from app.core.tenancy import require_current_company_id
from app.modules.ledger.schemas import JournalEntryCreate, JournalLineCreate
from app.modules.ledger.service import FUNCTIONAL_CURRENCY, post_journal_entry

logger = logging.getLogger(__name__)

# Same Core-level reference pattern as `ledger.service._ACCOUNTS` — see that
# module's docstring for why this is not an import of
# `masterdata.models.Account`.
_ACCOUNTS = sa_table(
    "accounts",
    sa_column("id"),
    sa_column("company_id"),
    sa_column("code"),
)


@dataclass(frozen=True)
class PostingRule:
    """One debit/credit pair a posting rule contributes to the journal entry.

    `amount_field` names the field in the event's (validated) payload model
    to read the line amount from.
    """

    debit_account_code: str
    credit_account_code: str
    amount_field: str


class PostingRuleNotFoundError(DomainValidationError):
    """No `PostingRule`s are registered for an event's `event_type`.

    ADR-003's normative posting flow: "an event with no valid posting
    configuration cannot half-succeed" — raising here aborts the whole
    publishing transaction (the event's business write included), it never
    silently drops the event.
    """


class AccountResolutionError(DomainValidationError):
    """A posting rule's account `code` has no matching account for this company.

    ADR-003 Decision 2: "missing account = the whole business transaction
    aborts" — kernel posting rules assume every company has seeded the
    standard chart-of-account codes documented below.
    """


# ---------------------------------------------------------------------------
# Standard chart-of-account codes the kernel rules assume (ADR-003 Action
# Item 4). Companies that want kernel-posted entries (this synthetic rule
# today; real sales/receivables rules from Week 4) must seed accounts with
# these codes. Documented here, next to the rules that depend on them, since
# there is no seed-data module yet to own this list.
#
#   1100  Accounts Receivable (asset)
#   4000  Revenue             (revenue)
# ---------------------------------------------------------------------------

SYNTHETIC_SALE_EVENT_TYPE = "test.synthetic_sale"


class SyntheticSalePayload(BaseModel):
    """Payload contract for `test.synthetic_sale` — Week 3 self-validation only.

    Mirrors the shape a real `receivables.invoice_issued`-style event would
    have (`company_id` + a traceable `source_id` + an `amount`), without
    being tied to any real business module.
    """

    company_id: uuid.UUID
    source_id: uuid.UUID
    amount: Decimal
    event_date: date | None = None


POSTING_RULES: dict[str, list[PostingRule]] = {
    SYNTHETIC_SALE_EVENT_TYPE: [
        PostingRule(
            debit_account_code="1100",
            credit_account_code="4000",
            amount_field="amount",
        ),
    ],
}


async def _resolve_account_ids(
    session: AsyncSession, company_id: uuid.UUID, codes: set[str]
) -> dict[str, uuid.UUID]:
    if not codes:
        return {}
    result = await session.execute(
        select(_ACCOUNTS.c.code, _ACCOUNTS.c.id).where(
            _ACCOUNTS.c.company_id == company_id, _ACCOUNTS.c.code.in_(codes)
        )
    )
    found: dict[str, uuid.UUID] = {row.code: row.id for row in result.all()}
    missing = codes - found.keys()
    if missing:
        raise AccountResolutionError(
            f"company {company_id} has no account(s) with code(s) {sorted(missing)} "
            "required by posting rules — see the standard chart-of-account codes "
            "documented in ledger.posting (ADR-003 Decision 2)"
        )
    return found


def _is_source_conflict(exc: IntegrityError) -> bool:
    """True iff `exc` is the `uq_journal_entries_source` unique-index conflict.

    Any other `IntegrityError` (a different constraint, a genuinely broken
    write) must NOT be swallowed here — only this one specific, expected
    "duplicate delivery" conflict is a no-op (ADR-003 R2).
    """
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name:
        return str(constraint_name) == "uq_journal_entries_source"
    return "uq_journal_entries_source" in str(exc)


def _line(account_id: uuid.UUID, *, debit: Decimal, credit: Decimal) -> JournalLineCreate:
    return JournalLineCreate(
        account_id=account_id,
        currency_code=FUNCTIONAL_CURRENCY,
        txn_debit=debit,
        txn_credit=credit,
        debit=debit,
        credit=credit,
        exchange_rate=Decimal("1"),
    )


async def handle_posting_event(session: AsyncSession, event_type: str, payload: BaseModel) -> None:
    """Turn one event into (at most) one balanced journal entry.

    Normative flow (ADR-003 "Posting flow"): rule lookup -> per-company
    account resolution -> build lines -> `post_journal_entry` inside a
    `SAVEPOINT` (R2) -> a duplicate `(company_id, source_type, source_id)`
    hits `uq_journal_entries_source` and no-ops (only the savepoint rolls
    back); any other failure propagates and aborts the caller's whole
    transaction (rule/account resolution failures never even reach the
    savepoint — nothing has been written yet at that point).
    """
    rules = POSTING_RULES.get(event_type)
    if not rules:
        raise PostingRuleNotFoundError(
            f"no posting rule registered for event_type {event_type!r} — the whole "
            "business transaction aborts rather than half-posting (ADR-003 Decision 2)"
        )

    company_id = require_current_company_id()
    payload_dict = payload.model_dump()

    codes = {code for rule in rules for code in (rule.debit_account_code, rule.credit_account_code)}
    account_ids = await _resolve_account_ids(session, company_id, codes)

    lines: list[JournalLineCreate] = []
    zero = Decimal("0")
    for rule in rules:
        amount = Decimal(str(payload_dict[rule.amount_field]))
        debit_account_id = account_ids[rule.debit_account_code]
        credit_account_id = account_ids[rule.credit_account_code]
        lines.append(_line(debit_account_id, debit=amount, credit=zero))
        lines.append(_line(credit_account_id, debit=zero, credit=amount))

    entry_date = payload_dict.get("event_date") or date.today()
    source_id = payload_dict.get("source_id")

    data = JournalEntryCreate(
        entry_date=entry_date,
        source_type=event_type,
        source_id=source_id,
        lines=lines,
    )

    try:
        async with session.begin_nested():
            await post_journal_entry(session, data)
    except IntegrityError as exc:
        if not _is_source_conflict(exc):
            raise
        logger.info(
            "event %s source_id=%s already posted for company %s; skipping duplicate delivery",
            event_type,
            source_id,
            company_id,
        )


def make_posting_handler(event_type: str) -> EventHandler:
    """Bind `event_type` into a bus-shaped handler (`(session, payload) -> None`).

    `handle_posting_event` takes `event_type` explicitly, so one function
    handles every event_type in `POSTING_RULES` — but
    `app.core.events.subscribe` expects a handler with exactly the
    `(session, payload)` signature (`EventHandler`). This factory closes
    over `event_type` to bridge the two; called once per event_type at
    startup (see `app.main`).
    """

    async def _handler(session: AsyncSession, payload: BaseModel) -> None:
        await handle_posting_event(session, event_type, payload)

    return _handler
