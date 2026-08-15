# ADR-008: Receivables — invoicing, payment application, and AR aging

**Status:** Accepted (consensus review v5 passed 2026-08-15; see "Consensus Revisions")
**Date:** 2026-08-15
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

## Context

Week 6 completes the Phase 1 O2C line: `receivables` — invoices issued off
sales orders, customer payments, payment application (沖帳), and the
AR-aging report. This closes the demo loop the master plan promises:
confirm → ship (COGS/Inventory) → invoice (AR/Revenue) → payment (Cash/AR)
→ a trial balance that balanced because business happened, plus an aging
report that ties back to the ledger's AR control account.

Fixed constraints, all established by prior ADRs and applied here without
relitigation: module independence via Core `table()` references with
explicit `company_id` filters (ADR-003/HANDOFF doctrine), sync
same-transaction bus dispatch (ADR-004), posting idempotency via partial
unique index + SAVEPOINT (ADR-003 R2), flush-only core + committing wrapper
with the try/except around the core call (ADR-003 R1 + Week 5 diff-review
fix #2), row-lock-before-state-check (ADR-005 R4 / ADR-006 R1), maintained
summary columns rebuildable from append-only facts (§10.5), money
NUMERIC(20,6) TWD-only with Pydantic-layer round-half-even and explicit
`ge=0` bounds, and fail-closed tenancy.

ADR-006 left one documented revisit for this week: "AR balance joins the
credit-exposure formula in Week 6." No tax of any kind is computed in
Phase 1 (營業稅/e-invoice are Phase 4; documented Non-Goal here).

## Decision

1. **Invoices are issued from `shipped` orders only**, full-order, at most
   one non-voided invoice per order (DB-enforced), header-level (no
   invoice-lines table in Phase 1). `POST /invoices` publishes
   `receivables.invoice_issued` → posting rule debit `1100 AR` / credit
   `4000 Revenue`, amount = invoice `total`.
2. **Payments are customer-level receipts with allocations to invoices**
   (`payments` + `payment_allocations`, one payment → many invoices;
   unallocated remainder is on-account). `POST /payments` publishes
   `receivables.payment_received` → posting rule debit `1000 Cash` /
   credit `1100 AR`, amount = payment `amount`. **Allocation itself is a
   subledger fact, not an accounting event** — it posts nothing. Payment
   creation is client-retry-safe via a required `external_ref` unique key,
   and payments have the same void-while-unapplied correction path
   invoices do (R2).
3. **Invoice/payment open balances are maintained columns**
   (`invoices.settled_amount`, `payments.allocated_amount`), updated under
   `SELECT ... FOR UPDATE` in the same transaction as the allocation rows
   that justify them, CHECK-constrained, and rebuildable from
   `payment_allocations` (reconciliation-tested).
4. **Void is the only invoice correction path**, allowed only while
   `settled_amount = 0`; it publishes `receivables.invoice_voided` →
   posting rule debit `4000 Revenue` / credit `1100 AR` (a rule-driven
   contra entry, NOT `ledger`'s manual-reversal endpoint), after which the
   order may be re-invoiced.
5. **AR aging is a CURRENT-STATE report** over open invoices
   (`total - settled_amount`), bucketed by days past `due_date` measured
   at a `bucket_date` (not a historical cutoff — R4), with per-customer
   unapplied credits reported alongside so the report's net total ties to
   the `1100` control-account balance — a property made true by
   protecting control accounts from manual postings (R5) and
   Hypothesis-tested.
6. **The credit-limit plugin's exposure formula gains the AR terms**
   (ADR-006 revisit): uninvoiced `CONFIRMED`/`SHIPPED` order totals + open
   invoice balances; unapplied credits deliberately do NOT reduce exposure
   in Phase 1 (conservative).

## Options Considered

### Decision 1 — when and how invoices are issued

**Option A: from `shipped` orders only, full-order, one live invoice per
order, header-only** — chosen

| Dimension | Assessment |
|-----------|------------|
| Accounting | Revenue recognized at/after delivery, same granularity and moment-shape as Week 5's COGS — the two entries that must tell one story do |
| Complexity | 1:1 with an immutable `shipped` order — totals, currency, and snapshots are simply copied, never recomputed |
| Concurrency | No order state transition happens at invoice time, so no R1 order-row lock is needed — a partial unique index is the whole race story (see below) |

Data model (money NUMERIC(20,6); audit + custom_data mixins as
established; all tables tenant-scoped):

```
invoices(id, company_id, invoice_no INV-{YYYY}-{NNNNNN}, order_id, customer_id,
         status[open|partial|paid|voided], currency_code,
         order_shipped_at,             -- snapshot of the order's shipped_at; see R13
         invoice_date, due_date,
         total,                        -- copied from the order, never client-supplied
         settled_amount,               -- maintained; see Decision 3
         snapshot_customer_code, snapshot_customer_name,   -- re-copied from the order
         voided_at, ...)
receivables_sequences(company_id, year, doc_type[invoice|payment], next_no)
```

Rules: the order must be `SHIPPED` (422 otherwise); `total`,
`currency_code`, `customer_id`, and customer snapshots are copied from the
order (which is immutable once shipped — ADR-006/007), so `total > 0`
holds by inheritance from ADR-006 R2 and is CHECK-backstopped anyway.
**`sales_orders.shipped_at` is persisted (R13, corrected by R17)** —
`ship_order` sets it alongside the `confirmed → shipped` transition. The
column is **nullable** — draft/confirmed/cancelled orders never have a
shipment timestamp, only `SHIPPED` ones do — with
`CHECK (status::text != 'SHIPPED' OR shipped_at IS NOT NULL)` (the
`status::text` form is mandatory: `SHIPPED` is an enum value added by
migration 0005, and this project's whole `upgrade head` runs in one
transaction, so a typed comparison against it here would hit the same
"unsafe new enum value" error migration 0006 already worked around — see
that migration's docstring). Migration 0008 **backfills** `shipped_at`
for any pre-existing `SHIPPED` row from that order's own
`sales.goods_shipped` outbox payload (`shipped_at` was always in the
event — Decision 4 of ADR-007 — even though the order row never stored
it; the backfill is a data migration keyed on `outbox.payload->>
'source_id' = sales_orders.id::text AND event_type =
'sales.goods_shipped'`, one query, no ambiguity since exactly one such
event exists per shipped order by the idempotent-posting invariant); a
populated-upgrade test (not just from-empty) proves this. This is what
makes the invoice-date rule below enforceable at all, closing a real gap
(the event payload carried `shipped_at` but the order row never did).
`invoices.order_shipped_at` is copied from it at issue time — same
snapshot-at-write pattern as every other order-derived invoice field, and
necessary because a Postgres `CHECK` cannot reference another table's
column, so the comparison must be local (and trivially non-null: an
invoice can only be issued from a `SHIPPED` order, which by the CHECK
above always has `shipped_at` set). `invoice_date` defaults to today
(never before `shipped_at`'s date by construction of the default), is
client-overridable **but bounded**: `CHECK (invoice_date >=
order_shipped_at::date)` — the earliest an invoice can legally date to is
the day goods actually left, keeping Decision 1's "revenue recognized at/
after delivery" claim true instead of aspirational. `due_date` defaults
to `invoice_date +
customers.payment_terms_days` (new masterdata column, `INT NOT NULL
DEFAULT 30`, `CHECK (payment_terms_days BETWEEN 0 AND 365)`, additive —
same low-risk precedent as ADR-007's `standard_cost`); both are
client-overridable at issue, with `CHECK (due_date >= invoice_date)`.
Reading terms from *current* masterdata at issue is a **documented,
deliberate exception to the confirm-time-snapshot doctrine (R9)**: terms
are billing-time attributes (the order froze what is owed, not when it is
due), and the invoice's stored `due_date` is itself the frozen outcome —
nothing about the invoice ever re-reads the customer after issue. Stated
in the model docstring so nobody "fixes" it into a confirm-time snapshot
without rereading this note. Whether
`invoice_date` falls in an open accounting period is *not* re-checked by
receivables — the posting subscriber's `post_journal_entry` already
enforces period state, and its failure aborts the whole issue transaction
(fail-closed, one enforcement point).

**Double-invoice race**: partial unique index
`uq_invoices_order_live ON invoices (company_id, order_id) WHERE
status != 'VOIDED'` — two concurrent issues both pass the read check, the
second insert hits the index, `_commit_or_conflict()`/the wrapper's
try/except (fix-#2 pattern) turns it into a 409. No order-row lock: unlike
confirm/cancel/ship there is no status transition on the order row to
protect, and `SHIPPED` is terminal (cannot regress), so R1's
lock-before-state-check has nothing to guard here — the DB constraint is
the invariant, stated in the model docstring.

`invoice_no`/`payment_no` come from `receivables_sequences` (get-or-create
+ `FOR UPDATE`, the `sales_sequences` pattern with a `doc_type`
discriminator column in the PK). **Gaplessness is NOT required** — these
are internal document numbers; Taiwan's legally-numbered 發票字軌 is
explicitly the Phase 4 `tw.einvoice` plugin's problem, not Phase 1's
(master plan §3). Documented like ADR-006's order numbers.

**Option B: allow invoicing from `confirmed` (bill-before-ship).** Real
businesses do prepayment — but it splits revenue recognition from COGS
timing, forces "invoiced but later cancelled" order semantics ADR-006/007
never defined (cancel is legal from `confirmed`!), and doubles the state
matrix for a demo that needs one clean path. Deferred with deposits/
prepayments (Phase 2+), not half-built.

**Option C: invoice lines table now.** Phase 4 e-invoice and credit notes
will want lines — but a Phase 1 invoice is 1:1 with a full, immutable,
already-snapshotted order; its lines ARE the order's lines, reachable via
`order_id`. A future lines table is purely additive. Rejected as premature.

### Decision 2 — payment shape and what posts

**Option A: customer-level payment + allocations subtable; receipt posts,
allocation doesn't** — chosen

```
payments(id, company_id, payment_no PAY-{YYYY}-{NNNNNN}, customer_id,
         status[received|voided],
         external_ref,                 -- client idempotency key; see R2
         currency_code, amount > 0,
         allocated_amount,             -- maintained; see Decision 3
         received_at, voided_at, ...)
payment_allocations(id, company_id, payment_id, invoice_id,
                    command_id,        -- FK to payment_allocation_commands; see R14
                    amount > 0, created_at)
payment_allocation_commands(id, company_id, payment_id, request_ref,
                    request_fingerprint, created_at)   -- R14: one row per allocate-payment call
```

The accounting story: cash arriving is the accounting event (Cash up, AR
control down, full receipt amount, at `received_at`); *which invoice* the
cash settles is subledger bookkeeping that moves no value between
accounts. So `POST /payments` publishes `receivables.payment_received`
(posting rule above, `source_id` = payment id, idempotent via
`uq_journal_entries_source` exactly like every posting event), while
allocations — whether passed inline at creation or added later via
`POST /payments/{id}/allocations` — write only `payment_allocations` rows
and the two maintained columns. An unallocated remainder is on-account
credit: already in the ledger's AR balance, visible in aging (Decision 5),
applicable later. No allocation event is published in Phase 1 (nothing
consumes it; deferred, documented — the outbox pattern makes adding it
additive).

Invariants, all 409 on violation and all under locks (Decision 3):
`Σ allocations(payment) ≤ payment.amount`;
`Σ allocations(invoice) ≤ invoice.total`; allocation targets must be
non-voided invoices of the *same customer* and same currency; the source
payment must be non-voided. Phase 1's "TWD-only" is NOT currently
trivially true — see R12: `customers.currency_code` is unrestricted
today, so a USD customer's order could reach `shipped` and its raw number
get posted as TWD. R12 closes this at the masterdata gate (the fix-#4
pattern) with receivables-side defense in depth.

**Retry safety and correction (R2)**: `external_ref` is a required,
client-supplied reference (bank slip no., import row id, or client UUID),
`UNIQUE (company_id, external_ref)` — a retried `POST /payments` hits the
constraint and 409s instead of double-posting Cash/AR; the response body
of the 409 identifies the existing payment. `POST /payments/{id}/void`
mirrors invoice void exactly (Decision 4): R1 lock on the payment row,
legal only while `status = received` AND `allocated_amount = 0`, publishes
`receivables.payment_voided` → posting rule Dr `1100` / Cr `1000`,
`source_id` = payment id. Voided payments drop out of unapplied-credit
sums and cannot receive allocations. A fat-fingered *allocated* payment
requires un-allocation, deferred with credit notes (same Phase 2 boundary
as partially-settled invoice void, same loud documentation).

**Option B: payment allocated at creation only (no later application).**
Simpler surface, but kills the on-account → apply-later flow that 沖帳
actually means in practice, and the "apply" service function must exist
for creation-time allocations anyway — the second endpoint is the same
function exposed. Rejected.

**Option C: post AR relief per-allocation instead of per-receipt** (Dr
Cash/Cr AR only as invoices are applied, unapplied cash in a deposits
liability account). More orthodox for audited deposits handling — and
Phase 2's likely destination — but it doubles the posting rules, adds an
account, and makes the control-account tie-out depend on allocation
timing. Phase 1 chooses the simpler model and *proves* the tie-out
property instead (Decision 5). Documented revisit.

### Decision 3 — balance representation

**Option A: maintained `settled_amount`/`allocated_amount` columns under
`FOR UPDATE`, rebuildable from allocations** — chosen

This is §10.5's stock pattern transplanted: `payment_allocations` is the
append-only fact table; the two columns are transactionally-maintained
projections.

**Allocation command contract (R7, hardened by R14)** — one flush-only
core, `allocate_payment(session, payment_id, request_ref, allocations)`,
used by both entry points (inline at `POST /payments`, later via
`POST /payments/{id}/allocations`; request shape
`{request_ref, allocations: [{invoice_id, amount}]}`, non-empty;
`request_ref` is required on the late endpoint and defaults to the
payment's `external_ref` for the inline batch). The batch is
**atomic** — any invalid target or capacity violation rejects the whole
request, nothing partial commits. Duplicate `invoice_id`s within one
request are a 422 (client bug, not aggregated silently).

**Retry safety, correctly scoped (R14 — replaces the R10 design, which
Codex v3 found didn't actually make `request_ref` unique *per command*: a
row-level `UNIQUE (..., request_ref, invoice_id)` let the same reference
succeed again with a disjoint invoice set, double-settling AR)**: a new
`payment_allocation_commands(id, company_id, payment_id, request_ref,
request_fingerprint, created_at)` header row, `UNIQUE (company_id,
payment_id, request_ref)`, written *first* inside the same transaction as
its allocation rows (which gain `command_id` instead of carrying
`request_ref` directly). `request_fingerprint` is a hash of the
normalized `{invoice_id, amount}` set; on a `request_ref` conflict the
core re-reads the existing command's fingerprint — a match means "this
exact command already ran," and the response 200s with the prior result
(true idempotent replay); a mismatch means a different body reused the
reference, which is the client contract violation the ADR always meant
to reject, now actually rejected with a distinct 409 the response body
identifies as such. One `request_ref` therefore commits **at most one**
distinct allocation set, ever, closing the double-settlement path.

Lock order is **normative, deadlock-avoiding**: payment row first, then
target invoice rows sorted by `id`; capacity on both sides is re-checked
under the locks; then the command header and allocation rows are
inserted, both maintained columns updated, and invoice `status`
(`open ↔ partial ↔ paid`) derived from the new `settled_amount`. All four
write paths (issue, invoice void, payment create+allocate, payment void)
are flush-only cores with committing router wrappers whose try/except
wraps the core call (fix-#2 pattern).

DB backstops — the status↔amount invariant is exhaustive over the enum
(one CHECK, every state enumerated, no "settled at implementation" gap;
verified by direct-SQL bypass tests):

```
CHECK (settled_amount >= 0 AND settled_amount <= total)                    -- invoices
CHECK (   (status = 'OPEN'    AND settled_amount = 0)
       OR (status = 'PARTIAL' AND settled_amount > 0 AND settled_amount < total)
       OR (status = 'PAID'    AND settled_amount = total AND total > 0)
       OR (status = 'VOIDED'  AND settled_amount = 0))
CHECK (allocated_amount >= 0 AND allocated_amount <= amount)               -- payments
CHECK (status != 'VOIDED' OR allocated_amount = 0)
CHECK ((status = 'VOIDED') = (voided_at IS NOT NULL))                      -- both tables
```

Timestamps are `timestamptz` (UTC) as everywhere else; a void event's
`event_date` is the UTC date of `voided_at` — same convention
`shipped_at.date()` already established in `ledger.posting`.

A reconciliation test asserts `settled_amount == SUM(allocations)` and
`allocated_amount == SUM(allocations)` after every test scenario touching
payments — same discipline as inventory's summary test. And the doctrine's
repair path ships too (R6): `python -m app.cli.rebuild_ar_balances`,
mirroring `rebuild_stock_summary`'s R2 discipline — per company, ONE
transaction, `SELECT ... FOR UPDATE` on ALL of that company's payment and
invoice rows first (same payment-then-invoice order), then recompute both
columns and re-derive non-voided invoice statuses from
`SUM(payment_allocations)`, with the same "blocks writers while running"
warning.

**Money inputs (R8)**: every client-supplied amount (`payments.amount`,
allocation `amount`, and any money field on invoice creation) goes through
the established Pydantic round-half-even-to-6dp validator
(`ledger.schemas.JournalLineCreate`'s pattern) and is bounds-checked
**after** rounding — `gt=0` where zero is meaningless (payment and
allocation amounts), so a positive sub-microunit input that rounds to zero
is a 422, not a zero-amount row.

**Option B: compute balances on read (`SUM` over allocations).** No
maintained state — but every allocation-capacity check, status derivation,
aging query, and credit-exposure query pays a join+aggregate, and the
concurrency story ("two allocations race the same invoice") degenerates to
the TOCTOU-shaped check-then-insert this project has rejected three times
(ADR-003/007). Rejected.

### Decision 4 — invoice corrections

**Option A: void (only while unsettled), rule-driven contra posting** —
chosen

`POST /invoices/{id}/void`: R1 doctrine applies for real here (unlike
issue) — `SELECT ... FOR UPDATE` on the invoice row, re-check
`status IN (open)` and `settled_amount = 0` under the lock (409
otherwise — a concurrent allocation and a void serialize on the row lock),
set `voided`/`voided_at`, publish `receivables.invoice_voided` → posting
rule Dr `4000` / Cr `1100`, `source_id` = invoice id. Using a posting rule
rather than `ledger`'s manual `/journal-entries/{id}/reverse` keeps the
correction idempotent-by-source and replayable like every other
event-sourced entry, and honors the Week 5 hard rule that **reversal rows
never carry `source_type`** — this contra entry is not a reversal row, it
is a first-class event posting. The partial unique index (Decision 1)
then permits re-issuing the order. `POST /payments/{id}/void` (R2) is the
same mechanism with the accounts swapped (Dr `1100` / Cr `1000`) and
`allocated_amount = 0` as its precondition — one correction doctrine, two
document types.

Voiding a partially-settled invoice requires un-allocation, which Phase 1
does not ship (deferred with credit notes/refunds, Phase 2+ — documented
loudly: the operational answer today is "apply the payment elsewhere is
impossible once allocated; allocate carefully"). This is the honest
minimal correction path, not a half-built credit-note engine.

**Option B: credit notes now.** The real mechanism eventually — needs its
own document type, numbering, application-to-invoice semantics, and
posting rules; nothing in the Phase 1 demo needs it. Deferred.
**Option C: no correction path at all.** A fat-fingered invoice would be
permanent in a system whose ledger is append-only — operationally
unacceptable even for a demo. Rejected.

### Decision 5 — AR aging

**Option A: current-state aging bucketed by days past `due_date`;
unapplied credits alongside; control accounts protected so the tie-out is
actually true** — chosen

`GET /reports/ar-aging?bucket_date=YYYY-MM-DD` (default today),
receivables-owned (each module owns its reports, as ledger owns trial
balance). **This is a CURRENT-STATE report (R4)**: it reads today's
`settled_amount`/`allocated_amount` and today's set of open invoices;
`bucket_date` only moves the boundary that classifies days-past-due (a
collections what-if), it is NOT a historical as-of cutoff — a
`bucket_date` in the past does not exclude later invoices or un-apply
later payments. Historical (fact-time) aging needs event-sourced balance
reconstruction and is deferred to the Phase 3 reporting layer; the
parameter is named `bucket_date` precisely so nobody mistakes it for
`as_of` semantics the endpoint doesn't have.

**Report population, defined precisely (R15 — resolves the P2 that an
invoice-anchored query silently drops payment-only customers, breaking
the tie-out)**: the row set is the **union** of (a) customers with at
least one non-voided invoice where `total - settled_amount > 0` and (b)
customers with `Σ (amount - allocated_amount) > 0` over non-voided
payments — not a query that starts from invoices and only incidentally
picks up customers who happen to have one. Per customer: open balance per
bucket — `current` (not yet due), `1–30`, `31–60`, `61–90`, `90+` days
past due — from that customer's qualifying invoices (zero buckets, all
zero, for a customer with no open invoices), an `unapplied_credits`
column from (b), and a net total that **may be negative** (a customer who
is all credit, no open invoice, nets negative — correct, not an error
state).

**Control-account protection (R5, hardened by R11)** — the tie-out claim
is only honest if receivables' events are the *sole* writers to `1100`.
Two enforcement pieces:

1. **`source_type`/`source_id` become server-only (R11)**: the public
   `POST /journal-entries` schema stops accepting them (they are stripped
   from `JournalEntryCreate`'s router-facing model; internal posting
   paths pass them via the service-layer call). This closes the bypass
   where a client claims a fake `source_type` to dodge any
   "manual means NULL" check — and closes a latent Week 3 gap in the same
   move: a client-supplied `(source_type, source_id)` could previously
   collide with a real event's idempotency key and silently block its
   posting. Manual entries are now *by construction* `source_type IS
   NULL`.
2. **Control designation that works for every company (R11)**:
   `accounts.is_control BOOLEAN NOT NULL DEFAULT false` (additive) for
   user-declared control accounts, AND a kernel-owned list of control
   *codes* (`1100`; lives next to the standard-chart doc in
   `ledger.posting`) — the manual-entry path rejects a line if its
   account `is_control` **or** its code is in the kernel list, so a newly
   created company whose chart was seeded without the flag is still
   protected. Rule-driven postings are unaffected.
3. **The manual-reversal endpoint gets the same rejection (R16 —
   resolves Codex v3's finding that `POST /journal-entries/{id}/reverse`
   bypassed both R11 checks entirely)**: `_post_reversal` builds its
   lines by copying the original entry's lines, so it never passes
   through `JournalEntryCreate`'s validation — `reverse_journal_entry`
   now runs the same is-control-or-kernel-code check against the
   original entry's line accounts *before* constructing the reversal, and
   422s if any line touches a control account. This is a deliberate,
   documented narrowing of what `reverse` can undo: control-account
   entries (which today means every `receivables`-posted entry, since
   `1100` is the only control account) are corrected exclusively through
   receivables' own void-then-contra path (Decision 4), never through
   ledger's generic reversal. Event-sourced non-control entries (e.g.
   `sales.goods_shipped`'s COGS/Inventory pair) are unaffected — they
   touch no control account and keep reversing exactly as before.

Grand-total property (Hypothesis-tested alongside the existing
trial-balance invariants): **Σ open invoice balances − Σ unapplied
credits = balance of account `1100`** (evaluated at current state) after
any sequence of issue/void/pay/allocate/payment-void operations — the
subledger provably ties to the control account, which is the whole point
of having both.

**Option B: bucket by invoice_date.** Loses the only business question
aging answers (what is *overdue*?); `due_date` exists precisely for this.
Rejected.
**Option C: true historical as-of aging now.** Requires reconstructing
allocation state at an arbitrary past date (event-sourcing the subledger
or snapshotting balances) for a report nothing in Phase 1 consumes
historically. Rejected as premature; revisit with the Phase 3 read layer.

### Decision 6 — credit-exposure formula (ADR-006 revisit)

**Option A: uninvoiced CONFIRMED/SHIPPED order totals + open invoice
balances; unapplied credits do not offset** — chosen

```
exposure = Σ total       over orders  status IN (CONFIRMED, SHIPPED)
                                      AND NOT EXISTS (non-voided invoice for order)
         + Σ (total - settled_amount) over non-voided invoices
         + total of the order now confirming
```

This fixes a real Week 5 gap: today a `SHIPPED` order silently leaves the
exposure sum (the plugin filters `status == CONFIRMED`) even though no
cash arrived. Under the new formula an order counts exactly once through
its life — as an order until invoiced, as AR until paid — and drops out
only as cash is applied. Unapplied credits deliberately don't reduce
exposure (conservative: on-account cash a customer could reclaim is not
collateral; revisit with Option C of Decision 2 in Phase 2). The plugin
already serializes on the customer row lock (ADR-006), which covers the
new terms too; it may import receivables models directly — plugins are
exempt from the independence contract (ADR-006 Decision 2).

**Option B: leave the formula alone.** Ships a credit check that a
shipment weakens — worse than Week 4's version. Rejected; ADR-006
explicitly scheduled this revisit.

## Cross-module mechanics (established patterns, stated for completeness)

- `receivables.service` reads `sales_orders` and `customers` via Core
  `table()` references **with explicit `company_id` filters** (the
  trial-balance lesson — every Core-table predicate carries it).
- Event payload schemas (R1) — normative, complete field lists; every
  field required, no extras. All four schemas live in `ledger.posting`
  next to their rules (the `GoodsShippedPayload` precedent); receivables
  publishes shape-matched dicts. `source_id` feeds the
  `uq_journal_entries_source` idempotency key; `event_date` is the field
  `ledger.posting`'s entry-date resolution already recognizes, set to
  `invoice_date` / `received_at.date()` / the void date respectively:

  ```
  InvoiceIssuedPayload   {company_id: UUID, source_id: UUID (=invoice id),
                          event_date: date, invoice_no: str, order_id: UUID,
                          customer_id: UUID, total: Decimal}
  InvoiceVoidedPayload   {company_id: UUID, source_id: UUID (=invoice id),
                          event_date: date, invoice_no: str, total: Decimal}
  PaymentReceivedPayload {company_id: UUID, source_id: UUID (=payment id),
                          event_date: date, payment_no: str,
                          customer_id: UUID, amount: Decimal}
  PaymentVoidedPayload   {company_id: UUID, source_id: UUID (=payment id),
                          event_date: date, payment_no: str, amount: Decimal}
  ```

  Note the void events' `source_id` equals the original document's id —
  safe because the idempotency key includes `source_type` (the event_type
  string), so issue and void of the same invoice are distinct keys, while
  replaying either stays a no-op. Replay tests prove one posting per
  event and that entry dates come from `event_date`, not `today()`.
- New standard-chart code: `1000 Cash` joins the documented list in
  `ledger.posting` (1100/4000 were reserved there since Week 3); `1100`
  is additionally marked `is_control` (R5).
- All service entry points (issue, invoice void, payment
  create+allocate, late allocation, payment void) are flush-only cores
  with committing router wrappers whose try/except wraps the core call
  (fix-#2 pattern).
- No new hook points: ADR-006 lets modules add them by naming them, but no
  consumer exists yet — deferred until one does (same restraint as
  ADR-007), documented in the module README.
- No new `sales_order_status` enum value (`closed` stays deferred — it
  would both trip the one-transaction enum quirk and require sales to
  subscribe to receivables events; nothing in Phase 1 needs it).
- Migration 0008 creates only new objects (two enums are *new types*, safe
  to reference in the same migration — the enum quirk applies only to
  values ADDed to existing types) plus the additive
  `customers.payment_terms_days`.

## Trade-off Analysis

The week's centerpiece is closure: the subledger-ties-to-control-account
property turns the whole O2C chain into one self-verifying demo — trial
balance balances AND aging nets to AR, after arbitrary operation
sequences. Everything here is established doctrine applied to a new
domain; the one new mechanism (maintained balance columns) is §10.5's
pattern, not an invention. Prices paid, consciously: header-only invoices
(lines are additive later), receipt-time AR relief instead of
allocation-time (orthodox deposits accounting deferred), void-only
corrections (credit notes deferred), no tax (Phase 4's moat, not Phase
1's scope). Each deferral is documented where an implementer will trip on
it.

## Consequences

- Easier: Phase 1's DoD demo is complete end-to-end; Week 7's E2E/seed
  work has every operation it needs; Phase 3 `payables` is this module
  mirrored (same shapes, opposite signs).
- Harder: masterdata grows two module-driven columns
  (`customers.payment_terms_days`, `accounts.is_control`); `sales_orders`
  grows `shipped_at` (R13); and ledger's manual-entry AND manual-reversal
  paths both gain a control-account rejection (R5, R16) — the first time
  a later module's needs reach back into ledger validation twice; the
  credit plugin now couples to two modules' models (accepted plugin
  trade-off, ADR-006); allocation lock ordering (payment → invoices by
  id), the `external_ref` requirement, and the allocation-command
  fingerprint contract are rules implementers and API
  clients must actually follow.
- Revisit: credit notes + un-allocation (Phase 2), deposits-account
  posting model (Phase 2), allocation events for integrations (Phase 2),
  invoice lines + tax (Phase 4 e-invoice), `closed` order status (when
  something needs it).

## Consensus Revisions (review v1, 2026-08-15 — real `codex` CLI, REJECTED → revised)

The first-pass review found 4 P1 + 5 P2; all nine are resolved below and
folded into the decision text above.

**R1 — Normative payload schemas (resolves P1: payload field lists were
ambiguous about `source_id`/`event_date`).** All four schemas spelled out
in "Cross-module mechanics" with every required field; replay tests must
prove one posting per event and `event_date`-derived entry dates.

**R2 — Payment retry safety and correction path (resolves P1: retried
`POST /payments` double-posts Cash/AR; payments had no correction path).**
Required `external_ref` + `UNIQUE (company_id, external_ref)` makes
payment creation idempotent per client reference (409 identifies the
existing payment); `POST /payments/{id}/void` (legal only while
unallocated, R1-locked) publishes `receivables.payment_voided` → contra
Dr `1100` / Cr `1000`. Folded into Decisions 2 and 4.

**R3 — (merged into R1's schema statement; number kept so decision-text
references R4–R9 stay stable.)**

**R4 — Aging is explicitly current-state (resolves P1: `as_of` implied a
historical cutoff the computation cannot honor).** Parameter renamed
`bucket_date`, defined as bucket-boundary-only; historical fact-time
aging deferred to Phase 3's read layer (new Option C records the
rejection). Tie-out property evaluated at current state.

**R5 — Control-account protection (resolves P1: manual entries to `1100`
falsify the tie-out).** `accounts.is_control` (additive); ledger's
manual-entry path (`source_type IS NULL`, manual reversals included)
rejects control-account lines with 422; rule-driven postings unaffected;
`1100` seeded as control. The tie-out property is now enforced, not
aspirational.

**R6 — Rebuild CLI ships (resolves P2: doctrine says projections are
rebuildable — operationally, not just in principle).**
`app/cli/rebuild_ar_balances.py`, mirroring `rebuild_stock_summary` R2:
per company, one transaction, lock-all-then-recompute (payment rows, then
invoice rows by id), re-derives non-voided invoice statuses, warns about
blocking writers.

**R7 — Allocation command contract (resolves P2: batch atomicity,
duplicate targets, and wrapper boundaries were unspecified).** Batch is
atomic; duplicate invoice targets in one request are 422; one flush-only
`allocate_payment` core serves both endpoints; all write paths use the
committing-wrapper fix-#2 pattern. Folded into Decision 3.

**R8 — Post-rounding money validation (resolves P2).** All client money
inputs use the established round-half-even-to-6dp validator with bounds
checked after rounding; `gt=0` for payment/allocation amounts. Folded
into Decision 3.

**R9 — Payment-terms-at-issue is a documented snapshot-doctrine exception
(resolves P2).** Terms are billing-time attributes; the invoice's stored
`due_date` is the frozen outcome; model docstring carries the note.
Plus (review recommendations, accepted): `CHECK (due_date >=
invoice_date)`, `CHECK (payment_terms_days BETWEEN 0 AND 365)`, and a
from-empty migration test for 0008.

## Consensus Revisions (review v2, 2026-08-15 — real `codex` CLI, REJECTED → revised)

v2 confirmed R1/R4/R6/R8/R9 adequate, R2 adequate for creation/void, and
found three remaining P1s — all verified against the code and accepted:

**R10 — Allocation-command idempotency (resolves P1: a timed-out
`POST /payments/{id}/allocations` retry double-settles AR).** Every
allocation row records its command's `request_ref`
(`UNIQUE (company_id, payment_id, request_ref, invoice_id)`); the batch's
atomicity turns any retry conflict into a whole-command 409 identifying
the already-applied command. Inline batches inherit the payment's
`external_ref`. Folded into Decision 3's command contract.

**R11 — `source_type`/`source_id` are server-only, and control
designation cannot be skipped by seeding (resolves P1: client-supplied
`source_type` bypassed the "manual = NULL" check; `is_control DEFAULT
false` alone left unseeded charts unprotected).** Public
`POST /journal-entries` no longer accepts source fields (closing, in the
same move, the latent Week 3 risk of a client colliding with a real
event's idempotency key); the manual-entry rejection checks `is_control`
OR a kernel-owned control-code list (`1100`) in `ledger.posting`. Folded
into Decision 5.

**R12 — TWD-only is enforced, not assumed (resolves P1: unrestricted
`customers.currency_code` lets a USD order reach invoicing and post its
raw number as TWD).** `CustomerCreate/Update.currency_code` rejects
non-TWD in Phase 1 (the established fix-#4 `CompanyCreate` pattern, same
error wording); defense in depth: `create_invoice` and `create_payment`
422 on any non-TWD source order/customer currency; cross-currency
rejection tests. No data migration needed (no deployed data), noted in
the migration docstring.

Accepted v2 recommendations: `CHECK ((status = 'VOIDED') = (voided_at IS
NOT NULL))` on both document tables; void `event_date` = UTC date of
`voided_at` (stated in Decision 3).

## Consensus Revisions (review v3, 2026-08-15 — real `codex` CLI, REJECTED → revised)

v3 confirmed the v2 resolutions adequate and found three more P1s + one
P2 — all verified against the code and accepted:

**R13 — `sales_orders.shipped_at` persisted; invoice date cannot predate
shipment (resolves P1: Decision 1 claimed "revenue recognized at/after
delivery" but nothing enforced it, and the order row had no shipment
timestamp to enforce it against).** `ship_order` now persists
`shipped_at`; `invoices.order_shipped_at` snapshots it at issue (a `CHECK`
can't reach across tables); `CHECK (invoice_date >=
order_shipped_at::date)`. Folded into Decision 1. **(R13 itself was
corrected in v4 — see R17: the column can't be `NOT NULL`.)**

**R14 — Allocation-command idempotency, correctly scoped (resolves P1:
Codex v3 found R10's row-level unique constraint didn't actually make
`request_ref` unique per *command* — the same reference with a different
invoice set still succeeded, double-settling AR).** Replaces R10 with a
`payment_allocation_commands` header table, `UNIQUE (company_id,
payment_id, request_ref)`, carrying a `request_fingerprint`: an exact
retry (same ref, same body) 200s idempotently off the stored fingerprint;
a reused ref with a different body 409s as the contract violation it
always should have been. Folded into Decision 3.

**R15 — Aging report population defined as a union, not an
invoice-anchored query (resolves P2: a customer with only unapplied
credit and no open invoice would silently vanish from the report,
breaking the tie-out for exactly the customers who most need to appear
in it).** Population = customers with a qualifying open invoice **or**
nonzero unapplied credit; net total may be negative. Folded into
Decision 5.

**R16 — Manual reversal is blocked on control-account entries (resolves
P1: `POST /journal-entries/{id}/reverse` built its lines by copying the
original entry directly, bypassing both R11 checks — a user could still
reverse an AR posting and break the tie-out through the one door R11
didn't watch).** `reverse_journal_entry` now runs the same is-control-
or-kernel-code check against the *original* entry's accounts before
building the reversal; control-account entries can only be corrected
through receivables' void-then-contra path. Folded into Decision 5.

Accepted v3 recommendation: `ledger.posting`'s internal posting call site
and the public `POST /journal-entries` DTO are explicitly two different
Pydantic models now (R11's server-only source fields apply to the public
one only; `ledger.posting` continues constructing entries with real
`source_type`/`source_id` via the internal path) — stated once here so
R11's implementation doesn't accidentally strip source fields from the
posting engine itself.

## Consensus Revisions (review v4, 2026-08-15 — real `codex` CLI, REJECTED → revised)

v4 confirmed R14/R15/R16 adequate and found R13 itself was not
implementable as written — verified against the code and accepted:

**R17 — `shipped_at` is nullable with a state-conditional CHECK, plus a
data backfill for pre-existing shipped rows (resolves P1: R13 specified
`NOT NULL` with no default, which fails for every draft/confirmed/
cancelled order — only `SHIPPED` orders ever have a shipment moment, and
migration 0008 runs against a database that may already contain shipped
orders from before this ADR).** Column is nullable;
`CHECK (status::text != 'SHIPPED' OR shipped_at IS NOT NULL)` (text-cast
per the migration-0006 discipline, since `SHIPPED` was itself added by
migration 0005 and this project's `upgrade head` is one transaction).
Migration 0008 backfills existing `SHIPPED` rows' `shipped_at` from their
own `sales.goods_shipped` outbox payload (the value was always there,
just never persisted on the order) — recoverable with certainty because
exactly one such event exists per shipped order (idempotent-posting
invariant). A populated-upgrade test (seed a pre-0008-shaped shipped
order, run 0008, assert `shipped_at` backfilled correctly) joins the
from-empty test. Folds into and supersedes R13's `NOT NULL` claim;
Decision 1 and the invoice-date CHECK are otherwise unchanged.

**v5 accepted recommendation**: before applying the state-conditional
CHECK, migration 0008's backfill step explicitly asserts every `SHIPPED`
order has exactly one matching `sales.goods_shipped` outbox payload and
raises a clear, named migration error (not a bare CHECK-violation
traceback) if any row doesn't — the idempotent-posting invariant makes
"exactly one" the expected count, so this is a diagnostic for an
already-impossible-in-theory state, not new logic.

## Action Items

1. [x] `/CODEX REVIEW ARCHITECTURE` — v1 REJECTED → R1–R9; v2 REJECTED → R10–R12; v3 REJECTED → R13–R16; v4 REJECTED → R17; **v5 APPROVED 2026-08-15 (Consensus Status: APPROVED)**, one non-blocking recommendation accepted (backfill diagnostic, folded into R17's text above)
2. [ ] Migration 0008: `sales_orders.shipped_at` (nullable, state-conditional CHECK, backfilled for existing SHIPPED rows from outbox), `invoices` (+ status enum, exhaustive status CHECK, `uq_invoices_order_live`, `order_shipped_at`, `invoice_date >= order_shipped_at`, `due_date >= invoice_date`), `payments` (+ status enum, `external_ref` unique, CHECKs), `payment_allocation_commands` (+ `request_ref` unique per payment), `payment_allocations` (`command_id` FK), `receivables_sequences`, `customers.payment_terms_days` (bounded), `accounts.is_control`, voided_at↔status CHECKs; from-empty AND populated-upgrade tests
3. [ ] `receivables` module: models/schemas/service/router — issue (with shipped_at snapshot), invoice void, payments (idempotent by `external_ref`), payment void, allocations (batch-atomic, idempotent-with-fingerprint by `request_ref`), aging report (`bucket_date`, union population); TWD-only defense-in-depth checks
4. [ ] Posting: four payload schemas + rules in `ledger.posting` (`1000 Cash` documented, `1100` in kernel control-code list), registration in `app.main`; ledger manual-entry AND manual-reversal control-account rejection; split public journal-entry DTO (no source fields) from the internal posting call site (keeps source fields)
4b. [ ] Masterdata: `CustomerCreate/Update.currency_code` TWD-only validator (fix-#4 pattern)
5. [ ] `app/cli/rebuild_ar_balances.py` + reconciliation test
6. [ ] Credit-limit plugin: new exposure formula + updated tests
7. [ ] Tests: issue happy path (AR/Revenue posted, trial balance moves), issue from non-shipped 422, invoice_date-before-shipment 422, double-invoice race (one 409), invoice void → contra entry → re-issue, void-vs-allocate race, payment retry via duplicate `external_ref` (one payment, one entry, 409), payment void → contra + drops from unapplied credits, payment + inline/late allocations, over-allocation both directions (409), duplicate-target 422, cross-customer/voided-target/voided-payment allocation rejection, concurrent allocations same invoice (capacity respected), rounding boundary cases (sub-microunit → 422), manual journal entry to `1100` rejected (422), manual reversal of a control-account entry rejected (422), aging buckets + unapplied credits + `bucket_date` semantics + payment-only-customer appears with negative net, **control-account tie-out property test**, replay idempotency for all four events (incl. issue-vs-void key distinctness), exact-retry allocation command 200s idempotently / reused-ref-different-body 409s, public journal-entry API cannot set `source_type`/`source_id`, non-TWD customer 422 + non-TWD order cannot invoice (defense-in-depth), reconciliation (maintained columns == SUM), rebuild CLI correctness under concurrency, cross-company isolation, **`shipped_at` backfill correctness on a populated database**
