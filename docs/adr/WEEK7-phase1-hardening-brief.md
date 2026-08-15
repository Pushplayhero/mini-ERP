# Week 7 — Phase 1 hardening & demo-readiness brief (for architecture consensus review)

Status: ACCEPTED — Codex architecture consensus v3 APPROVED (2026-08-15).
Implementation may begin, per the Decision 6 sequencing.
Author: Claude (architect)
Date: 2026-08-15
Scope owner: Ryan

Consensus history: v1 → NEEDS REVISION (2 P1, 5 P2, 1 P3). v2 → NEEDS
REVISION (7 of 8 resolved; seed idempotency across the full O2C chain still
under-specified + 1 new P2 on stock-reconciliation over-correction). v3 →
**APPROVED, no new findings** (both seed-idempotency points closed; Codex
verified the stock rule converges in all three cases: first run D+Q−Q=D,
full rerun D+0=D, partial D+R−R=D). All findings across all rounds were
verified against the actual repo before acceptance — see "Consensus
Revisions" at the bottom for the finding-by-finding record.

This is an **implementation brief**, not a full ADR. Week 7 is Phase 1
*hardening* (tests, seed data, docs, one narrow perf change), not new
domain architecture — so it does not introduce a new correctness-critical
invariant of its own. The one exception (Decision 3, ar-aging SQL rewrite)
touches an existing correctness invariant and is called out as such. Two
genuinely-foundational decisions this week retroactively *document*
(Decisions 5a/5b: ADR-001/002) are the ones that become permanent ADRs.

---

## 0. Context / where Phase 1 stands

Weeks 1–6 are committed (`HEAD = ab0e407`). All five Phase 1 kernel modules
exist and passed real Codex review: `masterdata`, `ledger`, `sales`,
`inventory`, `receivables`. Full local check sequence green: `ruff`,
`ruff format`, `mypy` (app clean), `lint-imports` (5/5 contracts), `pytest`
(212 tests, real PostgreSQL, migration chain 0001→0008).

The master plan (`docs/mini-erp-architecture.md` §7) defines Week 7 as
"E2E 測試、seed demo data、效能小調（報表 query）" and Week 8 as
"README 打磨、ADR、mermaid 架構圖、demo GIF、v0.1.0 release tag". This brief
covers Week 7 and pulls **two Week 8 items forward** (README rewrite,
ADR-001/002) with an explicit justification — see Decision 6.

### Verified current-state facts (checked against the repo, not assumed)

- **No full-chain O2C E2E test exists.** All 212 tests are single-module /
  single-API. `tests/test_smoke.py` is only a `/health` check. No single
  test walks `create order → confirm → ship → invoice → pay/allocate →
  trial balance balances`.
- **The hypothesis property test (`tests/ledger/test_property_trial_balance.py`)
  drives raw balanced journal entries directly** via
  `ledger_service.create_journal_entry`, NOT domain events through the
  posting engine. So "any valid *O2C event sequence* keeps the trial
  balance balanced" is currently **unproven** — only "any set of balanced
  raw entries" is.
- **`get_trial_balance` is already fully SQL-aggregated** (`func.sum` +
  `group_by`, one statement). It is NOT a perf concern.
- **`get_ar_aging` is the only report that aggregates in Python**: after
  Week 6's fix it issues one `UNION ALL` statement, but still buckets rows
  by days-past-due in a Python loop. This is the only "report query" perf
  candidate.
- **No seed-data mechanism and no Makefile.** `docker-compose.yml` runs
  only `alembic upgrade head && uvicorn`. `app/cli/` has three rebuild/
  replay scripts but no `seed_demo`.
- **`README.md` is stale**: it still declares "This repository state =
  Phase 1 / Week 1" and lists `sales`/`inventory`/`receivables`/`ledger`
  as "Non-Goals (this week) … empty module shells". This actively
  misrepresents a repo that has all five modules done.
- **ADR-001 and ADR-002 are missing.** `docs/adr/` starts at ADR-003.
  `docs/mini-erp-architecture.md` §6 explicitly commits to "ADR 至少寫四篇
  （面試官真的會看）" naming ADR-001 (modular monolith vs microservices)
  and ADR-002 (append-only inventory ledger).

---

## 1. Goal & definition of done

Bring Phase 1 to **"demo-ready and correctness-proven"**: a newcomer can
`docker compose up`, seed a realistic dataset, run a one-command O2C demo
that ends in a balanced trial balance, and read an accurate README that
links design decisions to ADRs — and the "any event sequence balances"
correctness claim is backed by an actual test.

Week 7 DoD (this brief):
1. One full O2C E2E test, green, asserting per-step balance deltas and the
   non-zero AR/1100 tie-out (Decision 1).
2. Property test driving **domain events** (not just raw entries), proving
   trial-balance balance + AR/1100 tie-out + `on_hand >= 0`, with the
   isolated-per-example harness (Decision 2). NB: this DoD line is binding
   — if the harness proves unreliable it is *formally deferred* to Week 8 in
   a committed brief edit, not silently dropped (Codex v1 P1).
3. `seed_demo` CLI (fresh-DB-safe bootstrap + idempotent) + `demo_o2c`
   runner + `Makefile` (`make up/seed/demo/test/check`) + docker-compose
   opt-in seed path; a run-twice seed idempotency test (Decision 4).
4. ar-aging bucketing pushed into SQL, tie-out property preserved.
5. README rewritten to reflect Phase 1 complete (incl. mermaid C4).
6. ADR-001 + ADR-002 written.

Non-DoD (deferred to Week 8 or later): demo GIF recording, `v0.1.0` tag,
public demo host, coverage badge, any Phase 2 work.

---

## 2. Decisions

### Decision 1 — Full O2C E2E test (`tests/e2e/test_o2c_end_to_end.py`)

One test function walking the entire order-to-cash line against a real
PostgreSQL, asserting the accounting invariants *at each step*, not just at
the end:

1. Set up company + open period + standard chart (1000/1100/1300/4000/5000)
   + customer + product + seeded stock (reuse existing `tests/*/_helpers`).
2. Create order → confirm → **assert NO ledger delta** and no stock delta
   (a bare "trial balance still balances" is too weak — a balanced-but-wrong
   entry would pass it; Codex v1 P2). Also exercise the credit-limit path.
3. Ship → **assert the only journal delta is Dr 5000 / Cr 1300** for the
   expected COGS amount, stock deducted by the shipped qty, nothing else
   moved.
4. Issue invoice → **assert the only journal delta is Dr 1100 / Cr 4000**
   for the invoice `total` (which matches the order total); **assert the
   non-zero tie-out here**: ar-aging net for the customer == ledger 1100
   balance == invoice total (this is the meaningful tie-out; the final
   fully-paid `0 == 0` is a weak check on its own — Codex v1 P2).
5. Receive payment → **assert the only journal delta is Dr 1000 / Cr 1100**;
   then allocate → **assert NO journal delta** (allocation is subledger-only,
   ADR-008 Decision 2), invoice `settled_amount` updated, status → PAID.
6. **Final assertions**: trial balance balanced (Σdebit == Σcredit) and the
   final ar-aging/1100 tie-out (now 0) — retained as a whole-flow sanity
   check, but the *load-bearing* tie-out assertion is the non-zero one at
   step 4.

**Assertion mechanism** (Codex v1 P2): snapshot per-account debit/credit
balances (via the trial-balance query) before and after each step and
assert the *delta set* — i.e. exactly which accounts moved and by how much
— rather than only re-checking the global balance identity. A small helper
`_account_balances(client, company_id) -> dict[code, Decimal]` makes each
step's assertion a dict-diff.

Rationale: this is the single most narratively-valuable test in the repo —
it turns the résumé claim ("order-to-cash 全流程，宣告式過帳引擎把領域事件
轉成複式簿記") into one green, readable proof. It lives in a new
`tests/e2e/` package so it's discoverable as *the* end-to-end demonstration.

Placement note: `tests/e2e/` will need an `__init__.py`; its helpers come
from the existing per-module `_helpers.py` (cross-importing test helpers is
already an established pattern — `tests/receivables/_helpers.py` imports
from `tests/sales/_helpers.py`).

### Decision 2 — Property test over domain-event sequences

Extend the hypothesis approach: generate a random-but-valid *sequence of
O2C operations*, then assert these invariants hold no matter the ordering:
- Trial balance balances (Σdebit == Σcredit).
- Σ(open invoice balances) − Σ(unapplied credits) == ledger 1100 balance
  (the control-account tie-out).
- **`on_hand >= 0` for every product** (Codex v1 P3 / master plan §6: the
  plan promises non-negative inventory under any valid event sequence too —
  it's a near-free assertion on the same trace, so include it rather than
  narrow the claim).

**Harness specification** (Codex v1 P1 — the existing property test's
pattern is NOT safe to copy): `tests/ledger/test_property_trial_balance.py`
sets up ONE company once and *accumulates* DB state across Hypothesis
examples (a running `cumulative_expected` total). Copying that into a
multi-module domain state machine would make failures order-dependent and
Hypothesis shrinking unreliable (a shrink would run against dirty state
from a prior example). This harness must instead:
- Draw a **pure operation plan** first (a list of typed ops:
  `NewOrder(qty)`, `Confirm(i)`, `Ship(i)`, `Invoice(i)`, `Pay(i, amount)`,
  `Allocate(i, invoice, amount)`), with each op only enabled from its legal
  predecessor state — so the plan is *always executable*, no "illegal
  transition" noise.
- Execute each Hypothesis example in a **fresh company** with fresh domain
  objects (new chart, period, customer, product, stock), so no example sees
  another's state.
- Create and dispose the async engine **within that example's own event
  loop** (same `asyncio.run()`-per-example, sync-test structure the existing
  property test already uses for loop isolation), with `deadline=None` and
  the function-scoped-fixture health check suppressed.
- Load `app.main`'s composition-root wiring (event schemas + subscribers)
  before running, or every posting raises `UnknownEventTypeError`.
- Guarantee **at least one non-zero posting event** in every generated plan
  (a plan that only creates drafts proves nothing) — e.g. require ≥1 ship
  or ≥1 payment.

New file `tests/e2e/test_property_o2c_balances.py`; the existing ledger
property test is unchanged (it keeps proving the narrower raw-entry
invariant).

**DoD status** (Codex v1 P1, second half): a DoD item cannot simultaneously
be "droppable". So Decision 2 is **either fully in Week 7 with the harness
above, or formally deferred to Week 8** — decided up front (see O-1), not
silently dropped mid-week. Recommendation: keep it, scheduled LAST, and if
the harness cannot be made reliable, formally move it (and its DoD line) to
Week 8 in a committed brief edit rather than quietly abandoning it.

### Decision 3 — ar-aging SQL bucketing (the one correctness-adjacent change)

Rewrite `get_ar_aging` so days-past-due bucketing happens in SQL, instead
of the current Python loop over a `UNION ALL` result.

**Query shape** (Codex v1 P2 — the right shape, made explicit): keep the
Week 6 `UNION ALL` as an inner derived table (invoice rows carry a
`due_date`, credit rows carry `NULL`), then in the OUTER query do
**conditional aggregation grouped by `customer_id`**: one
`SUM(CASE WHEN kind='invoice' AND days_past_due <= 0 THEN amount ELSE 0 END)`
per bucket, plus `SUM(CASE WHEN kind='credit' THEN amount ELSE 0 END)` for
unapplied credits. This preserves credit rows *independently* of whether the
customer has any invoice (so a payment-only customer still produces a row —
that is exactly the R15 trap a naive `GROUP BY` *inner join* would fall
into). Use numeric zero / `COALESCE`, and keep returning `unapplied_credits`
as a POSITIVE number with the subtraction happening only in `net_total`
(unchanged output contract).

**This is the only Week 7 change touching an existing correctness
invariant** (the ADR-008 Decision-5 tie-out + R15 population). Constraints:
- The `UNION ALL` single-statement-snapshot property from Week 6's fix MUST
  be preserved (invoice balances and unapplied credits still read in one
  statement — now the same outer query, so trivially still one statement).
- R15 population (a payment-only customer appears with a negative net) MUST
  be preserved.
- The existing `tests/receivables/test_aging.py` tie-out, payment-only, and
  two-statement structural tests must stay **green and unchanged** — they
  are the regression guard.

**Characterization tests FIRST** (Codex v1 P2 — the existing tests only
exercise a 45-day bucket, so they are *not* a complete equivalence guard):
before touching the query, add boundary cases to `test_aging.py` covering
the exact bucket edges — 0/1 day (current vs 1-30), 30/31 (1-30 vs 31-60),
60/61, 90/91 (61-90 vs 90+) — plus a customer with multiple invoices
landing in one bucket, and a customer holding both open invoice balances
AND unapplied credit simultaneously. These must pass against the CURRENT
Python implementation first (proving they characterize existing behaviour),
then still pass after the SQL rewrite (proving equivalence).

Because this touches correctness, it is the slice most deserving of the
Codex diff review the user has mandated for every slice.

Open question O-2: is Decision 3 worth doing at all in Week 7? At Phase 1
data volumes the Python bucketing is not measurably slow, and the Week 6
`UNION ALL` already removed the only *correctness*-relevant perf issue (the
two-statement race). Doing it is a "we know how to push aggregation into
SQL" signal + removes an O(rows) Python loop; skipping it costs nothing
functional. Recommendation: **do it, but scope it tightly** (aging only;
trial-balance is already SQL) and treat it as optional if time-boxed.

### Decision 4 — Seed demo data + Makefile + compose path

- New `app/cli/seed_demo.py`. **Bootstrap order is load-bearing** (Codex v1
  P1 — a fresh migrated DB has EMPTY `currencies`/`uom`: migration 0001
  creates those tables but does not populate them; today only the test-only
  `_seed_reference_data` conftest fixture inserts `TWD`/`EA`). The CLI must
  therefore, in this exact order:
  1. Import `app.main` (the composition root) so event schemas + subscribers
     are installed — otherwise the first posting raises
     `UnknownEventTypeError`.
  2. Create `TWD` currency and `EA` UoM idempotently (get-or-create) — these
     are FK prerequisites for the company and products.
  3. Get-or-create the demo company; **bind `company_context` explicitly**
     (seed runs outside any HTTP request, so fail-closed tenancy will
     otherwise reject every write — same as the rebuild CLIs).
  4. Create the chart of accounts AND a **current open accounting period**
     (postings require an open period for the entry date — ADR-005 R4).
  5. Create master data (customers, products), seed stock, then walk the
     O2C scenarios.
- **Idempotency** (Codex v1 P2 + v2 P2 — "stable codes" alone is
  insufficient, AND recovery must be specified across the WHOLE O2C chain,
  not just the order). Key insight: **the modules already have per-document
  idempotency keys — the seed's job is just to feed them stable values and
  recover by lookup, never blind-create.** Per document type:
  - Currencies/UoM/company/customers/products/accounts: get-or-create by
    their natural unique key; on a code collision with *incompatible*
    attributes, fail loudly rather than silently diverge.
  - **Orders**: tag via `SalesOrder.custom_data` with a stable seed key
    (e.g. `{"seed_scenario": "s3-partial-paid"}`) — `CustomDataMixin`
    already exists — and on rerun *find* that order and resume it through
    whatever legal states remain, never create a duplicate.
  - **Invoices** (Codex v2 P2): recover by ORDER — before issuing, look up
    the existing non-voided invoice for the order; re-issuing blindly would
    hit `uq_invoices_order_live` (the one-live-invoice-per-order partial
    index) and 409. Never re-issue; find-or-issue.
  - **Payments** (Codex v2 P2): use a STABLE per-scenario `external_ref`
    (e.g. `"seed-s3-pay1"`). The module's own
    `uq_payments_company_external_ref` makes a repeat a clean idempotent
    outcome (recover the existing payment by `external_ref` — the very
    lookup Week 6's finding-3 fix added, `get_payment_by_external_ref`),
    not a duplicate posting.
  - **Allocations** (Codex v2 P2): use a STABLE per-scenario `request_ref`
    with an identical payload. The allocation-command header
    (`uq_payment_allocation_commands_payment_request_ref` + the stored
    `request_fingerprint`) already makes an exact retry replay idempotently
    (ADR-008 R14) — the seed relies on that existing mechanism rather than
    inventing its own.
  - **Stock** (Codex v2 P2 — the over-correction trap): do NOT reconcile to
    a fixed pre-scenario target before resuming, or an already-shipped
    scenario's consumed stock gets restored and then not re-consumed (it
    skips shipping), changing on-hand and failing the run-twice test.
    Instead reconcile **remaining-work-aware**: set pre-run on-hand to
    `desired_final_on_hand + Σ(qty still awaiting shipment across
    not-yet-shipped seed orders for that product)`, THEN resume scenarios,
    THEN assert the `desired_final_on_hand` target. On a fully-set-up rerun
    (nothing left to ship) the awaiting sum is 0, so it reconciles straight
    to the final target and nothing moves.
  - New test `tests/e2e/test_seed_idempotent.py`: run the seed twice
    against a real DB and assert unchanged row counts, on-hand stock,
    document states, journal `source_id`s, ar-aging, and trial balance.
- New `Makefile`: `make up` (compose up **and block until the app's
  `/health` is ready** — Codex v1 P2; a demo that races an unready server
  is the classic flake), `make seed` (run seed_demo *inside the migrated
  app container*, after readiness), `make demo`, `make test`, `make check`
  (ruff+mypy+lint-imports+pytest, the Phase 1 sequence).
- **Demo runner is an explicit script, not inline Makefile curl** (Codex v1
  P2 — multi-step curl with ID-extraction inlined in a Makefile is brittle
  and not cross-platform). New `app/cli/demo_o2c.py` (or a `scripts/demo.sh`
  + a Python fallback): walks one order through the O2C API and prints the
  resulting trial balance. It must define its rerun behaviour explicitly —
  **resume a single stable demo scenario** (not create a fresh order each
  run) so repeated `make demo` cannot exhaust seeded stock or trip the
  credit limit.
- `docker-compose.yml`: add an **opt-in** seed path (documented `make seed`
  after `up`, or a separate compose profile) — NOT auto-seed on every boot
  (that fights idempotency and surprises anyone with real data).

### Decision 5 — Documentation (README + ADR-001/002)

**5a. README full rewrite** to reflect Phase 1 complete:
- One-line positioning (per master plan §8).
- Quick start: `docker compose up` → `make seed` → `make demo`.
- A mermaid C4-ish diagram (container + module view) — inline in the
  README, no external tooling.
- "Design Decisions" section linking each ADR-003..008 + the new 001/002.
- Accurate Non-Goals (Phase 2+ scope: plugin loader, workflow, RBAC,
  custom fields, frontend) — replacing the current "this week" framing.
- The O2C demo shown as a curl transcript ending in a balanced trial
  balance.

**5b. ADR-001 (modular monolith vs microservices)** and **ADR-002
(append-only inventory ledger)** — the two foundational ADRs the
architecture doc promised. These *document already-made and already-shipped
decisions* (not new ones), matching how ADR-003..008 read. Written in the
same format, added to `docs/adr/README.md`'s index.

### Decision 6 — Sequencing & why two Week 8 items move forward

Recommended order (each slice = its own commit, each gets a Codex diff
review per the mandated cadence). **Revised per Codex v1 P2**: README must
NOT go first — a README-first commit would advertise `make seed`/`make
demo` before they exist, i.e. the commit would itself be untruthful (the
exact failure mode this whole week exists to fix). ADRs *may* lead (they
document already-shipped decisions and reference nothing unbuilt); the
README goes LAST, after the commands and transcript it documents are real:

1. **ADR-001 + ADR-002** (Decision 5b) — pure docs of already-made
   decisions, references nothing unbuilt, zero-risk lead-off.
2. **O2C E2E test** (Decision 1) — the technical core; also de-risks the
   demo runner (Decision 4 walks the same path).
3. **Seed + Makefile + demo runner** (Decision 4) — the demo-facing infra.
4. **ar-aging SQL** (Decision 3) — correctness-adjacent, isolated;
   characterization tests land first (see Decision 3).
5. **Property-over-events test** (Decision 2) — last; if its harness can't
   be made reliable, formally defer to Week 8 (not silently drop).
6. **README rewrite** (Decision 5a) — LAST, once every command and
   transcript it shows actually exists and is green.

Open question O-3: is moving ADR-001/002 + README (nominally Week 8) into
Week 7 acceptable? Recommendation (confirmed by Codex v1): yes — ADRs lead,
README trails the infra it documents.

---

## 3. Scope / non-scope

In scope: Decisions 1–6 above.

Explicitly OUT of scope (Week 8 or later): demo GIF, `v0.1.0` git tag,
public demo host, CI coverage badge wiring, any Phase 2 platform work
(plugin loader, custom fields, workflow, RBAC, webhooks), any new business
capability, any schema/migration change (Week 7 adds no migration — if a
decision here appears to need one, that is a signal to stop and re-scope).

Files expected to change:
- New: `tests/e2e/__init__.py`, `tests/e2e/test_o2c_end_to_end.py`,
  `tests/e2e/test_property_o2c_balances.py`, `tests/e2e/test_seed_idempotent.py`,
  `app/cli/seed_demo.py`, `app/cli/demo_o2c.py` (demo runner),
  `Makefile`, `docs/adr/ADR-001-modular-monolith.md`,
  `docs/adr/ADR-002-append-only-inventory.md`.
- Modified: `app/modules/receivables/service.py` (ar-aging only),
  `tests/receivables/test_aging.py` (boundary characterization cases —
  added before the rewrite), `README.md`, `docs/adr/README.md`,
  `docker-compose.yml` (seed path), possibly `pyproject.toml` (only if a
  make/CLI target needs a script entry).

Files that MUST NOT change: any module `models.py` (no schema churn), any
`alembic/versions/*` (no new migration), the ledger/sales/inventory posting
rules and service cores, the tenancy/events/hooks core.

## 4. Validation

Per-slice: `ruff check`, `ruff format --check`, `mypy` (touched files),
`lint-imports` (must stay 5/5), `pytest` (affected + full before commit),
then a **real Codex diff review** (`codex exec --sandbox read-only -m
<tier> -`) per the user-mandated every-slice cadence. Tier: `gpt-5.6-sol`
for Decision 3 (correctness-adjacent) and Decision 1/2 (the E2E/property
proofs); `gpt-5.6-terra` (routine) is acceptable for the pure-docs
(Decision 5) and seed/Makefile (Decision 4) slices.

Whole-week: the full 212-test suite (+ the new E2E/property tests) green
from a fresh DB; `make check` green; `docker compose up && make seed &&
make demo` produces a balanced trial balance by hand.

## 5. Risks

- **R-a (Decision 2 flakiness)**: hypothesis + real DB + a domain generator
  can shrink slowly or find non-bugs (e.g. a legal-but-awkward sequence the
  generator didn't constrain). Mitigation: small `max_examples`, a tightly
  constrained transition menu, no concurrency, and it is the last/droppable
  slice.
- **R-b (Decision 3 silently breaking tie-out)**: pushing bucketing to SQL
  could drop payment-only customers (R15) or change rounding. Mitigation:
  the existing aging tests are the regression guard and must pass
  unchanged; this slice gets the strictest review tier.
- **R-c (seed data colliding with tests)**: if `seed_demo` writes to the
  same DB a test run uses, or is not idempotent, re-runs corrupt state.
  Mitigation: idempotent get-or-create keyed on stable codes; seed targets
  a demo company only; never invoked by the test suite.
- **R-d (scope creep into Week 8)**: "while I'm in the README" → GIF, tag,
  badge. Mitigation: §3 non-scope is explicit; those are Week 8.

## 6. Open decisions — resolved by Codex consensus v1

- **O-1 (Decision 2 in Week 7 or deferred?)** — Keep it, scheduled LAST,
  with the isolated-per-example harness in Decision 2. It closes an explicit
  Phase 1 proof gap. But it is NOT "droppable while it remains in the DoD":
  if the harness can't be made reliable, formally move it (and its DoD line)
  to Week 8 in a committed edit, don't silently drop it.
- **O-2 (Decision 3 worth doing?)** — Yes, tightly scoped, and only AFTER
  the boundary characterization tests land. It requires no migration. E2E
  and seed remain higher priority since the perf benefit is presently
  unmeasured.
- **O-3 (pull ADR-001/002 + README into Week 7?)** — Yes, but README does
  NOT lead. ADRs may go first (they document shipped decisions); README goes
  last, after the commands/transcript it documents exist. (See Decision 6.)
- **O-4 (review tier per slice)** — `sol` (high-risk) for Decisions 1–4
  (E2E, property, aging, AND seed — seed is stateful bootstrap touching
  tenancy + event wiring + idempotency, so it belongs at the strict tier),
  `terra` (routine) for Decision 5 (pure docs). Every slice still gets a
  diff review; using `sol` for the docs slice too is unnecessary.

## 7. Consensus Revisions (v1 → v2)

Codex architecture-consensus review v1 (gpt-5.6-sol) returned **NEEDS
REVISION**: "the core approach is correct, but the property-test harness,
fresh-database seed bootstrap, idempotency strategy, and commit sequencing
need specification before implementation starts." All 8 findings were
verified against the actual repo (not accepted blindly) and are all real —
each is a "specify more precisely", none disputes the plan's direction.
Resolutions:

- **P1 (seed bootstrap on a fresh DB)** — verified: migration 0001 creates
  but does not populate `currencies`/`uom`; only the test-only
  `_seed_reference_data` fixture inserts `TWD`/`EA`. Folded the exact
  bootstrap order (wiring → TWD/EA → company → `company_context` → chart +
  open period → masterdata/stock/scenarios) into Decision 4. No migration
  needed.
- **P1 (property harness isolation)** — verified: the existing property
  test shares ONE company and accumulates `cumulative_expected` across
  examples. Rewrote Decision 2's harness spec to require a fresh company
  per example, pure operation plans, per-example engine create/dispose,
  `deadline=None`, `app.main` wiring, and ≥1 non-zero posting; and resolved
  the "droppable DoD item" contradiction (either in with the harness, or
  formally deferred).
- **P2 (seed idempotency)** — verified: `SalesOrder` has `CustomDataMixin`
  (usable dedup key) but no external-ref; manual stock adjustments have no
  dedup key. Rewrote Decision 4's idempotency to key scenarios on
  `SalesOrder.custom_data`, resume through legal states, reconcile stock to
  a target quantity, reject incompatible collisions, and added a run-twice
  idempotency test.
- **P2 (E2E assertion strength)** — folded per-step balance-delta snapshots
  and the non-zero tie-out-at-invoice-issue into Decision 1 (a bare
  "balances" check and a final `0 == 0` are too weak).
- **P2 (aging equivalence guard)** — folded the boundary characterization
  tests (0/1, 30/31, 60/61, 90/91 days, multi-invoice one bucket, mixed
  balance+credit customer) and the outer-conditional-aggregation query
  shape into Decision 3.
- **P2 (sequencing)** — README moved from FIRST to LAST; ADRs lead.
  Decision 6 rewritten.
- **P2 (demo runner)** — added an explicit `app/cli/demo_o2c.py` (not
  inline Makefile curl), `make up` readiness gating, and a defined
  resume-a-stable-scenario rerun behaviour, in Decision 4.
- **P3 (inventory invariant)** — added `on_hand >= 0` to Decision 2's
  property assertions (near-free, and the master plan promises it).

Open question answers O-1..O-4 above are Codex v1's recommendations,
adopted.

### v2 → v3 (Codex consensus review v2: 7/8 resolved, 1 open + 1 new)

Codex v2 confirmed seven of the eight v1 findings RESOLVED and returned
NEEDS REVISION on one theme only: **seed idempotency across the full O2C
chain**, split into two concrete points (both verified against the repo,
both real — the modules' own idempotency keys are exactly the mechanisms
the fix leans on):

- **P2 (seed-idempotency, still open)** — recovery was specified only for
  the order, not the invoice/payment/allocation downstream. Rewrote
  Decision 4's idempotency to recover invoices by order lookup (never
  re-issue into the `uq_invoices_order_live` conflict), feed payments a
  stable `external_ref` (reusing `uq_payments_company_external_ref` +
  Week 6's `get_payment_by_external_ref`), and feed allocations a stable
  `request_ref` with identical payload (reusing the ADR-008 R14
  allocation-command fingerprint). The seed invents no new idempotency
  mechanism — it feeds stable keys into the ones the modules already have.
- **New P2 (stock-target over-correction)** — reconciling stock to a fixed
  pre-scenario target before resuming would restore an already-shipped
  scenario's consumed stock and then skip re-shipping, changing on-hand and
  failing the run-twice test. Rewrote the stock rule to be
  remaining-work-aware: reconcile to `desired_final_on_hand + Σ(qty still
  awaiting shipment)`, resume, then assert the desired final target.

**This brief now awaits a v3 consensus re-review; implementation does not
begin until Consensus Status: APPROVED.**
