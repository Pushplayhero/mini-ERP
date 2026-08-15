"""Posting engine — turns business events into balanced journal entries (ADR-003).

Lives in `ledger` (not `core`) per ADR-003 Decision 1: `core` must never
import business modules (import-linter contract), and turning events into
entries is ledger's own domain competence — it already owns entry creation,
periods, and numbering. Publishers (`sales`, future `receivables`) know
nothing about this module; they just call `app.core.events.publish`.
`app.core.events` itself knows nothing about `ledger` either — it dispatches
to whatever registered `event_type -> handler` mapping exists, which is
exactly the decoupling ADR-004 Decision 3 is built on.

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

Week 5: `sales.goods_shipped` is the first real posting event (ADR-007)
---------------------------------------------------------------------------
Week 3's `test.synthetic_sale` self-validation event — a synthetic,
self-contained stand-in whose only job was to prove the full pipeline works
end to end (publish -> outbox write -> handler dispatch -> rule lookup ->
per-company account resolution -> balanced journal entry -> idempotent
re-delivery) — is retired in this deploy: its schema, rule, and event_type
constant are deleted outright, not merely superseded (ADR-007 "Synthetic
event retirement"; see migration 0005's R1 for how already-outboxed rows of
that event_type are neutralized rather than left to poison replay forever).
`sales.goods_shipped` (published by `sales.service.ship_order`) takes its
place as the first posting rule driven by a real business event: debit
`5000 COGS` / credit `1300 Inventory`, amount = the event's `total_cost`.

`GoodsShippedPayload` is defined here, not in `sales.events` (where
`sales.order_confirmed`'s `SalesOrderConfirmedPayload` lives) — a deliberate
asymmetry, not an inconsistency: `sales.order_confirmed` has zero
subscribers and its schema is purely descriptive of what sales did.
`sales.goods_shipped` has two subscribers (`inventory`, this module) and
this module additionally needs the payload shape at rule-definition time
(`POSTING_RULES`'s `amount_field="total_cost"` reads a key that must exist
on whatever schema gets registered for this event_type) — so the schema
lives next to the rule that consumes it, exactly as `SyntheticSalePayload`
used to live next to its own rule. `sales.service.ship_order` never imports
this class: it publishes a plain `dict` shaped to match it, and
`app.core.events.publish` validates that dict against whichever schema
`app.main` registered for `"sales.goods_shipped"` (this one) — the
independence boundary holds because the *string* `event_type` is the only
thing both modules need to agree on, never a shared Python type.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
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
# Item 4). Companies that want kernel-posted entries must seed accounts with
# these codes. Documented here, next to the rules that depend on them, since
# there is no seed-data module yet to own this list.
#
#   1000  Cash                (asset)     -- receivables.payment_received/_voided (ADR-008)
#   1100  Accounts Receivable (asset)     -- receivables.invoice_issued/_voided (ADR-008)
#                                             — also the one kernel-mandated
#                                             control account (ADR-008 R5/R11,
#                                             `ledger.service
#                                             .KERNEL_CONTROL_ACCOUNT_CODES`):
#                                             protected from manual entries
#                                             and manual reversal.
#   4000  Revenue             (revenue)   -- receivables.invoice_issued/_voided (ADR-008)
#   5000  COGS                (expense)   -- sales.goods_shipped (ADR-007)
#   1300  Inventory           (asset)     -- sales.goods_shipped (ADR-007)
# ---------------------------------------------------------------------------

GOODS_SHIPPED_EVENT_TYPE = "sales.goods_shipped"

# ADR-008: receivables' four posting events. Same "string is the only
# shared contract" pattern as `GOODS_SHIPPED_EVENT_TYPE` above — these
# literals are independently declared in `app.modules.receivables.events`
# too (never imported from there; see that module's docstring).
RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE = "receivables.invoice_issued"
RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE = "receivables.invoice_voided"
RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE = "receivables.payment_received"
RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE = "receivables.payment_voided"


class GoodsShippedLine(BaseModel):
    """One shipped line's cost detail — kept in the payload alongside the
    total (ADR-007 "per-line `unit_cost` + total `total_cost` payload
    redundancy is deliberate") so the entry is derivable from the event
    alone, forever, without re-querying `products.standard_cost` at replay
    time (a product's standard cost can change after the fact).
    """

    product_id: uuid.UUID
    qty: Decimal
    unit_cost: Decimal


class GoodsShippedPayload(BaseModel):
    """Payload contract for `sales.goods_shipped` (ADR-007). See module
    docstring for why this schema is defined here rather than in
    `sales.events`.
    """

    company_id: uuid.UUID
    source_id: uuid.UUID
    order_no: str
    lines: list[GoodsShippedLine]
    total_cost: Decimal
    shipped_at: datetime


# ADR-008: receivables' four payload schemas. Defined here rather than in
# `receivables.events`, same asymmetry `GoodsShippedPayload` established —
# each schema needs to sit next to the rule that consumes it
# (`amount_field` reads a key that must exist on the shape registered for
# that event_type). `receivables.service` never imports these; it publishes
# plain dicts shaped to match, exactly like `sales.service.ship_order` does
# for `GoodsShippedPayload`.
#
# Every field is required (ADR-008 R1/R3): `source_id` feeds
# `uq_journal_entries_source`'s idempotency key, and `event_date` is the
# field `handle_posting_event`'s entry-date resolution already recognizes
# (checked before the `shipped_at` fallback) — set by the publisher to
# `invoice_date` / `received_at`'s date / the void date respectively. Note
# that a void event's `source_id` equals the *original* document's id: this
# is safe because the idempotency key includes `source_type` (the
# event_type string), so issue and void of the same invoice are distinct
# keys, while replaying either individually stays a no-op.


class InvoiceIssuedPayload(BaseModel):
    company_id: uuid.UUID
    source_id: uuid.UUID
    event_date: date
    invoice_no: str
    order_id: uuid.UUID
    customer_id: uuid.UUID
    total: Decimal


class InvoiceVoidedPayload(BaseModel):
    company_id: uuid.UUID
    source_id: uuid.UUID
    event_date: date
    invoice_no: str
    total: Decimal


class PaymentReceivedPayload(BaseModel):
    company_id: uuid.UUID
    source_id: uuid.UUID
    event_date: date
    payment_no: str
    customer_id: uuid.UUID
    amount: Decimal


class PaymentVoidedPayload(BaseModel):
    company_id: uuid.UUID
    source_id: uuid.UUID
    event_date: date
    payment_no: str
    amount: Decimal


POSTING_RULES: dict[str, list[PostingRule]] = {
    GOODS_SHIPPED_EVENT_TYPE: [
        PostingRule(
            debit_account_code="5000",
            credit_account_code="1300",
            amount_field="total_cost",
        ),
    ],
    # ADR-008 Decision 1: revenue recognized at invoice issue.
    RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE: [
        PostingRule(debit_account_code="1100", credit_account_code="4000", amount_field="total"),
    ],
    # ADR-008 Decision 4: void is a rule-driven contra entry, not a
    # `ledger` manual reversal — the accounts flip relative to issue.
    RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE: [
        PostingRule(debit_account_code="4000", credit_account_code="1100", amount_field="total"),
    ],
    # ADR-008 Decision 2: receipt-time AR relief for the full receipt
    # amount; which invoice(s) it settles is subledger bookkeeping that
    # posts nothing.
    RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE: [
        PostingRule(debit_account_code="1000", credit_account_code="1100", amount_field="amount"),
    ],
    RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE: [
        PostingRule(debit_account_code="1100", credit_account_code="1000", amount_field="amount"),
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

    Normative flow (ADR-003 "Posting flow"): rule lookup -> zero-cost skip
    check (ADR-007 Decision 3, new this week) -> per-company account
    resolution -> build lines -> `post_journal_entry` inside a `SAVEPOINT`
    (R2) -> a duplicate `(company_id, source_type, source_id)` hits
    `uq_journal_entries_source` and no-ops (only the savepoint rolls back);
    any other failure propagates and aborts the caller's whole transaction
    (rule/account resolution failures never even reach the savepoint —
    nothing has been written yet at that point).
    """
    rules = POSTING_RULES.get(event_type)
    if not rules:
        raise PostingRuleNotFoundError(
            f"no posting rule registered for event_type {event_type!r} — the whole "
            "business transaction aborts rather than half-posting (ADR-003 Decision 2)"
        )

    company_id = require_current_company_id()
    payload_dict = payload.model_dump()

    # Zero-cost skip (ADR-007 Decision 3): computed *before* touching
    # accounts or the DB at all. If every rule's amount for this event is
    # zero (e.g. a shipment of products that all have `standard_cost=0`),
    # there is nothing of value to post — a zero-amount journal line would
    # (correctly) be rejected by `ledger.service._validate_lines`'s own
    # "must have a nonzero debit or credit" rule, so attempting the entry
    # would just trade a legitimate no-op for a confusing 422. This check
    # deliberately precedes `_resolve_account_ids` too: a company whose
    # shipments always net to zero cost should not be required to have the
    # standard chart of accounts seeded just to ship. The stock still
    # moves — this function is never in that transaction's path at all
    # for the inventory side; only the journal-entry half is skipped.
    amounts = {rule.amount_field: Decimal(str(payload_dict[rule.amount_field])) for rule in rules}
    if all(amount == 0 for amount in amounts.values()):
        logger.info(
            "event %s source_id=%s has zero posting amount(s) %s; skipping journal entry "
            "(ADR-007 Decision 3 zero-cost skip)",
            event_type,
            payload_dict.get("source_id"),
            amounts,
        )
        return

    codes = {code for rule in rules for code in (rule.debit_account_code, rule.credit_account_code)}
    account_ids = await _resolve_account_ids(session, company_id, codes)

    lines: list[JournalLineCreate] = []
    zero = Decimal("0")
    for rule in rules:
        amount = amounts[rule.amount_field]
        debit_account_id = account_ids[rule.debit_account_code]
        credit_account_id = account_ids[rule.credit_account_code]
        lines.append(_line(debit_account_id, debit=amount, credit=zero))
        lines.append(_line(credit_account_id, debit=zero, credit=amount))

    # `entry_date` resolution stays generic across future event types: a
    # payload may carry an explicit `event_date` (the original
    # self-validation event's shape); `sales.goods_shipped` instead carries
    # `shipped_at` (a full timestamp — the actual business moment goods
    # left), which is more accurate than defaulting to "today" whenever it
    # is present. Only falls all the way back to `date.today()` for a
    # payload shape that supplies neither.
    entry_date = payload_dict.get("event_date")
    if entry_date is None:
        shipped_at = payload_dict.get("shipped_at")
        if isinstance(shipped_at, datetime):
            entry_date = shipped_at.date()
    if entry_date is None:
        entry_date = date.today()
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
