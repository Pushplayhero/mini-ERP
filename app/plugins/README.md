# `app/plugins/` — customize without forking the core

This directory is where mini-erp's "客製不碰核心" (customize without
touching the kernel) thesis becomes real code, per ADR-006. Everything in
here is optional, in the sense that deleting a plugin file and its one
registration line in `app/main.py` removes the customization it provides
and nothing else breaks — the module it customizes never imported it, never
called it directly, and has no idea it existed.

## What a plugin is

A plugin is trusted, admin-installed Python code (no sandbox, no signature
verification — see master-plan §10.3) that hooks into one of two mechanisms
the kernel exposes, both living in `app.core`:

- **`app.core.events`** (ADR-004): "something happened." A plugin can
  `subscribe()` to a durable, replayable fact (e.g. `sales.order_confirmed`)
  and react to it — send a Slack message, call a webhook, write an
  audit-log row. Handlers run synchronously, inside the publisher's
  transaction; an exception aborts that transaction.
- **`app.core.hooks`** (ADR-006): "something is *about* to happen — should
  it be allowed?" A plugin can `register()` on a named hook point (e.g.
  `sales.order.validate_confirm`, exposed as
  `app.modules.sales.service.SALES_ORDER_VALIDATE_CONFIRM`) and veto the
  operation by raising, or let it proceed by returning normally. This is
  never durably recorded and never replayed — see `app/core/hooks.py`'s
  module docstring for why events and hooks are kept as two separate,
  narrowly-scoped mechanisms instead of one.

Both mechanisms are fail-closed: a plugin's exception is never swallowed on
its behalf. If your hook/handler wants to reject a business operation,
raise an `app.core.exceptions.AppError` subclass (e.g. `ConflictError` for
a 409, `DomainValidationError` for a 422) so `app.main`'s existing exception
handlers turn it into the right HTTP status. Raising a bare `Exception` is a
plugin bug, and will surface as a loud 500 — that is intentional; a plugin
crash should never be silently absorbed into a 200.

## What ships here today

- **`credit_limit.py`** — registers on
  `sales.order.validate_confirm`. Blocks a sales order from being confirmed
  if doing so would push the customer's confirmed-order exposure past
  `customer.credit_limit`. `credit_limit == 0` means "don't check." See the
  module's own docstring for the exposure formula and the concurrency
  argument (`SELECT ... FOR UPDATE` on the customer row).

## Writing a new plugin

1. Add a new file under `app/plugins/` (or extend an existing one — nothing
   requires one file per plugin).
2. Import whatever you need — `app.core.*` and any `app.modules.*` are all
   fair game; `app.plugins` is explicitly exempt from the
   business-module-independence `import-linter` contract that keeps
   `sales`/`ledger`/`masterdata`/etc. from importing each other. Prefer
   calling a module's public **service functions** where one exists
   (`app.modules.masterdata.service.get_customer`, etc.) over reaching into
   its ORM models directly — models can change shape between releases in
   ways a plugin author has no visibility into, and a service function is
   the module's actual public contract. Reaching into models (as
   `credit_limit.py` does, for `Customer`/`SalesOrder` — see that module's
   docstring for why) is acceptable when you specifically need something a
   service function does not expose (e.g. `with_for_update()` locking, or a
   raw aggregate query) — just know you are now coupled to that module's
   schema, and a future migration could break you. Enforcing "public
   surface only" is deferred to Phase 2's plugin loader; for now it is
   convention, not physics.
3. Write your handler with the signature the mechanism expects:
   - event subscriber: `async def handler(session: AsyncSession, payload: BaseModel) -> None`
   - hook handler: `async def handler(session: AsyncSession, context: HookContext) -> None`
4. Register it in `app/main.py`, next to the other module wiring:
   `events.subscribe("some.event_type", handler)` or
   `hooks.register("some.hook_point", handler)`.
5. That's it. To remove the customization: delete the registration line (and
   the file, if nothing else uses it). Both `app.core.events` and
   `app.core.hooks` support `unregister(...)` too, if you need to swap a
   handler out at runtime (mainly useful for tests — see
   `tests/sales/test_credit_limit_plugin.py`'s
   `test_removing_the_plugin_allows_confirm_that_would_otherwise_be_blocked`
   for the canonical proof that this decoupling is real, not aspirational).

## What Phase 2 will add (not here yet)

A real plugin *loader* — discovery via entry points, dependency/ordering
declarations between plugins, versioned hook-point compatibility, and
enforcement (not just convention) that a plugin only touches modules'
public surfaces. Week 4 intentionally ships the minimum registry that
proves the dependency direction and nothing more (ADR-006 Decision 1,
Option A's whole argument is that this minimum is what Phase 2's loader
plugs into unchanged).
