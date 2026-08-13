# ADR-003: Posting engine — declarative rules, module placement, and idempotency

**Status:** Accepted (consensus review v1 passed 2026-08-13; see "Consensus Revisions")
**Date:** 2026-08-13
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

> One of the four ADR topics reserved in `docs/open-erp-master-plan.md` §6.
> Companion: ADR-004 (event bus). Together they define the Week 3 deliverable.

## Context

The posting engine converts business events (goods shipped, invoice issued,
payment received) into balanced journal entries. Master-plan
`mini-erp-architecture.md` §4 sketched a declarative `POSTING_RULES` table in
`core/posting.py`; two things it glossed over now bind:

1. **Import boundaries.** import-linter forbids `app.core` from importing
   `app.modules.*`, and business modules from importing each other. The
   engine must call `ledger.service.create_journal_entry` — so it cannot
   live in `core` as sketched.
2. **Account references.** The sketch wrote rules like
   `Rule(debit="5000-COGS", ...)`, but `accounts` are tenant-scoped rows each
   company creates itself. A rule referencing a literal account id cannot be
   shared across companies; something must resolve the reference per company.

Also in scope: what happens when an event is delivered twice (ADR-004's bus
is at-least-once once replay exists) — double-posting would corrupt the books.

## Decision

1. **The posting engine lives in `app/modules/ledger/posting.py`** — ledger
   owns the rules and subscribes to the bus; `core` stays module-free.
2. **Rules are declarative, in code, and reference accounts by `code`**
   (resolved per company at posting time; missing account = the whole
   business transaction aborts).
3. **Posting is idempotent per source event**: migration 0003 adds a partial
   unique index on `journal_entries (company_id, source_type, source_id)
   WHERE source_type IS NOT NULL`; a duplicate delivery becomes a no-op.

## Options Considered

### Decision 1 — where the engine lives

**Option A: `app/modules/ledger/posting.py`** — chosen

| Dimension | Assessment |
|-----------|------------|
| Boundary integrity | core stays pure infrastructure; contracts unchanged |
| Cohesion | "turn events into entries" is ledger's domain competence |
| Coupling | publishers know nothing about posting; they just emit events |

**Pros:** ledger already owns entry creation, periods, numbering — the engine
is a thin orchestrator over machinery in the same module; import-linter
contracts stay exactly as they are.
**Cons:** "posting rules" arguably serve the whole system, not just ledger —
mitigated by keeping the rule *format* documented at the top level.

**Option B: `app/core/posting.py` (original sketch)**

**Pros:** matches the original architecture doc.
**Cons:** requires core → ledger imports, breaking the "core must not import
business modules" contract that CI has enforced since Week 1; either the
contract dies (bad precedent) or the engine needs an inversion layer whose
only job is to launder the dependency (complexity with no user benefit).

**Option C: new top-level `app/posting/` package**

**Pros:** neutral home.
**Cons:** a sixth top-level thing whose entire content is "call ledger" —
still needs the ledger import, so it's Option A with an extra hop.

### Decision 2 — rule representation and account references

**Option A: in-code declarative registry, account codes** — chosen

```python
# app/modules/ledger/posting.py
POSTING_RULES: dict[str, list[PostingRule]] = {
    "sales.goods_shipped": [
        PostingRule(debit_account_code="5000", credit_account_code="1300",
                    amount_field="cost"),
    ],
    "receivables.invoice_issued": [
        PostingRule(debit_account_code="1100", credit_account_code="4000",
                    amount_field="net_amount"),
    ],
}
```

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — a dict, a dataclass, a resolver |
| Reviewability | Rules are diffable, code-reviewed, tested like code |
| Multi-company | Codes resolved per company at post time — one rule set, N companies |

**Pros:** the rule set is version-controlled and property-testable; per-company
resolution by `code` means every company just needs a chart of accounts using
the standard codes (seedable); resolution failure is loud (transaction
aborts), never a silently mis-posted entry.
**Cons:** changing rules requires a deploy (acceptable until the Phase 2
plugin system, which is the designed extension point for custom rules — hook
`register_posting_rules` is already listed in master-plan §2.5); companies
must adopt the standard account codes for kernel-posted entries (documented;
custom charts are a Phase 2+ configuration concern).

**Option B: rules stored in DB tables**

**Pros:** runtime-editable per company; no deploy to change a mapping.
**Cons:** rules become data with no review gate — a typo'd account mapping
silently mis-posts every subsequent event; needs admin UI, validation
tooling, and migration story that Phase 1 has no budget for; testing surface
explodes. This is Phase 2+ territory *behind* the plugin system, if ever.

**Option C: hardcoded posting inside each business module**

**Pros:** no indirection at all.
**Cons:** exactly the anti-pattern master-plan §2 rejects — posting logic
scattered across modules, every module needs ledger imports (breaking
independence), and the "one engine, provable invariants" story dies.

### Decision 3 — duplicate event delivery

**Option A: partial unique index + skip-on-conflict** — chosen

**Pros:** idempotency is a DB guarantee, not a code promise — same philosophy
as ADR-005 (invariants that hold against any writer); makes ADR-004's replay
CLI safe by construction; `source_type`/`source_id` columns already exist on
`journal_entries` (reserved in Week 2 precisely for this).
**Cons:** one more migration; posting service must catch the conflict and
distinguish "already posted" (no-op, log) from other integrity errors.

**Option B: check-then-insert in the service layer**

**Pros:** no migration.
**Cons:** TOCTOU race under concurrent delivery — the same class of bug R4
just eliminated for periods; rejected on consistency of principle alone.

## Trade-off Analysis

All three decisions follow the project's established doctrine: invariants at
the DB layer (Decision 3, mirroring ADR-005), boundaries enforced by CI
rather than convention (Decision 1), and correctness-reviewable artifacts
over runtime flexibility (Decision 2). The genuine trade-off surrendered is
runtime rule editing — deliberately deferred to the Phase 2 plugin system,
which is the architected place for it, rather than half-building it now.

Posting flow (normative): business module publishes event inside its own
transaction → bus (ADR-004) dispatches synchronously → ledger's handler looks
up rules for the event type → resolves account codes for the event's company
→ builds lines → `create_journal_entry` (existing service: period lock,
gapless numbering, balance validation all apply unchanged) → duplicate
`(company_id, source_type, source_id)` hits the index and no-ops. Any other
failure raises and aborts the entire business transaction — an event with no
valid posting configuration cannot half-succeed.

## Consequences

- Easier: Week 4+ business modules get accounting "for free" by emitting
  events; the trial-balance-always-balances property test extends naturally
  to "any event sequence keeps the books balanced"; Phase 2 plugins have a
  clean registration surface (`register_posting_rules`).
- Harder: kernel rules assume standard account codes (seed data becomes
  load-bearing and must be documented); rule changes ship as code.
- Revisit: per-company rule overrides when the Phase 2 plugin/customization
  layer lands; DB-backed rules only if a concrete customer need survives
  contact with the plugin alternative.

## Consensus Revisions (review v1, 2026-08-13)

**R1 — Transaction ownership (resolves P1: `create_journal_entry` commits,
but handlers must run inside the publisher's transaction).** Week 2's
`ledger.service.create_journal_entry` is refactored into a non-committing
core, `post_journal_entry(session, ...)`, which validates, locks, allocates
the entry number, and `flush()`es — **never commits**. The HTTP route keeps
its current behavior via a thin wrapper that calls the core then commits
(same `_commit_or_conflict` semantics, so the Week 2 API contract and all 73
existing tests are unaffected). The posting handler calls the core only.
Normative rule: **the transaction is owned by whoever opened it** (HTTP
request, replay CLI iteration, or future job) — no service or handler below
that level may commit or roll back the outer transaction.

**R2 — Duplicate delivery via SAVEPOINT (resolves P2: after an
IntegrityError the whole Postgres transaction is aborted, so
"catch-and-continue" alone is impossible).** The handler wraps the entry
insert in `session.begin_nested()`. On unique-index conflict
(`uq_journal_entries_source`), only the savepoint rolls back; the handler
logs "already posted" and returns, and the publisher's business transaction
continues intact. Any other IntegrityError re-raises and aborts everything,
as before.

**R3 — Every event payload carries `company_id` (resolves P2: replay has no
request context).** See ADR-004 R2/R3 for the bus-side enforcement and
replay-side binding.

## Action Items

1. [x] `/CODEX REVIEW ARCHITECTURE` on this ADR + ADR-004 — passed v1; 1 P1 + 3 P2 resolved across the two ADRs (Consensus Status: APPROVED)
1a. [ ] Refactor `ledger.service.create_journal_entry` into non-committing `post_journal_entry` core + committing HTTP wrapper (R1) — all Week 2 tests must stay green unmodified
2. [ ] Migration 0003: partial unique index `uq_journal_entries_source` on `(company_id, source_type, source_id) WHERE source_type IS NOT NULL`
3. [ ] `ledger/posting.py`: `PostingRule`, `POSTING_RULES`, account resolver, event handler; registration onto the bus at app startup
4. [ ] Seed/document the standard chart-of-account codes the kernel rules assume
5. [ ] Tests: rule resolution per company, missing-account abort, duplicate-delivery no-op (two deliveries ⇒ one entry), property test (random event sequences ⇒ trial balance balances), posting failure aborts the publishing transaction
