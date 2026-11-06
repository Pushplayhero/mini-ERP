# `app/plugins/` — customize without forking the core

This directory is where mini-erp's "客製不碰核心" (customize without
touching the kernel) thesis becomes real code, per ADR-006 and ADR-010. Everything in
here is optional, in the sense that removing a plugin declaration and its
manifest registration unplugs the customization it provides
and nothing else breaks — the module it customizes never imported it, never
called it directly, and has no idea it existed.

## What a plugin is

A plugin is trusted, admin-installed Python code (no sandbox, no signature
verification — see master-plan §10.3 and ADR-010) that hooks into kernel mechanisms
exposed in `app.core`:

- **`app.core.events`** (ADR-004): "something happened." A plugin can
  subscribe to a durable, replayable fact (e.g. `sales.order_confirmed`)
  and react to it — send a Slack message, call a webhook, write an
  audit-log row. Handlers run synchronously, inside the publisher's
  transaction; an exception aborts that transaction.
- **`app.core.hooks`** (ADR-006, ADR-010): "something is *about* to happen — should
  it be allowed?" A plugin can bind to a versioned hook point (e.g.
  `sales.order.validate_confirm`, exposed as
  `app.modules.sales.service.SALES_ORDER_VALIDATE_CONFIRM`) and veto the
  operation by raising, or let it proceed by returning normally. This is
  never durably recorded and never replayed.

Both mechanisms are fail-closed: a plugin's exception is never swallowed on
its behalf. If your hook/handler wants to reject a business operation,
raise an `app.core.exceptions.AppError` subclass (e.g. `ConflictError` for
a 409, `DomainValidationError` for a 422) so `app.main`'s existing exception
handlers turn it into the right HTTP status. Raising a bare `Exception` is a
plugin bug, and will surface as a loud 500 — that is intentional; a plugin
crash should never be silently absorbed into a 200.

## What ships here today

- **`credit_limit.py`** — declares `PLUGIN_MANIFEST` registered on
  `sales.order.validate_confirm`. Blocks a sales order from being confirmed
  if doing so would push the customer's confirmed-order exposure past
  `customer.credit_limit + config.grace_amount`. `credit_limit == 0` means "don't check."
  Configured per-tenant via `CreditLimitPluginConfig(grace_amount)`.
  See the module docstring for the exposure formula and concurrency argument.

## Writing a new plugin (ADR-010)

1. Add a new file under `app/plugins/` (or package as an installed Python entry point
   under the group `"mini_erp.plugins"`).
2. **Public surface only (CI-enforced by import-linter)**:
   Plugins may import `app.core.*` and business module public service layers
   (`app.modules.<module>.service`, `app.modules.<module>.schemas`).
   Importing internal ORM models (`app.modules.<module>.models`) or routers
   (`app.modules.<module>.router`) is strictly forbidden by import-linter.
   Intra-transaction helpers that run under an already-authorized business action
   use `*_unchecked` service helpers that enforce tenant isolation (`require_current_company_id()`).
3. Define an optional `config_schema` using Pydantic `BaseModel` with `extra="forbid"`.
   The schema must be viable with `{}` defaults (`model_validate({})` succeeds).
4. Declare `PLUGIN_MANIFEST = PluginManifest(...)`:
   - `name`: unique identifier (`^[a-z][a-z0-9_]{1,99}$`)
   - `version`: SemVer `X.Y.Z`
   - `title`, `description`
   - `default_enabled`: whether newly created tenants run this by default
   - `requires`: tuple of `PluginDependency` prerequisites
   - `config_schema`: optional Pydantic model
   - `hooks`: tuple of `HookBinding(hook_name, required_version, handler)`
   - `events`: tuple of `EventBinding(event_type, handler)`
5. Register built-in manifests in `app/main.py` via `BUILTIN_PLUGIN_MANIFESTS` passed to
   `plugins_loader.bootstrap_plugins(...)`.
