# ADR-005: Ledger module — journal storage, immutability, and trial balance

**Status:** Accepted (consensus review v1 passed 2026-08-13; see "Consensus Revisions")
**Date:** 2026-08-13
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

> Numbering note: ADR-001..004 are reserved for the topics listed in
> `docs/open-erp-master-plan.md` §6 (modular monolith, append-only inventory,
> posting engine, sync events) and will be written alongside their
> implementing weeks. This ADR covers the Week 2 ledger deliverable.

## Context

Week 2 delivers the `ledger` module: chart-of-account usage (accounts already
exist in `masterdata`), journal entries/lines, accounting periods, and the
trial balance API. Master-plan §10.1 fixes some parameters already — amounts
are `NUMERIC(20, 6)`, journal lines carry both transaction-currency and
functional-currency amounts, **balance is enforced in functional currency**,
and entries are immutable (reversal-only corrections). Four design decisions
remain open and are resolved here.

Constraints: modular monolith on PostgreSQL; one developer; every invariant
must be provable by tests (property-based where possible); Phase 1 data
volumes are small but the schema must not need rework in Phase 3 (MRP/costing
multiplies entry volume).

## Decision

1. **Debit/credit as two columns** (`debit`, `credit`, both `NUMERIC(20,6) >= 0`,
   with `CHECK (debit = 0 OR credit = 0)`), not a single signed amount.
2. **Entry balance enforced by a deferred Postgres constraint trigger**
   (SUM(debit) = SUM(credit) per entry in functional currency, checked at
   commit), plus the same validation in the service layer for fast failure.
3. **Immutability enforced at both layers**: no UPDATE/DELETE endpoints or
   service functions, plus `BEFORE UPDATE OR DELETE` triggers on
   `journal_entries`/`journal_lines` that raise unconditionally.
4. **Trial balance computed on the fly** (indexed aggregate over
   `journal_lines`), no maintained balance table in Phase 1.

## Options Considered

### Decision 1 — line amount representation

**Option A: two columns (debit / credit)** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — mirrors how accountants read a journal |
| Correctness | `CHECK` constraints make illegal lines unrepresentable |
| Reporting | Trial balance debit/credit totals fall out directly |

**Pros:** matches accounting convention and every ERP users compare against;
CHECK-enforceable at the row level; unambiguous in API payloads.
**Cons:** two nullable-ish columns instead of one; SUM expressions slightly longer.

**Option B: single signed amount**

**Pros:** simpler arithmetic (`SUM(amount) = 0` per entry); one column.
**Cons:** sign conventions per account type are a chronic source of confusion
and bugs; row-level CHECK cannot distinguish "credit of 100" from "typo";
every report must re-derive debit/credit presentation.

### Decision 2 — balance invariant enforcement

**Option A: deferred constraint trigger + service-layer check** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — one trigger function, well-documented pattern |
| Integrity | Holds even if a future code path (plugin, script, bulk import) bypasses the service |
| Performance | Per-entry SUM at commit; negligible at Phase 1–3 volumes |

**Pros:** master-plan §10.1 says "DB constraint" — a cross-row invariant in
Postgres honestly requires a constraint trigger (plain CHECK cannot span
rows); defense in depth against non-service writers, which matter once the
plugin system (Phase 2) allows third-party code in-process.
**Cons:** trigger logic lives in a migration, slightly less visible than
Python; deferred triggers need a note in CONTRIBUTING for test authors.

**Option B: service-layer check only**

**Pros:** simplest; all logic in one language.
**Cons:** the invariant the whole project's credibility rests on ("trial
balance always balances") would be enforceable-by-convention only; a single
buggy plugin or manual SQL fix could silently corrupt the books.

**Option C: DB trigger only, no service check**

**Pros:** single enforcement point.
**Cons:** violations surface as opaque commit-time errors instead of a clean
422 with line-level detail; poor API ergonomics.

### Decision 3 — immutability mechanism

**Option A: absent endpoints + DB triggers** — chosen

**Pros:** "no UPDATE endpoint" is policy, the trigger makes it physics;
reversal-only correction becomes structurally guaranteed, which is the audit
story told in the README and interviews; trivial to demo (`UPDATE` in psql
fails loudly).
**Cons:** admin data-repair requires an explicit, logged escape hatch
(deliberately out of scope until a real need appears).

**Option B: absent endpoints only**

**Pros:** zero DB-side machinery.
**Cons:** same class of gap as Decision 2 Option B — one careless migration
or script rewrites history and the audit claim is false.

### Decision 4 — trial balance computation

**Option A: on-the-fly aggregate** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — one GROUP BY query with `(company_id, account_id)` index support |
| Freshness | Always exact; no reconciliation job to break |
| Scale ceiling | Fine to millions of lines; revisit in Phase 3 costing |

**Pros:** zero denormalization to keep consistent; the trial balance being a
pure function of `journal_lines` is itself the correctness demo.
**Cons:** report latency grows with history; period-end snapshots will
eventually be wanted (recorded as the Phase 3 revisit trigger).

**Option B: maintained `account_balances` table**

**Pros:** O(1) reads.
**Cons:** a second source of truth requiring transactional maintenance and a
rebuild command — exactly the machinery inventory needs (§10.5) but ledger,
at Phase 1 read volumes, does not; premature.

## Trade-off Analysis

The through-line in all four choices: **the ledger's invariants are the
product**. Where a decision trades a little implementation convenience for an
invariant that holds against *any* writer (not just the well-behaved service
layer), we take the DB-level guarantee — that is precisely decisions 2 and 3.
Where denormalization would create a second source of truth without a present
performance need, we decline it — decision 4. Decision 1 is simply choosing
the representation the domain has used for five hundred years.

Period model (not a contested decision, recorded for completeness):
`accounting_periods(company_id, year, month, status open|closed)` unique per
company-month; periods are created explicitly via API (open by default on
creation, closable once, reopening is out of scope until a real workflow
demands it); posting resolves `entry_date` → period and rejects with 409 if
the period is closed or absent. Entries are tenant-scoped via the same
`TenantScopedMixin` mechanism as masterdata.

## Consequences

- Easier: proving correctness (hypothesis property tests can hammer random
  entry sequences against a DB that physically cannot hold unbalanced or
  edited entries); explaining the audit posture; Week 3's posting engine
  gets a ledger API that only accepts valid, immutable entries.
- Harder: admin corrections require reversal entries even for fat-finger
  mistakes (by design); migrations carry trigger DDL that must be kept in
  sync if line schema evolves; deferred-trigger semantics are a new concept
  for contributors (document in CONTRIBUTING).
- Revisit: balance snapshot/materialization when Phase 3 costing lands or
  trial balance p95 exceeds ~500ms; admin escape hatch policy if/when a real
  data-repair case appears.

## Consensus Revisions (review v1, 2026-08-13)

Four P2 findings from `/CODEX REVIEW ARCHITECTURE`, all accepted and resolved
here. These are normative for the Week 2 implementation.

**R1 — Dual-currency line schema (resolves: §10.1 mapping left implicit).**
`journal_lines` carries two debit/credit pairs plus the rate snapshot:

| Column | Type | Meaning |
|---|---|---|
| `currency_code` | CHAR(3) FK | transaction currency of this line |
| `txn_debit` / `txn_credit` | NUMERIC(20,6) ≥ 0 | amounts in transaction currency |
| `debit` / `credit` | NUMERIC(20,6) ≥ 0 | amounts in the company's functional currency |
| `exchange_rate` | NUMERIC(20,10) | rate used at posting (txn → functional) |
| `rate_date` | DATE | rate snapshot date |

The one-side-only CHECK applies to both pairs (`txn_debit = 0 OR txn_credit = 0`,
`debit = 0 OR credit = 0`, and the nonzero sides must agree). **The balance
constraint trigger sums the functional pair (`debit`/`credit`) only** — §10.1's
"balance enforced in functional currency". Phase 1 is TWD-only in practice:
`currency_code = functional`, `exchange_rate = 1`, both pairs equal.

**R2 — Gapless entry numbering (resolves: entry_no undefined).**
`entry_no` is gapless per company per year, format `JE-{YYYY}-{NNNNNN}`,
allocated from a `ledger_sequences(company_id, year, next_no)` counter row
locked with `SELECT ... FOR UPDATE` inside the posting transaction — a
rollback rolls the counter back too, so no gaps. Trade-off accepted: entry
creation serializes per company; fine at Phase 1–3 volumes, and the audit
story ("no missing voucher numbers") is worth more than parallel inserts.
Revisit only if posting throughput ever becomes a measured bottleneck.

**R3 — Reversal is part of Week 2 (resolves: reversal-only policy had no
mechanism).** `journal_entries.reversal_of_id` (nullable FK, `UNIQUE` — an
entry can be reversed at most once) + `POST /journal-entries/{id}/reverse`,
which creates a new entry in the *current* open period with all lines
debit/credit-swapped and links it. Reversing an already-reversed entry or a
reversal itself returns 409. This ships in the same week as immutability —
the policy and its only escape valve arrive together.

**R4 — Period-close vs posting race (resolves: TOCTOU on period status).**
Within the posting transaction, the service resolves `entry_date` → period
and takes `SELECT ... FOR SHARE` on that period row before inserting; the
close operation updates the period row (which requires `FOR UPDATE`,
conflicting with all in-flight `FOR SHARE` holders). A concurrent close
therefore either waits for in-flight postings to commit or beats them, in
which case their period re-check fails — no entry can land in a period that
was closed when its transaction committed. Verified by a dedicated
concurrency test (two sessions: post vs close racing on the same period).

**P3 (deferred, documented):** trial balance query parameters (period /
as-of-date filter) defined at implementation; admin escape hatch remains
explicitly out of scope until a real data-repair case appears.

## Action Items

1. [x] Run `/CODEX REVIEW ARCHITECTURE` on this ADR — passed v1, four P2s resolved above (Consensus Status: APPROVED)
2. [ ] Migration 0002: `accounting_periods`, `journal_entries` (+`reversal_of_id`), `journal_lines` (dual-currency per R1), `ledger_sequences` + CHECK constraints + balance constraint trigger + immutability triggers
3. [ ] `ledger` module: schemas/service/router — create entry, list/get entry, reverse entry (R3), open/close period, trial balance endpoint
4. [ ] Tests: API integration, closed-period rejection, period-close race (R4), gapless numbering under rollback (R2), reversal semantics (R3), immutability (direct SQL UPDATE must fail), hypothesis property test (random valid entries ⇒ trial balance always balances), unbalanced-entry rejection at both service and DB layer
5. [ ] Update module README; ADR status set to Accepted (done)
