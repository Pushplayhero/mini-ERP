# ADR-006: Sales module, minimal hook registry, and the credit-limit plugin

**Status:** Accepted (consensus review v1 passed 2026-08-13; see "Consensus Revisions")
**Date:** 2026-08-13
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)

## Context

Week 4 delivers the first real business module (`sales`: order lifecycle)
and, per master-plan §10.3, the **minimal hook registry** with the
credit-limit check implemented as the first demonstration plugin — the first
proof of the "customize without forking the core" thesis the whole project
is positioned on. Constraints already fixed by prior ADRs: modules stay
independent (import-linter), events go through the ADR-004 bus, money is
NUMERIC(20,6) TWD-only, masterdata values are snapshotted into transactions
(master-plan §2.3), and transaction ownership belongs to whoever opened it
(ADR-003 R1).

Shipping and invoicing are Weeks 5–6; Week 4 orders stop at `confirmed`.
Order confirmation is not an accounting event, so Week 4 publishes an event
with **no posting subscriber** — which also exercises the bus's claim that
posting is just one subscriber among potential many.

## Decision

1. **Hook registry lives in `app/core/hooks.py`**: named hook points
   (strings), sequential in-process execution inside the caller's
   transaction, exceptions propagate (fail-closed). Trust model per
   master-plan §10.3: hooks are admin-installed trusted code, no sandbox.
2. **Plugins live in `app/plugins/`**, a new top-level package that MAY
   import `core` and any business module; business modules and core MUST
   NOT import plugins (two new import-linter contracts). The credit-limit
   check ships as `app/plugins/credit_limit.py`, registered in `app/main.py`.
3. **Order lifecycle is a server-enforced state machine**
   (`draft → confirmed`, `draft/confirmed → cancelled`; `shipped`/`closed`
   reserved for Weeks 5–6), with masterdata snapshots frozen at confirm.
4. **`sales.order_confirmed` is published through the bus** (outbox +
   schema validation, no posting subscriber). The Week 3 synthetic event
   stays until Week 5's first real posting event replaces it.

## Options Considered

### Decision 1 — hook registry shape

**Option A: named hook points in `core/hooks.py`, sync, fail-closed** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — a dict of `hook_name → list[callable]`, mirroring `core/events.py` |
| Consistency | Same in-transaction, fail-closed semantics as the event bus |
| Phase 2 path | Full plugin loader (entry points, dependency mgmt) plugs into this registry unchanged |

Hook naming convention: `"<module>.<entity>.<phase>_<action>"`; Week 4
defines exactly one point: `"sales.order.validate_confirm"`, invoked inside
the confirm transaction before the state transition. Hook signature:
`async (session, context: HookContext) -> None`; raising aborts the confirm.
Hooks that reject business operations must raise `AppError` subclasses so
the existing exception handlers map them to proper HTTP statuses — a plugin
raising a bare `Exception` is a plugin bug and surfaces as a 500, loudly.

**Pros:** one mental model for both extension mechanisms (bus = react to
facts, hooks = veto/augment intentions); nothing new to operate.
**Cons:** synchronous hooks add latency to the hooked operation — acceptable
by the same policy as bus subscribers (consistency-critical only).

**Option B: reuse the event bus for validation ("sales.order.confirming" event)**

**Pros:** one mechanism instead of two.
**Cons:** conflates semantics — events announce facts that DID happen
(and are durably outboxed for external consumers); a validation veto is a
question about something that MIGHT happen and must never appear in the
outbox as if it did. Replaying a "confirming" veto event would be
meaningless-to-harmful. Separate mechanisms keep both contracts honest.

**Option C: defer hooks entirely to Phase 2**

**Pros:** less Week 4 scope.
**Cons:** master-plan §10.3 explicitly commits the minimal registry +
demonstration plugin to Phase 1; the credit check would then be hardcoded in
sales — the exact "posting logic scattered in modules" smell ADR-003
rejected, applied to validation.

### Decision 2 — where plugins live and what they may import

**Option A: `app/plugins/` top-level, plugins import freely, nothing imports plugins** — chosen

**Pros:** plugins are integration points, like `app/main.py` — they *should*
see modules' public services (the credit plugin needs customer credit limits
and order totals); the dependency direction (modules never know plugins
exist) is the entire point, and two new import-linter contracts make it
CI-enforced physics: `app.modules must not import app.plugins` and
`app.core must not import app.plugins`.
**Cons:** a plugin importing module *internals* (models) couples it to
schema churn — mitigated by convention (plugins call service functions /
Core table refs), noted in the plugins README; enforcement of "public
surface only" is deferred to Phase 2's loader.

**Option B: plugins as `app/modules/*` peers**

**Pros:** no new package.
**Cons:** the independence contract would forbid them from importing the
modules they exist to extend — self-defeating; carving exceptions per-plugin
erodes the contract that protects the kernel.

### Decision 3 — order lifecycle and snapshots

**Option A: server-enforced state machine, snapshots frozen at confirm** — chosen

Data model (all money NUMERIC(20,6); audit + custom_data columns as
established):

```
sales_orders(id, company_id, order_no SO-{YYYY}-{NNNNNN}, customer_id,
             status[draft|confirmed|cancelled], currency_code,
             total,                      -- server-computed from lines, never client-supplied
             snapshot_customer_code, snapshot_customer_name,
             confirmed_at, cancelled_at, ...)
sales_order_lines(id, company_id, order_id, line_no, product_id,
                  qty > 0, uom_id, unit_price,
                  snapshot_sku, snapshot_product_name, amount, ...)
```

Rules: drafts are editable (line replacement allowed, totals recomputed
server-side on every change); `confirm` freezes snapshots (copied from
masterdata at confirm time — a draft that sat for a week confirms at
*current* prices unless lines carried manual overrides), runs
`sales.order.validate_confirm` hooks, sets `confirmed_at`; confirmed orders
reject all line/field mutation (409); `cancel` is allowed from draft and
confirmed (Week 4 has no downstream shipment to conflict with — revisit at
Week 5). `order_no` uses a per-company `sales_sequences` counter (same
get-or-create + `FOR UPDATE` pattern as `ledger_sequences`); **gaplessness
is NOT required for orders** (unlike vouchers — cancelled orders keep their
number; gaps from rollbacks are acceptable and documented).

**Option B: append-only order versions (event-sourced orders)**

**Pros:** full history for free.
**Cons:** massively more machinery than Phase 1 needs; audit columns +
immutable-after-confirm covers the actual audit requirement; the ledger is
the system's event-sourced component — orders don't need to be.

### Decision 4 — the confirm event

**Option A: publish `sales.order_confirmed`, no posting subscriber** — chosen

**Pros:** exercises the bus/outbox path with a second real event type and
proves "no subscriber" is a valid, silent configuration (POSTING_RULES
lookup only happens in handlers that are actually subscribed — an event
type with no subscription simply gets outboxed for future integrations);
Week 5's `sales.goods_shipped` then slots into a proven pattern.
**Cons:** outbox accumulates events nothing consumes yet — harmless, and
exactly what the Phase 2 dispatcher will drain.

**Option B: no events until something posts**

**Pros:** minimal.
**Cons:** integrations (Phase 2 webhooks) will want order-confirmed
notifications; recording them durably from day one costs one `publish` call.

## Credit-limit plugin (the demonstration)

`app/plugins/credit_limit.py` registers on `sales.order.validate_confirm`:

- **Exposure formula (Phase 1)**: `SUM(total) of this customer's CONFIRMED
  orders + the order being confirmed ≤ customer.credit_limit`. Cancelled
  orders drop out of the sum automatically. AR balance joins the formula in
  Week 6 (documented revisit).
- **`credit_limit = 0` means "no credit checking"** (unlimited) — explicit,
  documented sentinel; a real zero-credit customer is modeled by a small
  positive limit or a Phase 2 customer-hold flag.
- **TOCTOU**: the plugin takes `SELECT ... FOR UPDATE` on the customer row
  before summing, serializing concurrent confirms for the same customer —
  same doctrine as ADR-005 R4 and §10.5.
- Violation raises `CreditLimitExceededError(ConflictError)` → 409 with the
  computed exposure in the message.

## Trade-off Analysis

The week's real deliverable is the *dependency direction*: sales knows
nothing about credit limits; it fires a hook point and the plugin — living
outside both core and modules, removable by deleting one file and one
registration line — enforces policy. That inversion is what "客製不碰核心"
means concretely, and both new import-linter contracts make regressing it a
CI failure rather than a review catch. Cost accepted: two extension
mechanisms (bus + hooks) to document and keep conceptually distinct
(facts vs. intentions), and hook latency inside business transactions.

## Consequences

- Easier: Week 5/6 modules get validation extension points by naming them;
  Phase 2's loader has a registry to discover into; the README/interview
  story gains its "plugin architecture" chapter with working proof.
- Harder: plugin authors must learn the AppError contract; hook points are
  API surface that needs semver discipline from day one (documented).
- Revisit: exposure formula at Week 6 (AR); cancel-after-confirm semantics
  at Week 5 (shipped orders can't cancel); "public surface only" plugin
  imports at Phase 2.

## Consensus Revisions (review v1, 2026-08-13)

**R1 — State transitions lock the order row (resolves P2: double-confirm
race).** `confirm` and `cancel` begin with `SELECT ... FOR UPDATE` on the
order row, then re-check the current status under the lock; an illegal
current status (already confirmed, already cancelled) returns 409. Two
concurrent confirms therefore serialize: the second sees `confirmed` and
409s — one event, one outbox row, one `confirmed_at`. Same doctrine as
ADR-005 R4; verified by a dedicated two-session concurrency test.

**R2 — Empty or zero-total orders cannot confirm (resolves P2).** Confirm
requires at least one line and `total > 0`; violation is a 422
(`DomainValidationError`) raised before hooks run.

**R3 — Registries must support removal (resolves P2: the plugin-removal
test is unwritable against an append-only registry).** `core/hooks.py`
ships `unregister(hook_name, fn)` and a test-facing `reset()` from day one;
`core/events.py` gets the same additions retrofitted (its append-only
`_handlers`/`_schemas` dicts are a latent test-isolation leak that Week 3
tests dodge via unique naming — fix it while touching the pattern). The
Action Item 7 removal test unregisters the credit-limit plugin and proves
confirm then succeeds — the decoupling demo the ADR promises.

**P3 (deferred, documented):** explicit line-level `unit_price` override
semantics (quoted-price freezing) specified at implementation;
`sales.order_confirmed` payload includes `source_id` (= order id) for shape
consistency with posting events and the replay classifier.

## Action Items

1. [x] `/CODEX REVIEW ARCHITECTURE` — passed v1; 3 P2 resolved above (Consensus Status: APPROVED)
2. [ ] Migration 0004: `sales_orders`, `sales_order_lines`, `sales_sequences` + CHECKs (qty > 0, status enum, one snapshot per confirmed order)
3. [ ] `core/hooks.py`: registry + `HookContext`; two import-linter contracts (`modules ↛ plugins`, `core ↛ plugins`)
4. [ ] `sales` module: schemas/service/router — order CRUD (draft), confirm, cancel, with server-computed totals and snapshot freezing
5. [ ] `plugins/credit_limit.py` + registration in `main.py`
6. [ ] `sales.order_confirmed` event schema + publish on confirm
7. [ ] Tests: lifecycle transitions (legal + illegal), snapshot freezing, server-side total recomputation (client-supplied totals ignored), credit-limit pass/block/sentinel-0/concurrent-confirm race, hook removal test (unregister plugin ⇒ confirm succeeds — proves the decoupling), cross-company isolation, event-on-confirm outbox row
