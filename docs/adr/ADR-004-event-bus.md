# ADR-004: Event bus — synchronous in-process dispatch, outbox, and event contracts

**Status:** Accepted (consensus review v1 passed 2026-08-13; see "Consensus Revisions")
**Date:** 2026-08-13
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

> One of the four ADR topics reserved in `docs/open-erp-master-plan.md` §6.
> Companion: ADR-003 (posting engine). Together they define the Week 3
> deliverable.

## Context

Business modules need to announce domain events ("goods shipped") that other
parts of the system react to — primarily the posting engine (ADR-003), later
webhooks/integrations (Phase 2 via the outbox dispatcher). Master-plan §2.6
already commits to the outbox pattern with §10.4's schema and delivery
semantics (at-least-once, idempotent consumers, the `outbox` table has
existed since Week 1). What remains open: dispatch semantics of the
*in-process* bus, error handling, handler ordering, and how event contracts
work without violating the import-linter independence contract between
business modules (ledger must react to sales' events without importing
sales).

## Decision

1. **Synchronous, in-process, same-transaction dispatch.** `publish(event,
   session)` runs every subscribed handler inline, in the caller's DB
   transaction. Any handler exception propagates and aborts the whole
   transaction — fail-closed, matching master-plan §2.6's "交易一致性優先".
2. **Every published event is also written to `outbox` in the same
   transaction** (payload = event type + JSON payload). Week 3 ships the
   write side plus a replay CLI; the async dispatcher stays Phase 2.
3. **Event contracts are `event_type` strings + JSON-serializable payload
   dicts, validated at publish time against a schema the publisher registers**
   — no cross-module Python imports anywhere in the event path.

## Options Considered

### Decision 1 — dispatch semantics

**Option A: sync, in-process, same transaction** — chosen

| Dimension | Assessment |
|-----------|------------|
| Consistency | Business write + journal entry + outbox row: one atomic commit |
| Complexity | ~100 lines; no broker, no worker, no retry machinery |
| Failure model | One rule: everything commits or nothing does |

**Pros:** the O2C invariant ("no shipment without its COGS entry") is a
transaction property, not an eventual-consistency hope; trivially testable;
this is the exact architecture master-plan §2.6 and §4 committed to, with the
outbox as the pre-built escape hatch to async later.
**Cons:** handler latency adds to request latency (fine: posting is a few
inserts); a slow future handler could bloat transactions — mitigated by
policy: *only* consistency-critical handlers (posting) may subscribe
in-process; everything else must consume from the outbox (Phase 2).

**Option B: async task queue / message broker now**

**Pros:** decoupled latency; retries for free.
**Cons:** "no entry without its shipment" becomes reconciliation instead of a
guarantee — the exact failure mode this project exists to demonstrate
avoiding; a broker is heavy operational baggage for a one-developer Phase 1;
master-plan §2.6 explicitly deferred this, and the outbox already reserves
the migration path.

### Decision 2 — outbox write placement

**Option A: bus writes outbox on every publish** — chosen

**Pros:** single choke point — publishers cannot forget the outbox write;
in-process consumers and future async consumers see the identical event
stream; replay CLI (reads outbox, re-dispatches) combined with ADR-003's
idempotent posting gives safe at-least-once semantics end to end, satisfying
§10.4 with machinery that exists this week.
**Cons:** consistency-critical events are "delivered twice" (inline + outbox
for later consumers) — by design; idempotency (ADR-003 Decision 3) makes the
duplicate path harmless, and the replay test proves it.

**Option B: publishers write outbox themselves when they care**

**Pros:** marginally less magic.
**Cons:** opt-in durability guarantees are forgotten durability guarantees;
divergence between what in-process handlers saw and what the outbox recorded
is unauditable after the fact.

### Decision 3 — event contracts across module boundaries

**Option A: `event_type` string + registered payload schema** — chosen

```python
# publisher (e.g. sales, Week 4), at import time:
bus.register_event("sales.goods_shipped", GoodsShippedPayload)  # pydantic
# publisher, at business time (same transaction):
await bus.publish(session, "sales.goods_shipped", payload_dict)
# consumer (ledger), at startup — knows the STRING, not the module:
bus.subscribe("sales.goods_shipped", posting_handler)
```

| Dimension | Assessment |
|-----------|------------|
| Boundary integrity | Zero cross-module imports; import-linter contracts untouched |
| Type safety | Publish-time validation against the registered pydantic schema |
| Evolution | Payload schema changes are caught at publish, not deep in a handler |

**Pros:** consumers depend on the event *name and shape*, not on the
publisher's Python package — the same decoupling the outbox JSON payload
needs anyway (an async consumer in Phase 2 gets JSON, not Python objects), so
the in-process contract and the durable contract are one and the same;
unknown-event publishes and unregistered-schema publishes fail loudly.
**Cons:** no static (mypy-level) coupling between publisher and consumer —
mitigated by publish-time schema validation plus integration tests that
exercise real event flows; string typos surface at test time.

**Option B: shared `app/contracts/` package importable by all modules**

**Pros:** mypy-checked payload classes end to end.
**Cons:** requires a new import-linter carve-out ("everyone may import
contracts") — a boundary exception that history says grows; contracts package
becomes a coupling magnet; and the durable/JSON path still needs the
string+schema form, so Option B builds *both* representations.

**Option C: consumers import publishers' `events.py` directly**

**Pros:** simplest to type.
**Cons:** breaks the module-independence contract outright; ledger would
import sales — rejected on the same grounds as ADR-003 Option C.

## Trade-off Analysis

The bus is deliberately boring: a dict of `event_type → list[handler]`, a
pydantic-validation gate, and an outbox INSERT. The interesting guarantees
live at the edges — atomicity from Decision 1, durability-without-divergence
from Decision 2, boundary integrity from Decision 3 — and each is enforced
structurally (transaction scope, single choke point, no imports to violate)
rather than by convention. What is given up: static typing across the
publish/subscribe seam, and any parallelism in event handling. Both are the
right price at Phase 1 scale; the second is exactly what the outbox +
Phase 2 dispatcher is reserved to buy back.

Handler ordering (normative, recorded to close ADR-005's precedent of
leaving nothing implicit): handlers run in subscription order, which is
deterministic because subscription happens once at app startup
(`app/main.py` wiring); Week 3 has exactly one subscriber (posting), so
ordering is future-proofing, not a live concern.

## Consequences

- Easier: Week 4+ modules emit events with one call and get atomic
  accounting + durable outbox recording; Phase 2's dispatcher consumes a
  stream that has been accumulating correctly since Week 3; integration
  tests can assert on outbox contents as an audit of "what happened".
- Harder: no static type link between publisher and consumer (tests carry
  that weight); in-process subscription is a privilege that must stay
  restricted to consistency-critical handlers (documented policy, revisit as
  a lint rule if it's ever violated).
- Revisit: async dispatcher + webhook delivery (Phase 2,
  `platform.integration`); handler-ordering guarantees if a second
  in-process subscriber ever appears.

## Consensus Revisions (review v1, 2026-08-13)

**R1 — Replay dispatches to handlers directly, never through `publish()`
(resolves P2: publish always writes outbox, so replaying via publish would
duplicate every replayed row — one replay doubles the queue).** The replay
CLI reads outbox rows and invokes the subscribed handlers for each
`event_type` directly, bypassing the publish choke point. Each row is
processed in its own transaction (open → bind company context → dispatch →
commit), so one poison row aborts only itself, and ADR-003's idempotent
posting makes re-processing previously-succeeded rows a no-op.

**R2 — `company_id` is a mandatory field in every registered event schema.**
`register_event` rejects any payload schema lacking a `company_id: UUID`
field. Inline dispatch inherits the request's tenancy context as before (and
the bus asserts payload `company_id` matches the active context — a
mismatch is a bug and raises); replay establishes context per row via
`company_context(payload["company_id"])`. Without this, any handler touching
tenant-scoped data would hit the fail-closed `TenancyContextError` the
moment it ran outside a request (resolves P2).

**R3 — Handler exceptions during replay** follow R1's per-row transaction
scoping: log, count, continue to the next row; a summary line reports
succeeded/skipped/failed. (Inline dispatch is unchanged: exception = abort
the publisher's transaction, fail-closed.)

## Action Items

1. [x] Consensus gate shared with ADR-003 — passed v1 (Consensus Status: APPROVED)
2. [ ] `core/events.py`: `EventBus` (register_event / subscribe / publish), publish-time schema validation, outbox write in caller's session
3. [ ] Replay CLI (`python -m app.cli.replay_outbox`): re-dispatch outbox rows through the bus; safe because posting is idempotent (ADR-003)
4. [ ] Tests: publish validates schema, handler exception aborts the caller's transaction (business write rolls back too), outbox row written atomically with business data, replay produces zero duplicate entries, unknown event type / unregistered schema fail loudly
5. [ ] Document the in-process-subscriber policy (consistency-critical only) in the module README
