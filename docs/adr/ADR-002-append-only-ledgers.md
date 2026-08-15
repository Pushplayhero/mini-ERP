# ADR-002: Append-only fact tables, and caching their current-state aggregate only where the domain needs it

**Status:** Accepted — retroactively documented (Week 7, 2026-08-15), per
`docs/open-erp-master-plan.md` §6's commitment to write this ADR.
**Date decided:** the append-only-fact-table axiom was fixed at Week 1's
architecture sketch (`mini-erp-architecture.md` §3) and never seriously
reopened; the *caching* question below it was decided three times, once
per domain, with two different outcomes: Week 2 (ledger, ADR-005 Decision
4 — no cache), Week 5 (inventory, ADR-007 Decision 2 — cached), Week 6
(receivables, ADR-008 Decision 3 — cached). **Date documented:** 2026-08-15.
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate, for
each concrete application).

> One of the four ADR topics `docs/open-erp-master-plan.md` §6 commits to.
> Master plan's own naming for this ADR is narrower ("庫存採 append-only
> 異動帳" — inventory specifically); see "Scope note" at the bottom for why
> this ADR is written one level more general than that.

## Context

Three places in this codebase need to answer "what is the current
[account balance / on-hand quantity / settled amount] of X", where X
changes over time via many independent, possibly-concurrent transactions,
under a hard requirement that the answer be *provably* correct
(property-tested, not just "usually right") and *auditable* (every change
traceable to the business event that caused it):

1. **Ledger account balances** (Week 2, ADR-005).
2. **Inventory on-hand quantity** (Week 5, ADR-007 Decision 2).
3. **Invoice/payment settlement** (Week 6, ADR-008 Decision 3).

There are actually **two separable design questions** here, and this
codebase answers them differently:

- **Question 1 — is the history append-only, or does a write mutate a
  quantity/balance column in place with no trace of what changed?** This
  was fixed once, early, as a project-wide axiom
  (`mini-erp-architecture.md` §3: "分錄不可變...錯帳開反向分錄沖銷"; "庫存
  用異動帳而非數量欄位...天然稽核軌跡") and never seriously reopened —
  `journal_lines`, `stock_moves`, and `payment_allocations` are all
  `INSERT`-only fact tables with no `UPDATE`/`DELETE` endpoint, full stop.
  ADR-007's own Decision 2 restates the inventory instance of this axiom
  for completeness ("Option B: quantity column on products… rejected in
  master-plan §10.5 already") rather than re-deriving it from scratch.
- **Question 2 — given the append-only facts, is the *current-state
  aggregate* (balance / on-hand / settled amount) cached in a
  transactionally-maintained column, or computed fresh on every read?**
  This one is genuinely a per-domain trade-off, and — this is the part
  worth an ADR, not just a restatement — **the three domains answered it
  differently, on its actual merits each time**: the ledger computes on
  the fly (ADR-005 Decision 4, chosen over "Option B: maintained
  `account_balances` table", rejected as "premature" at Phase 1 volumes);
  inventory and receivables both cache (ADR-007 Decision 2, ADR-008
  Decision 3), because both have a synchronous, concurrency-sensitive
  *capacity check* on every write (won't overship, won't over-allocate).
  ADR-008 Decision 3 spells this out explicitly: its own "Option B:
  compute balances on read (`SUM` over allocations)" was rejected for
  degenerating the concurrent-allocation check into the exact
  check-then-insert TOCTOU shape this project's row-lock-before-state-check
  doctrine (ADR-005 R4 / ADR-006 R1) exists to eliminate — a maintained
  column, locked before the check, is what makes the lock *mean* something;
  an on-the-fly aggregate has no row to lock. The ledger has no equivalent
  concurrent-capacity-check need (a journal entry's own balance is
  self-contained per entry, not competing for a shared account-level
  budget the way concurrent allocations compete for an invoice's remaining
  balance), so on-the-fly stays adequate there.

## Decision

**Every fact table in this codebase that records value-bearing history is
append-only, unconditionally (Question 1 — the axiom).** Whether its
current-state aggregate is additionally cached in a maintained column
(Question 2) is decided **per domain, on that domain's own concurrency and
read-volume profile** — not applied uniformly, because it doesn't need to
be:

1. **Ledger** (`journal_lines` → `accounts` balance): NOT cached.
   `accounts` has no balance column at all; the trial balance is always an
   on-the-fly `SUM(debit)/SUM(credit)` aggregate over `journal_lines`
   (ADR-005 Decision 4). No summary to drift, ever, by construction —
   adequate because Phase 1 read volumes don't need the O(1) win and there
   is no concurrent-capacity-check that would otherwise degenerate into a
   TOCTOU race.
2. **Inventory** (`stock_moves` → `stock_summary.on_hand`): cached, under
   `SELECT ... FOR UPDATE` on the summary row, updated in the same
   transaction as the move (ADR-007 Decision 2) — because shipping needs a
   synchronous "is there enough stock" capacity check.
3. **Receivables** (`payment_allocations` → `invoices.settled_amount` /
   `payments.allocated_amount`): cached, same lock discipline (ADR-008
   Decision 3) — because allocation needs the equivalent "is there enough
   remaining invoice/payment capacity" check, and ADR-008 explicitly
   rejected the on-the-fly alternative for exactly the TOCTOU reason above.

Where a maintained column exists (2 and 3), the shared discipline is
identical across both: it's updated **transactionally, in the same commit
as the fact row**, under `SELECT ... FOR UPDATE` (the row-lock-before-
state-check doctrine, ADR-005 R4 / ADR-006 R1, applied uniformly); it is
**always rebuildable from the facts alone** via a dedicated CLI
(`app/cli/rebuild_stock_summary.py`, `app/cli/rebuild_ar_balances.py`)
plus a reconciliation test asserting summary == aggregate of facts after
every test scenario that touches it; and a DB-level `CHECK` backstops the
invariant it protects (`stock_summary.on_hand >= 0`; the exhaustive
status↔amount CHECKs on `invoices`/`payments`) so even a bug that bypasses
application logic cannot write a physically-impossible state. The ledger's
own dual-entry balance is enforced the same "DB backstop, not just
application trust" way, just via a trigger over the raw facts
(`ledger_check_entry_balance`, migration 0002) rather than a check on a
cached column, since it has no cached column to check.

## Options Considered

### Option A: Append-only facts always; cache the aggregate only where a concurrent capacity check needs it — chosen

| Dimension | Assessment |
|---|---|
| Audit trail | Free in all three domains — the fact table *is* the audit trail |
| Concurrency | Where a maintained column exists, `SELECT ... FOR UPDATE` on it correctly serializes the concurrent capacity check it protects; the ledger achieves the equivalent "no invariant can be silently violated" guarantee without a per-account lock, because nothing in the ledger's write path needs one |
| Provability | Where cached, the reconciliation property ("summary == aggregate of facts") is a single, testable, property-testable assertion; where not cached (ledger), the aggregate being a pure function of the facts is itself the property |
| Recoverability | A maintained column corrupted by a bug is a `rebuild_*` CLI run against the untouched facts, never a data-loss incident; the ledger's balance can't drift at all — there's nothing to corrupt |
| Cost discipline | Two domains pay "one extra column + rebuild CLI + reconciliation test" because they need the concurrency property it buys; the third domain doesn't pay it because it wouldn't buy anything there — the pattern isn't applied by rote |

**Pros:** Question 1 alone (append-only facts) is what makes every
correctness claim in this project *auditable and rebuildable* — the fact
that trial balance always balances, AR ties to the ledger, and stock never
goes negative can each be diagnosed and repaired from history, because
none of the underlying mechanisms are ever destructively overwritten. The
invariants themselves are each enforced by their own separate mechanism,
not by Question 1 alone: the trial balance's dual-entry balance is a DB
trigger over `journal_lines` (ADR-005); stock non-negativity is Question
2's `stock_summary.on_hand >= 0` CHECK plus the `FOR UPDATE` lock on it
(ADR-007); the AR/1100 tie-out depends on both Question 2's maintained
columns *and* the posting rules and control-account protections ADR-008
adds on top (neither of which is this ADR's subject). What Question 1
buys is the *substrate* those mechanisms can rely on — none of them would
be provable or repairable if the facts behind them could be silently
edited. Question 2's answer being chosen per domain, landing on two
different outcomes, is itself evidence the pattern was reasoned about each
time rather than cargo-culted from the first instance.
**Cons:** two different "how do I get the current balance" code shapes
exist in the codebase (on-the-fly query vs. read-a-column) instead of one
uniform shape — a newcomer has to learn both are the same underlying idea
applied differently, not that one is stale.

### Option B: Uniform answer to Question 2 (always cache, or never cache)

**Always cache:** would have added an unused, permanently-in-sync-by-
construction `account_balances` table for the ledger — ADR-005 Decision 4
explicitly rejected this as "premature" at Phase 1 volumes: pure
denormalization overhead (a second thing to keep consistent, a rebuild CLI
to write and test) bought against a real-only-at-scale performance win the
project doesn't have yet. **Never cache:** ADR-008 Decision 3's "Option B:
compute balances on read" is the explicit, written-out version of this
rejection for receivables — every allocation-capacity check, status
derivation, aging query, and credit-exposure query would pay a join+
aggregate, and the concurrency story for "two allocations race the same
invoice's remaining capacity" degenerates into exactly the check-then-insert
TOCTOU shape this project's locking doctrine exists to eliminate (ADR-008
cites ADR-003 and ADR-007 as the prior instances of that same doctrine
being applied). Inventory's shipment capacity check
(`on_hand >= qty` under `SELECT ... FOR UPDATE`, ADR-007 Decision 2) is the
identical concurrency shape one level earlier — a concurrent shipment must
not oversell the same product — though ADR-007's own text states the
chosen design without writing out an explicit "vs. compute-on-read"
comparison the way ADR-008 later did; the underlying need (a locked
capacity check, not a racy read-then-write) is the same. Rejected in both
cases: a single uniform "never cache" answer would leave inventory and
receivables with a concurrency hole they can't afford.

### Option C: Full event sourcing (never materialize any aggregate anywhere; every read replays all events)

**Pros:** the purest form of "facts are the only source of truth" for
every domain uniformly — no cached column anywhere, so no reconciliation
story to get wrong, and no per-domain judgment call to make (or defend).
**Cons:** for inventory and receivables specifically, this reintroduces
the exact concurrent-capacity-check TOCTOU problem Option A's caching was
chosen to solve (replaying events to check capacity is still a
check-then-insert race unless something is locked, and there's no natural
row to lock without a materialized column); would need a caching layer
anyway to be usable at real data volume, at which point it's Option A with
extra steps and no natural home for a DB-level CHECK backstop on a
non-existent column. Rejected as reintroducing the concurrency problem
Option A already solves where it matters, for a uniformity this project's
design doesn't actually value over correctness.

## Trade-off Analysis

The decision optimizes for **matching each domain's actual concurrency
need**, not for surface-level uniformity: the ledger stays maximally
simple (no cache, nothing that can drift) because nothing in its write
path needs a locked capacity check; inventory and receivables both pay the
"maintained column + rebuild CLI + reconciliation test" cost because both
have a real concurrent-capacity-check requirement that on-the-fly
aggregation would leave racy. The audit-trail property (Question 1) is
never traded away in any of the three — that one really is applied
uniformly, because nothing about it is domain-specific.

## Consequences

- **Easier:** the "prove this invariant holds under any valid event
  sequence" property tests in this repo (existing: trial-balance-balances;
  planned, Week 7 Decision 2: extended to drive domain events and prove
  `on_hand >= 0`) are provable at all because the facts behind every
  invariant are never destructively rewritten — each invariant's own
  mechanism (a DB trigger, a CHECK plus a lock, a maintained column) still
  does the actual enforcing; a data-integrity incident in either maintained summary
  (inventory, receivables) is a `rebuild_*` CLI run, not a support
  escalation, while the ledger's balance can't drift at all; a future
  domain (Phase 3's costing) inherits a clear decision procedure — "does
  this need a locked concurrent-capacity check? if yes, cache; if no,
  don't" — instead of a single rule applied without asking the question.
- **Harder:** every write path touching a maintained column must remember
  the lock-then-check-then-write discipline correctly (documented once per
  ADR, but a rule every new writer must still apply); the codebase has two
  different "read the current balance" shapes to learn instead of one.
- **Revisit:** if the ledger's on-the-fly trial balance ever genuinely
  outgrows Phase 1 read volumes (ADR-005 Decision 4's own stated revisit
  trigger), it gets the same maintained-column treatment inventory and
  receivables already have — the decision procedure above already covers
  that case, it just hasn't been needed yet.

## Scope note

`docs/open-erp-master-plan.md` §6 names this ADR "庫存採 append-only 異動
帳" (inventory specifically), reflecting that inventory was the master
plan's original example. This ADR is written one level more general
because the actual codebase's answer is more interesting than "the same
thing three times": Question 1 (append-only) really was applied uniformly;
Question 2 (cache or not) was decided per domain, correctly landing on two
different answers. Domain-specific consequences (COGS costing basis,
settlement status transitions, dual-entry balance triggers) stay in their
own ADRs (005/007/008) — this one is the cross-cutting decision procedure.

It received a routine-tier Codex diff review for factual accuracy against
the actual repo and against ADR-005/007/008's own text (fact-table/summary
column names, which Option was actually chosen/rejected where, CHECK
constraints, rebuild CLI paths) before merge, per the Week 7 hardening
brief's Decision 6/O-4. **Revision note:** the first review pass (2026-08-15)
correctly caught an earlier draft's overclaim that the ledger has a
"maintained summary column" like inventory/receivables — it does not; this
revision restructures the whole ADR around the two-question framing above
specifically to make that distinction structurally impossible to blur
again, rather than patching the one sentence that stated it.
