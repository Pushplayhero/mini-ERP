# ledger — Week 2 + Week 3

Journal entries, accounting periods, the trial balance report (Week 2, per
`docs/adr/ADR-005-ledger-journal-design.md`), and the posting engine (Week
3, per `docs/adr/ADR-003-posting-engine.md`, alongside `app/core/events.py`
per `docs/adr/ADR-004-event-bus.md`). Chart of accounts (`accounts`) lives
in `masterdata` — this module only consumes `account_id` by reference (see
"Design decisions" below), it never imports `masterdata`'s models.

## What's here

- `models.py` — `AccountingPeriod`, `LedgerSequence`, `JournalEntry`,
  `JournalLine`. See migration `0002_ledger_initial` for the DB-level half
  of the invariants (CHECK constraints, the deferred balance constraint
  trigger, the immutability triggers), and `0003_posting_source_index` for
  the Week 3 idempotency index.
- `schemas.py` — DTOs. `entry_no`, `company_id`, `reversal_of_id`,
  `posted_at`, `period_id` are always server-decided, never client-supplied.
- `service.py` — period create/close, journal entry create/reverse, trial
  balance aggregation. `post_journal_entry` (ADR-003 R1) is the
  non-committing core both the HTTP layer (`create_journal_entry`) and the
  posting engine (`posting.handle_posting_event`) call; commit/rollback is
  always the caller's decision, never this module's.
- `router.py` — thin FastAPI routes. No UPDATE/DELETE endpoints for entries
  or lines, by design (see Decision 3 in the ADR) — corrections are
  reversal entries only.
- `events.py` — placeholder; ledger publishes nothing in Phase 1 (see file
  docstring). Not to be confused with `posting.py`, which *subscribes* to
  other modules' events.
- `posting.py` — the posting engine (ADR-003): declarative `POSTING_RULES`,
  per-company account-code resolution, and the SAVEPOINT-wrapped handler
  that turns an event into a balanced journal entry. Week 3 ships exactly
  one synthetic self-validation event (`test.synthetic_sale`, see the
  module docstring) since no real business module publishes events yet.

## Design decisions carried over from ADR-005

1. **Debit/credit as two non-negative columns**, both a transaction-currency
   pair and a functional-currency pair per line (R1). Phase 1 only accepts
   the functional currency (`TWD`) — the columns exist for Phase 2+
   multi-currency, the conversion logic does not.
2. **Balance enforced twice**: fast-fail in `service._validate_lines`, and
   again by a deferred Postgres constraint trigger at commit (functional
   currency only) — defense in depth against any writer that bypasses this
   service.
3. **Immutable**: no UPDATE/DELETE code path exists, and `BEFORE UPDATE OR
   DELETE` triggers on `journal_entries`/`journal_lines` make it physically
   impossible even via raw SQL. Corrections are `POST
   /journal-entries/{id}/reverse` (R3): a new entry with debit/credit
   swapped, linked via `reversal_of_id` (`UNIQUE` — reversible once).
4. **Trial balance is computed on the fly** — a `GROUP BY` over
   `journal_lines` joined to `accounts`, no maintained balance table in
   Phase 1 (Decision 4).
5. **Gapless entry numbering** (R2): `JE-{YYYY}-{NNNNNN}`, allocated from a
   `(company_id, year)`-scoped counter locked with `SELECT ... FOR UPDATE`
   inside the same transaction as the entry — a rollback rolls the counter
   back too.
6. **Period-close vs posting race** (R4): posting takes `SELECT ... FOR
   SHARE` on the target period row before inserting; closing takes `SELECT
   ... FOR UPDATE` on the same row. No entry can land in a period that was
   closed by the time its transaction committed.

## Independence boundary (import-linter)

`ledger` never imports `app.modules.masterdata`. Two things would ordinarily
want to:

- **"Does this `account_id` belong to my company?"** — answered via a
  lightweight `sqlalchemy.table("accounts", ...)` reference (Core, not the
  ORM model), not by importing `masterdata.models.Account`. The DB foreign
  key alone would prove the account *exists* but not that it belongs to the
  posting company (accounts have no cross-company uniqueness at the FK
  level), so this is a real check, not just a formality.
- **Trial balance account metadata** (`code`/`name`/`type`) — same
  `sqlalchemy.table()` reference, joined in the aggregate query.
- **"What is this company's functional currency?"** — *not* answered by a
  cross-module lookup at all; Phase 1 hardcodes `TWD` as the only accepted
  currency (`service.FUNCTIONAL_CURRENCY`) instead, since master-plan §10.1
  already states Phase 1 is TWD-only in practice. Revisit when real
  multi-currency posting needs each company's actual functional currency —
  likely via a published snapshot/event rather than a live import.

## Not implemented (Week 4+)

Real business events (`sales.goods_shipped`, `receivables.invoice_issued`,
...) and their posting rules — Week 3 ships the pipeline and one synthetic
self-validation event only (see `posting.py`'s module docstring). Also
still out of scope: real multi-currency conversion, period reopening, any
admin data-repair escape hatch, and per-company rule overrides (deferred to
the Phase 2 plugin system per ADR-003's Consequences section).
