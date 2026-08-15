# ADR-001: Modular monolith, not microservices

**Status:** Accepted — retroactively documented (Week 7, 2026-08-15), per
`docs/open-erp-master-plan.md` §6's commitment to write this ADR; the
decision itself was made and has held, unchanged, since the repo's Week 1
skeleton.
**Date decided:** Phase 1 kickoff (Week 1). **Date documented:** 2026-08-15.
**Deciders:** Ryan (project owner).

> One of the four ADR topics `docs/open-erp-master-plan.md` §6 commits to
> ("面試官真的會看"). Companion: ADR-002 (the append-only ledger pattern
> this architecture makes practical). This ADR documents a decision already
> made and shipped — see "Documentation note" at the bottom for why it's
> backfilled rather than pre-implementation like ADR-003..008.

## Context

`docs/open-erp-master-plan.md` scopes a 4-phase, multi-year ERP: Phase 1
Kernel (masterdata/ledger/sales/inventory/receivables), Phase 2 Platform
(plugin loader, custom fields, workflow, RBAC, integration), Phase 3 Suites
(purchase, payables, MRP, costing), Phase 4 Taiwan localization. Every
phase after Phase 1 assumes the prior phases' data and transactions are
still reachable in the same process — a sales order confirmation needs to
check inventory *and* the ledger *and* (once it exists) a workflow engine's
approval state, inside one commit-or-nothing boundary.

The project also has exactly one developer for the foreseeable future
(§5's whole "open-source 經營" section is about *attracting* more, not
assuming they exist yet), and no deployed users whose independent scaling
needs would justify splitting anything.

## Decision

**Modular monolith for Phases 1–3**, not microservices. One deployable
process (`app/main.py`), one PostgreSQL database, business logic organized
into `app/modules/<domain>/` packages (`masterdata`, `ledger`, `sales`,
`inventory`, `receivables`, ...) that are independent at the *code* level
but share one transaction boundary at the *data* level.

Two structural choices make this "monolith with the option to leave", not
"one giant undifferentiated codebase":

1. **Modules never import each other's ORM models.** Cross-module reads go
   through either a lightweight `sqlalchemy.table()` Core reference with an
   explicit `company_id` predicate (e.g. `receivables.service` reading
   `sales_orders`), or the in-process event bus (ADR-004) for
   cross-module *writes* triggered by another module's state change (e.g.
   `ledger.posting` reacting to `sales.goods_shipped`). No module ever
   calls another module's `service.py` function directly.
2. **`import-linter` enforces this in CI, not by convention.** Five
   contracts (see `pyproject.toml`): core must not import business modules;
   business modules are independent of each other; masterdata must not
   import other business modules; core/business modules must not import
   plugins. A violation fails the build — the same way a network boundary
   between microservices is enforced by "you literally cannot call a
   function in a process you can't reach", CI enforces it here by static
   analysis instead.

The three extraction candidates explicitly reserved for Phase 3+ (master
plan §4) — a read-replica reporting/BI layer, an integration gateway
(webhook fan-out), and scheduled batch jobs (MRP explosion) — are exactly
the workloads with genuinely different scaling/consistency needs from the
transactional core, and they'd be extracted *behind the same outbox event
stream* (ADR-004) that already durably records everything a downstream
consumer would need. Nothing else is a extraction candidate before there's
a concrete, felt need.

## Options Considered

### Option A: Modular monolith — chosen

| Dimension | Assessment |
|---|---|
| Cross-module consistency | Free — one DB transaction covers order confirm + credit check + (later) workflow approval + inventory reservation |
| Operational cost | One process to deploy, log, monitor, migrate — matches a solo maintainer's actual capacity |
| Team size fit | Correct for "1 person, possibly a few contributors" — microservices' entire value proposition (independent team ownership, independent deploy cadence) requires a team size this project doesn't have |
| Extraction discipline | import-linter's CI-enforced module boundaries mean extraction later is a *mechanical* exercise (the boundary already exists in code), not an archaeology project |
| Interview/README narrative | "modular monolith, CI-enforced boundaries, ready to split when it matters" is itself a demonstrated architectural judgment call — more credible than either extreme |

**Pros:** every cross-module invariant this project cares about proving
(trial balance always balances across *any* sequence of sales/inventory/
receivables events, stock never goes negative, AR always ties to the
ledger) is trivial to guarantee inside one transaction and genuinely hard
to guarantee across a network boundary (distributed transactions, sagas,
eventual-consistency reconciliation — all real engineering, none of it
this project's *point*). Local function calls have none of a network
call's failure modes (partial failure, retries, idempotency-across-services,
service discovery) to design around on top of the domain problem.

**Cons:** a genuine microservices story (service mesh, independent
scaling, polyglot services) is not what gets demonstrated. Accepted — this
project's résumé/portfolio narrative (`docs/mini-erp-architecture.md` §9)
is about domain modeling, correctness proof, and *disciplined* module
boundaries, not distributed-systems operations; those are different,
equally valid, portfolio stories and this one isn't trying to be both.

### Option B: Microservices from the start

**Pros:** the "expected" answer for a system with this many bounded
contexts (masterdata/ledger/sales/inventory/receivables/...); independent
deploy/scale per service; the naive box-per-module reading of the module
list in master plan §3 maps almost 1:1 onto "service per box".

**Cons — the actual reasons this is rejected, not just "harder"**:
- **Distributed transactions for domain-critical invariants.** "Ship an
  order" must atomically move inventory, post COGS, and (per this
  project's own correctness obsession) never leave the books in a state
  where the property test could catch an imbalance. Across services this
  needs a saga/outbox-per-service/compensating-transaction design — real,
  well-understood engineering, but its entire cost buys *nothing* this
  project needs yet, since there is no scaling or team-ownership pressure
  to justify it.
- **One-person operational reality.** N services means N sets of
  deploy config, N logs to correlate, N independent failure domains to
  reason about, for zero current benefit (no independent scaling need, no
  independent team). Master plan §5's own risk table names "燒完熱情
  (burning out passion)" as the #1 project risk — self-inflicted
  distributed-systems operational overhead is exactly that risk's fuel.
- **Rejected the "microservices = better architecture" framing directly**:
  master plan §4 states this plainly — "ERP 核心交易需要跨模組強一致；一人
  開發，微服務是自殺" (ERP core transactions need strong cross-module
  consistency; solo development, microservices is suicide).

### Option C: "Big ball of mud" single-package monolith (no module boundaries at all)

**Pros:** fastest to start; no import-linter contracts to write or
maintain; no discipline required day to day.

**Cons:** this is the failure mode Option A is specifically designed to
avoid — without CI-enforced boundaries, module coupling accretes silently
(a `sales` function importing `ledger.service` directly "just this once")
until nothing can ever be extracted without a rewrite, and the codebase
stops being legible as separate domains at all. It also undermines the
entire portfolio narrative: "disciplined modular monolith" is a real,
demonstrable engineering choice; "monolith because nobody stopped it" is
not. Rejected — the cost of `import-linter` contracts is a handful of
config lines and a CI check; the alternative is unbounded, silent coupling
debt.

## Trade-off Analysis

The decision optimizes for exactly the two things this project's stated
context provides in abundance and exactly one thing it lacks: it optimizes
for **cross-module transactional correctness** (abundant need — every ADR
in this repo from ADR-003 onward assumes one transaction spans multiple
modules' writes) and **solo-maintainer operational simplicity** (abundant
constraint), at the cost of **not demonstrating distributed-systems
operations** (a capability this project's narrative was never trying to
sell). The `import-linter`-enforced boundary is what keeps this a genuine
trade-off rather than a cop-out: the monolith is disciplined enough that
Phase 3's three extraction candidates (reporting/BI, integration gateway,
scheduled batch) can actually be pulled out later *because* the seam
already exists in code, not despite it never having been drawn.

## Consequences

- **Easier:** every ADR-003..008 invariant (trial balance balance, AR
  tie-out, stock non-negativity, idempotent posting) is provable and
  testable inside one transaction with one property-test harness; adding a
  new module (Phase 3's `purchase`/`payables`/`mrp`/`costing`) is "add a
  package + register its event handlers", not "stand up new
  infrastructure"; CI catches boundary violations before they're a design
  discussion, let alone a production incident.
- **Harder:** cannot scale one module's read/write throughput
  independently of the others (irrelevant at Phase 1's demo scale;
  revisited via Phase 3's extraction candidates if it's ever real); a bug
  in one module's request handler can, in principle, take down the whole
  process (mitigated by the same test/typing/lint discipline every ADR in
  this repo already applies, and by this being a monolith with clean seams
  rather than a soup — a crash is a crash either way, but a *hang* or
  resource leak is easier to isolate with actual process boundaries; not
  yet a problem this project has hit).
- **Revisit:** if/when Phase 3's reporting/BI layer, integration gateway,
  or scheduled-batch workload need genuinely independent scaling or
  deployment cadence, extract them behind the outbox event stream
  (ADR-004) that already durably records what they'd consume — the
  boundary is drawn; extraction is an infrastructure exercise at that
  point, not a design one.

## Documentation note

This ADR was written in Week 7 to fulfil `docs/open-erp-master-plan.md`
§6's explicit commitment ("ADR 至少寫四篇（面試官真的會看）：ADR-001 為什麼
選 modular monolith 而非微服務"). Unlike ADR-003 through ADR-008, it does
not carry a "Consensus Revisions" section from a pre-implementation
architecture-consensus review — the decision it documents was made at
Week 1 and has been exercised, unchanged, by every subsequent ADR and
week's implementation since; there was nothing left to gate before code.
It received a routine-tier Codex diff review for factual accuracy against
the actual repo (import-linter contracts, module layout) before merge, per
the Week 7 hardening brief's Decision 6/O-4.
