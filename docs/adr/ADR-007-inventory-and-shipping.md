# ADR-007: Inventory module, shipping, and the first real posting event

**Status:** Accepted (consensus review v1 passed 2026-08-13; see "Consensus Revisions")
**Date:** 2026-08-13
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

## Context

Week 5 delivers `inventory` (append-only stock moves + summary, per
master-plan §10.5) and the `ship` operation that makes three modules
cooperate through the bus inside one transaction: sales transitions the
order, inventory deducts stock, ledger posts COGS. This retires Week 3's
synthetic event — `sales.goods_shipped` becomes the first real posting
event. Fixed constraints: module independence (no cross-module imports),
sync same-transaction dispatch (ADR-004), posting idempotency via partial
unique index + SAVEPOINT (ADR-003), row-lock-before-state-check (ADR-005
R4 / ADR-006 R1), and §10.5's stock_summary + `FOR UPDATE` design.

There is no purchasing module until Phase 3, so stock has no way IN yet —
Week 5 needs a manual intake mechanism as the documented stand-in.

## Decision

1. **Ship is owned by sales** (`POST /sales-orders/{id}/ship`): order-row
   lock, `confirmed → shipped` transition, then publish
   `sales.goods_shipped`; inventory and ledger react as bus subscribers.
2. **Inventory = append-only `stock_moves` + transactionally-maintained
   `stock_summary`** (§10.5), with a manual adjustments endpoint as the
   Phase 1 intake stand-in, and a rebuild CLI + reconciliation test.
3. **COGS cost basis = `products.standard_cost`** (new column), snapshotted
   into the event payload at ship time; moving-average costing stays
   Phase 3.
4. **Stock deduction is idempotent the same way posting is**: partial
   unique index on `stock_moves (company_id, source_type, source_id,
   product_id)` + SAVEPOINT skip, so replaying a shipped event can never
   double-deduct.
5. **Full-order shipment only** in Phase 1; partial shipment is deferred.

## Options Considered

### Decision 1 — who owns "ship"

**Option A: sales owns the endpoint; inventory/ledger subscribe** — chosen

| Dimension | Assessment |
|-----------|------------|
| Boundary integrity | Zero new cross-module imports; sales already owns the state machine |
| Atomicity | One transaction: transition + deduction + COGS all commit or none do |
| Architecture proof | First event with TWO subscribers — ordering and fail-closed semantics get real exercise |

Subscription order (wired in `app/main.py`, deterministic per ADR-004):
inventory's deduction handler first, ledger's posting handler second —
"move the goods, then account for them"; since everything is atomic, the
order matters for error attribution, not correctness. Insufficient stock
raises in the inventory handler → the whole ship aborts, order stays
`confirmed` — the fail-closed story, now with a business-meaningful case.

**Option B: inventory owns a ship endpoint.** Requires inventory to drive
sales' state machine — a cross-module service call the independence
contract forbids; inverting ownership of the order lifecycle for the
convenience of one operation. Rejected.

**Option C: a new orchestration layer above both.** A third place that
knows both modules — this is what the bus already is, minus the extra
indirection. Rejected as premature.

### Decision 2 — inventory representation

**Option A: append-only moves + maintained summary (§10.5)** — chosen

```
stock_moves(id, company_id, product_id, qty_delta,           -- + in, - out
            move_type[shipment|adjustment], source_type, source_id,
            reason, created_at, created_by)                   -- immutable, append-only
stock_summary(company_id, product_id, on_hand,                -- PK (company_id, product_id)
              CHECK on_hand >= 0)                             -- DB backstop for the invariant
```

Shipment flow per line, inside the handler: get-or-create the summary row
(`INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE` — the
established sequences pattern), check `on_hand >= qty` (else
`InsufficientStockError(ConflictError)` → 409 → whole ship aborts), insert
the move, update the summary. Moves are the source of truth; summary is
rebuildable at any time (`python -m app.cli.rebuild_stock_summary`) and a
reconciliation test asserts `summary == SUM(moves)` after every test
scenario that touches stock.

Manual intake: `POST /inventory/adjustments` (product_id, qty_delta
positive or negative, reason mandatory) — same lock-check-move-update path
(negative adjustments also cannot take `on_hand` below zero). Documented
loudly as the Phase 1 stand-in that Phase 3's purchase receipts replace.

**Option B: quantity column on products.** Rejected in master-plan §10.5
already (lost-update-prone, no audit trail); restating for completeness.

### Decision 3 — COGS cost basis

**Option A: `products.standard_cost`, snapshotted into the event** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | One column + one multiplication |
| Reproducibility | Cost rides in the payload — the entry is derivable from the event alone, forever |
| Honesty | Standard costing is a real, legitimate method — not a hack |

`ship` computes `cost = Σ qty × standard_cost` (read via the established
Core table reference), puts per-line detail AND the total into the payload.
**Zero-cost handling**: if total cost is 0 (all products have no standard
cost set), the posting handler skips entry creation entirely — a
zero-amount journal line would (correctly) be rejected by ledger's own
validation, so "nothing of value moved, nothing to post" is the only
consistent reading. Skip is logged. The stock still moves.

**Option B: moving average now.** Real costing needs receipts with costs
(Phase 3 purchasing) to average over — Week 5 has manual adjustments with
no cost dimension. Building it now means building it on fake inputs.
**Option C: no COGS until Phase 3.** Leaves the flagship demo (ship →
trial balance shows COGS/Inventory movement) empty, and retires nothing —
the synthetic event would have to stay. Rejected.

### Decision 4 — replay safety for stock

**Option A: partial unique index + SAVEPOINT, mirroring ADR-003** — chosen

`uq_stock_moves_source ON (company_id, source_type, source_id, product_id)
WHERE source_type IS NOT NULL`. The inventory handler wraps its whole batch
(all lines of one shipment) in one SAVEPOINT; a conflict on any line means
"this shipment's deduction already happened" → roll back the savepoint,
skip, log — identical semantics, same `_is_source_conflict`-style
constraint-name check, to ADR-003 R2. Manual adjustments have NULL
source_type (user-initiated, no replay path, no dedup needed) and are
excluded by the partial index just like manual journal entries are.

**Option B: handler-level existence check.** The TOCTOU-shaped
check-then-insert ADR-003 already rejected for posting; same rejection.

### Decision 5 — shipment granularity

**Full-order only** (`confirmed → shipped`, all lines, one event). Partial
shipment forces per-line shipped-qty tracking, backorder states, and
N-partial-COGS entries — real requirements, but Phase 1's demo needs none
of them. `shipped` orders cannot cancel (ADR-006's cancel-from-confirmed
allowance ends here — the goods left; Phase 2+ returns/RMA is the undo
mechanism, not cancel). Deferred: partial shipment, backorders, returns.

## Order lifecycle update

`draft → confirmed → shipped`; `cancel` legal from `draft`/`confirmed`
only (attempting to cancel a `shipped` order = 409). Migration 0005 adds
`SHIPPED` to the `sales_order_status` enum. Ship follows the R1 doctrine
exactly: `SELECT ... FOR UPDATE` on the order row, re-check
`status == confirmed` under the lock, then proceed; concurrent
ship-vs-ship or ship-vs-cancel serialize on the row lock.

## Synthetic event retirement

`test.synthetic_sale` (schema, rule, registration) is deleted, replaced by
`sales.goods_shipped` in `POSTING_RULES` (debit `5000 COGS` / credit
`1300 Inventory`, amount = payload total cost; both codes join the
documented standard chart). Week 3's posting tests migrate to drive the
real event. Payload: `company_id`, `source_id` (= order id), `order_no`,
`lines: [{product_id, qty, unit_cost}]`, `total_cost`, `shipped_at`.

## Trade-off Analysis

The week's centerpiece is atomic cross-module cooperation without
coupling: sales never imports inventory or ledger, yet a single `POST
/ship` moves stock and books COGS or does neither. Everything chosen here
is the established doctrine applied to a new domain — locks before state
checks, DB-enforced idempotency, append-only facts with rebuildable
projections — deliberately: Week 5 should prove the architecture
generalizes, not invent a new one. Prices paid: standard cost is a
simplification real manufacturers outgrow (Phase 3 exists for that);
full-order shipment dodges the genuinely hard inventory problems
(backorders, partials, returns) — dodged consciously, documented, and
deferred rather than half-built.

## Consequences

- Easier: Week 6 (invoicing → AR) is now "another subscriber on another
  sales event", a proven pattern; the O2C demo can finally show a trial
  balance that moved because goods moved.
- Harder: `standard_cost` must be maintained on products for COGS to mean
  anything (seed data + docs); the shipped state makes sales' state machine
  asymmetric (cancel legality now depends on history).
- Revisit: moving-average/FIFO costing and purchase receipts (Phase 3);
  partial shipments and returns (Phase 2+); adjustment approval workflow
  (Phase 2 platform.workflow).

## Consensus Revisions (review v1, 2026-08-13)

**R1 — Legacy synthetic outbox rows are neutralized in migration 0005
(resolves P2: retired event type poisons replay forever).** 0005 executes
`UPDATE outbox SET dispatched_at = now() WHERE event_type =
'test.synthetic_sale' AND dispatched_at IS NULL` with a comment explaining
why: after the schema registration is deleted, these rows can never be
replayed successfully, only fail eternally. Marking them dispatched is the
honest terminal state for events whose type no longer exists.

**R2 — Rebuild CLI locks before it recomputes (resolves P2: rebuilding
under concurrent shipments corrupts the summary).** `rebuild_stock_summary`
runs per company in ONE transaction: `SELECT ... FOR UPDATE` on ALL of that
company's `stock_summary` rows first, then recompute from `SUM(moves)`,
then write. Any concurrent shipment's own `FOR UPDATE` on a summary row
serializes against the rebuild (before or after, never interleaved). The
CLI additionally prints a warning that running it during heavy write load
will block writers for the duration — correct, and visible.

**R3 — Enum migration discipline (resolves P2: `ALTER TYPE ADD VALUE`
transaction semantics).** Migration 0005's normative constraints, stated in
the migration file itself: (a) the migration may ADD the `SHIPPED` value
but must not reference it in any other statement (Postgres forbids using a
new enum value in the transaction that added it); (b) `downgrade()` is a
documented no-op for the enum value (Postgres cannot drop an enum value
without rebuilding the type; a stray unused value is harmless, a type
rebuild on a populated table is not) — the rest of 0005's objects downgrade
normally.

**P3 (deferred, documented):** `standard_cost` added to masterdata's
Product schemas (additive, low-risk change to Week 1 code); per-line
`unit_cost` + `total_cost` payload redundancy is deliberate (audit
reproducibility from the event alone).

## Action Items

1. [x] `/CODEX REVIEW ARCHITECTURE` — passed v1; 3 P2 resolved above (Consensus Status: APPROVED)
2. [ ] Migration 0005: `stock_moves` + `uq_stock_moves_source`, `stock_summary` (+ CHECK), `products.standard_cost` (NUMERIC(20,6), default 0), `SHIPPED` enum value
3. [ ] `inventory` module: models/service/router — adjustments endpoint, stock query endpoint, the deduction bus handler
4. [ ] `sales.service.ship_order` (R1-locked, flush-only core) + router wrapper + `sales.goods_shipped` schema/publish
5. [ ] Posting: replace synthetic rule with `sales.goods_shipped` (zero-cost skip), delete synthetic artifacts, migrate Week 3 posting tests
6. [ ] `app/cli/rebuild_stock_summary.py` + reconciliation test
7. [ ] Tests: ship happy path (stock down, COGS posted, trial balance moves), insufficient stock aborts everything atomically, concurrent ship-vs-ship and ship-vs-cancel, concurrent deduction race on the same product (two orders, stock for one), replay double-deduction proof, zero-cost skip, adjustment intake + below-zero rejection, rebuild reconciliation, cross-company isolation
