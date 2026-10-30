# ADR-010: `platform.plugins` — plugin loader, tenant enablement governance, and public-surface isolation

**Status:** **ACCEPTED** — Architecture consensus review APPROVED, round 2 (2026-10-30). Round 1 identified 6 findings (3 High / P1, 3 Medium / P2), all resolved in Round 2 (`0 Findings (APPROVED)`) — see "Consensus Revisions" for the round-by-round history. Implementation may begin per Action Item 1 onward.
**Date:** 2026-10-30
**Deciders:** Ryan (project owner), architecture review gate
**Depends on:**
- `docs/adr/PHASE2-platform-architecture-overview.md` §2.2, §4, §5.1, §7.2 Q4 (ACCEPTED)
- `docs/open-erp-master-plan.md` §2.5, §3, §10.3
- `docs/adr/ADR-004-event-bus.md` (ACCEPTED)
- `docs/adr/ADR-006-sales-and-hook-registry.md` (ACCEPTED)
- `docs/adr/ADR-008-receivables.md` Decision 6 (ACCEPTED)
- `docs/adr/ADR-009-platform-permissions.md` Decisions 4, 7, 9, 10 (ACCEPTED)

> Numbering note: ADR-010 is the second Phase 2 ADR, immediately following
> ADR-009 (`platform.permissions`) per `docs/adr/PHASE2-platform-architecture-overview.md`
> §7.2 Q4 ("Option A — `platform.permissions` first; `platform.plugins` is
> second").

---

## Context

Phase 1 delivered the minimal hook registry (`app/core/hooks.py`) and one
demonstration plugin (`app/plugins/credit_limit.py`) under ADR-006, later
updated in ADR-008 Decision 6 to include open invoice balances in customer
credit exposure. That Phase 1 slice intentionally deferred five structural
capabilities to Phase 2's `platform.plugins` module:

1. **No formal plugin manifest, discovery protocol, or dependency ordering.**
   Today `app/main.py` wires `credit_limit.check_credit_limit` manually with a
   single `hooks.register(SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)`
   call. There is no `PluginManifest` contract, no Python entry-point discovery
   (`importlib.metadata.entry_points`), no inter-plugin dependency graph
   (`requires`), no cycle detection, and no deterministic topological ordering
   when multiple plugins bind to the same hook or event.
2. **No SemVer hook-contract verification or startup event-type validation.**
   `docs/open-erp-master-plan.md` §10.3 specifies that hook points are
   versioned contracts and that the plugin loader must verify compatibility at
   startup. Currently, hook names are unversioned strings and `events.subscribe`
   accepts arbitrary `event_type` strings without verifying that a corresponding
   event schema has been registered in `app.core.events`.
3. **No separation between deploy-time installation and tenant-scoped runtime
   enablement/configuration (`PHASE2-platform-architecture-overview.md` §2.2).**
   Today every company runs `credit_limit` unconditionally with hardcoded
   parameters. There is no `company_plugins` table, no per-company enable/disable
   switch, no validated per-company `config JSONB`, and no RBAC-protected
   management API (`/api/v1/plugins`).
4. **Cross-module ORM model reach-in by plugins (`ADR-006` Decision 2 technical
   debt).** `app/plugins/credit_limit.py` currently imports
   `app.modules.masterdata.models.Customer`,
   `app.modules.sales.models.SalesOrder`, and
   `app.modules.receivables.models.Invoice` directly. ADR-006 Decision 2
   documented this as a Phase 1 shortcut and explicitly charged Phase 2's
   plugin architecture with enforcing a **public-surface-only** boundary via
   `import-linter`.
5. **Dependency inversion and non-HTTP execution compatibility.**
   `app.core` and `app.modules` are forbidden by `import-linter` from importing
   `app.platform` or `app.plugins`. Moreover, hooks (`hooks.run`) and domain
   events (`events.publish`, `events.redispatch`) execute inside business
   transactions where the active actor holds business permissions (such as
   `sales.order.confirm`, not `platform.plugin.read`) or inside
   `app/cli/replay_outbox.py` (which is exempt from `ActorContext` under
   ADR-009 Decision 10 Rule 10a). The runtime tenant gate and any internal
   service helpers called by plugins must honor fail-closed tenancy
   (`require_current_company_id()`) without imposing management-permission checks
   on business/replay callers.

---

## Decision

### 1. Two-boundary governance model: deploy-time discovery vs. company-scoped runtime state

Per `docs/adr/PHASE2-platform-architecture-overview.md` §2.2 and
`docs/open-erp-master-plan.md` §10.3, `platform.plugins` enforces two distinct
boundaries:

1. **Operator / deploy-time boundary (outside application RBAC):**
   - Plugins are trusted Python code installed into the runtime environment by
     the system operator (Odoo trust model — in-process execution, no untrusted
     code sandbox, and **never** an HTTP endpoint that uploads or executes
     arbitrary code).
   - At application startup, `app.platform.plugins.loader` discovers plugin
     manifests from two deterministic sources:
     1. **Built-in manifests** passed explicitly by the composition root
        (`app.main`), starting with `(credit_limit.PLUGIN_MANIFEST,)`.
     2. **Installed package entry points** registered under the Python
        entry-point group `"mini_erp.plugins"` (queried via
        `importlib.metadata.entry_points(group="mini_erp.plugins")`), gated by
        `settings.enable_plugin_entrypoints: bool = True`.
   - Operator escape hatch: `settings.disabled_plugins: list[str] = []`
     (configured via `MINI_ERP_DISABLED_PLUGINS`) allows operators or test
     harnesses to exclude named plugins from discovery at startup without
     rebuilding the container image. If an active non-disabled plugin depends on
     a plugin listed in `disabled_plugins`, startup fails closed with
     `PluginLoadError`.
2. **Company-scoped runtime boundary (governed by `platform.permissions` RBAC):**
   - Among the plugins discovered and validated at startup, each company
     independently controls whether a plugin is enabled (`is_enabled: bool`) and
     what validated configuration parameters (`config: dict[str, Any]`) apply
     to that company via `company_plugins`, guarded by `platform.plugin.read`
     and `platform.plugin.manage`.

---

### 2. Port-and-Adapter split (`app.core.plugins`), SemVer hook contracts, and deterministic topological loading

To prevent circular or forbidden imports between `app.plugins` and
`app.platform.plugins`, the declarative plugin contract and runtime context
carrier live in **`app/core/plugins.py`** (the core port, analogous to
`app/core/authorization.py` in ADR-009), while discovery, topological sorting,
persistence, and management APIs live in **`app/platform/plugins/`**.

#### 2.1 Versioned hook points in `app.core.hooks`
- `app.core.hooks` adds a hook-point specification registry:
  - `define_hook_point(hook_name: str, version: str) -> None` records the
    authoritative SemVer (`X.Y.Z`) of a core/module hook point in `_hook_specs`.
  - `get_hook_point_version(hook_name: str) -> str | None` and
    `defined_hook_points() -> Mapping[str, str]` expose read-only views for the
    startup loader.
  - `app.modules.sales.service` registers `SALES_ORDER_VALIDATE_CONFIRM` at
    version `"1.0.0"` via `hooks.define_hook_point(SALES_ORDER_VALIDATE_CONFIRM, "1.0.0")`.
- `hooks.reset()` resets handler registrations (`_handlers.clear()`) while
  preserving module-import-time hook point definitions (`_hook_specs`), so test
  suites that call `hooks.reset()` do not erase static hook specifications.

#### 2.2 Subscribed event-type verification in `app.core.events`
- `app.core.events` adds a read-only inspection helper
  `registered_event_types() -> frozenset[str]` returning `frozenset(_schemas)`.
- During startup validation, every `EventBinding.event_type` declared by a
  plugin must exist in `events.registered_event_types()`, or `loader` raises
  `PluginLoadError`.

#### 2.3 `PluginManifest` and SemVer compatibility rules
`app/core/plugins.py` defines the immutable manifest contract:

```python
@dataclass(frozen=True)
class PluginDependency:
    plugin_name: str
    min_version: str  # Caret-compatible SemVer requirement, e.g. "1.0.0"

@dataclass(frozen=True)
class HookBinding:
    hook_name: str
    required_version: str  # Caret-compatible SemVer requirement against hook point
    handler: HookHandler

@dataclass(frozen=True)
class EventBinding:
    event_type: str
    handler: EventHandler

@dataclass(frozen=True)
class PluginManifest:
    name: str  # Must match ^[a-z][a-z0-9_]{1,99}$
    version: str  # Strict SemVer X.Y.Z
    title: str
    description: str
    default_enabled: bool = False
    requires: tuple[PluginDependency, ...] = ()
    config_schema: type[BaseModel] | None = None
    hooks: tuple[HookBinding, ...] = ()
    events: tuple[EventBinding, ...] = ()
```

- **SemVer compatibility rule (`is_semver_compatible(actual: str, required: str) -> bool`):**
  - Both strings must parse as strict `MAJOR.MINOR.PATCH` non-negative integers
    (malformed versions raise `PluginLoadError`).
  - For `actual.major >= 1`: compatible iff `actual.major == required.major` and
    `(actual.minor, actual.patch) >= (required.minor, required.patch)`.
  - For `actual.major == 0` (initial development): compatible iff
    `actual.major == 0 and required.major == 0 and actual.minor == required.minor and actual.patch >= required.patch`.

#### 2.4 Deterministic topological sort and startup inductive invariants
At startup, `app.platform.plugins.loader.resolve_and_validate_plugins` enforces:

1. **Unique plugin names, no intra-manifest duplicate bindings, & unique handler ownership:**
   - Duplicate `manifest.name` values raise `PluginLoadError`.
   - Duplicate `(hook_name, handler)` or `(event_type, handler)` bindings within
     the same `PluginManifest` raise `PluginLoadError`.
   - Two manifests in the active set sharing the exact same handler function
     object (`id(handler)` collision), or a handler already stamped with a
     different `__plugin_name__`, raise `PluginLoadError`.
2. **Hook & Event contract verification:**
   - Every `HookBinding.hook_name` must be defined in `hooks.defined_hook_points()`
     and satisfy `is_semver_compatible(actual_hook_version, binding.required_version)`.
   - Every `EventBinding.event_type` must exist in `events.registered_event_types()`.
3. **Dependency existence & SemVer check:** Every `dep` in `manifest.requires`
   must reference a discovered `PluginManifest` whose `version` satisfies
   `is_semver_compatible(target.version, dep.min_version)`.
4. **Deterministic Kahn topological sort with lexicographical tie-breaking:**
   - Directed edges run `prerequisite -> dependent`.
   - Zero-in-degree nodes are popped from a min-heap (`heapq`) keyed by
     `manifest.name`.
   - Prerequisites are therefore guaranteed to be registered (and executed on
     shared hooks/events) strictly before their dependents, and independent
     plugins always execute in canonical alphabetical order by `manifest.name`.
   - If visited count `< len(manifests)`, a dependency cycle exists and startup
     fails closed with `PluginLoadError`.
5. **Inductive Invariant A — Upward-closed `default_enabled`:**
   - For every plugin $B$ with `default_enabled=True`, every prerequisite
     $A \in B.\text{requires}$ must also declare `default_enabled=True`.
   - Otherwise startup raises `PluginLoadError`. This guarantees that for any
     brand-new company (zero rows in `company_plugins`), the initial effective
     state satisfies the dependency invariant.
6. **Inductive Invariant B — Unconditional default configuration viability:**
   - For **every** plugin where `manifest.config_schema is not None` (regardless
     of `default_enabled`):
     - `manifest.config_schema` must be a `pydantic.BaseModel` subclass with
       `model_config.get("extra") == "forbid"`.
     - `manifest.config_schema.model_validate({})` must succeed at startup,
       producing `manifest.default_config_dict = model.model_dump(mode="json")`
       (when `config_schema is None`, `manifest.default_config_dict` is `{}`).
     - If `{}` fails validation against `config_schema`, startup raises
       `PluginLoadError`. This ensures every plugin always has a canonical,
       schema-valid default configuration for `GET /api/v1/plugins`, first-time
       partial `PATCH` upserts (`{"is_enabled": true}` with `config=None`), and
       unconfigured companies.

---

### 3. Tenant plugin state (`company_plugins`), runtime dependency invariant, and write-skew serialization

#### 3.1 Database schema (Alembic Migration `0011`)
Migration `0011` creates `company_plugins` (inheriting `TenantScopedMixin` and
`TimestampAuditMixin` from `app/core/tenancy.py` and `app/core/db.py`):

- `id`: `UUID` primary key (`default=uuid.uuid4`)
- `company_id`: `UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT` (indexed; matches `TenantScopedMixin` in `app/core/tenancy.py` line 119 and migrations `0004`, `0008`, `0009`)
- `plugin_name`: `VARCHAR(100) NOT NULL`
- `is_enabled`: `BOOLEAN NOT NULL`
- `config`: `JSONB NOT NULL DEFAULT '{}'::jsonb`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at`: `TIMESTAMPTZ NOT NULL DEFAULT now()`
- `created_by`: `UUID NULL` (required by `TimestampAuditMixin` in `app/core/db.py` lines 59–62)
- `updated_by`: `UUID NULL` (required by `TimestampAuditMixin` in `app/core/db.py` lines 59–62)
- Unique constraint: `uq_company_plugins_company_plugin UNIQUE (company_id, plugin_name)`

#### 3.2 Default-state fallback and partial-`PATCH` merge semantics
A company is not required to pre-populate rows in `company_plugins` when
created.
- **Read fallback:** When `(company_id, plugin_name)` has no row in
  `company_plugins`, its effective state is:
  - `is_enabled` = `manifest.default_enabled`
  - `config` = `manifest.default_config_dict` (`{}` when `config_schema is None`,
    or `config_schema.model_validate({}).model_dump(mode="json")` when
    `config_schema is not None`).
- **Write (`PATCH`) merge semantics:** When `update_company_plugin` processes
  `CompanyPluginUpdate(is_enabled: bool | None = None, config: dict[str, Any] | None = None)`:
  - If a `company_plugins` row already exists for `(company_id, plugin_name)`:
    omitted (`None`) `is_enabled` preserves `row.is_enabled`; omitted (`None`)
    `config` preserves `row.config` (re-validated against `manifest.config_schema`).
  - If no `company_plugins` row exists yet for `(company_id, plugin_name)`:
    omitted (`None`) `is_enabled` defaults to `manifest.default_enabled`;
    omitted (`None`) `config` defaults to `manifest.default_config_dict` (never
    an unvalidated raw `{}` when `config_schema` defines defaults such as
    `{"grace_amount": "0.000000"}`).

Because `credit_limit` declares `default_enabled=True` and
`CreditLimitPluginConfig(grace_amount=Decimal("0.000000"))`, every existing and
newly created company automatically enforces `credit_limit` with zero grace
amount out of the box—preserving 100% backward compatibility with Phase 1 and
Week 9 tests—while allowing any company to disable `credit_limit` or configure a
positive `grace_amount` via `PATCH /api/v1/plugins/credit_limit`.

#### 3.3 Runtime dependency invariant and concurrency serialization
For any company $C$ and loaded plugin graph $G$:

$$\forall B \in G:\quad \text{effective\_enabled}(C, B) \implies \forall A \in B.\text{requires}:\ \text{effective\_enabled}(C, A)$$

To prevent `READ COMMITTED` write-skew where concurrent requests simultaneously
disable prerequisite $A$ and enable dependent $B$ for the same company (and
because `company_plugins` rows may not exist yet prior to first upsert):
1. `platform.plugins.service.update_company_plugin` **first** locks the tenant
   root row via `await masterdata_service.lock_company_unchecked(session, company_id)`
   (`SELECT * FROM companies WHERE id = :company_id FOR UPDATE`), serializing
   all plugin state mutations for company $C$.
2. It then loads all existing `company_plugins` rows for company $C$ and
   computes the post-mutation effective state across all loaded plugins:
   - **Enable check:** If target plugin $B$ will have `effective_enabled == True`
     and any $A \in B.\text{requires}$ has `effective_enabled == False`, raise
     `DomainValidationError` (`422`).
   - **Disable check:** If target plugin $A$ will have `effective_enabled == False`
     and any loaded plugin $B$ with $A \in B.\text{requires}$ currently has
     `effective_enabled == True`, raise `ConflictError` (`409`).

---

### 4. Dependency-inverted runtime gate and non-wrapping handler identity

#### 4.1 Why handler wrapping is rejected
In `tests/sales/test_credit_limit_plugin.py` (ADR-006's decoupling proof), test
code calls:
```python
hooks.unregister(sales_service.SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)
try:
    ...
finally:
    hooks.register(sales_service.SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)
```
If the plugin loader wrapped `credit_limit.check_credit_limit` in an anonymous
closure before passing it to `hooks.register`, then:
1. `hooks.unregister(..., credit_limit.check_credit_limit)` would fail to match
   the wrapper by function identity unless special-cased; and
2. `hooks.register(..., credit_limit.check_credit_limit)` in the `finally` block
   would re-register the **unwrapped** function, permanently stripping the
   tenant-enablement gate for all subsequent tests in the process!

#### 4.2 Safe `__plugin_name__` stamping + `PluginRuntimeGate` port in `app.core.plugins`
Instead of wrapping handlers:
1. During `PluginManifest.__post_init__` (and re-verified across manifests in
   `resolve_and_validate_plugins`), each bound handler in `self.hooks` and
   `self.events` is checked first:
   ```python
   existing = getattr(binding.handler, "__plugin_name__", None)
   if existing is not None and existing != self.name:
       raise PluginLoadError(
           f"handler {binding.handler!r} is already bound to plugin {existing!r} "
           f"and cannot be shared with plugin {self.name!r}"
       )
   setattr(binding.handler, "__plugin_name__", self.name)
   ```
   Checking `existing` *before* calling `setattr` ensures a conflicting manifest
   never overwrites an existing plugin's `__plugin_name__` attribute in place.
2. `app.platform.plugins.loader.bootstrap_plugins` registers the original
   `binding.handler` directly via `hooks.register(binding.hook_name, binding.handler)`
   and `events.subscribe(binding.event_type, binding.handler)`.
3. `app.core.plugins` holds the runtime gate port:
   ```python
   PluginRuntimeGate = Callable[
       [AsyncSession, str, Callable[[], Awaitable[None]]],
       Awaitable[None],
   ]
   ```
   wired once at startup by `app.main` to
   `platform.plugins.service.execute_with_plugin_gate_unchecked`.
4. In `app.core.hooks.run(hook_name, session, context)` and
   `app.core.events._dispatch(session, event_type, payload_model)`:
   - For each registered `handler`:
     - Read `plugin_name = getattr(handler, "__plugin_name__", None)`.
     - If `plugin_name is None` (or no runtime gate is wired, e.g. isolated unit
       tests of `app.core.hooks` / `app.core.events`), call
       `await handler(session, ...)` directly with **zero DB queries**.
       Core domain-event subscribers such as `inventory_service.handle_goods_shipped`
       or `ledger_posting.make_posting_handler(...)` have no `__plugin_name__`
       and therefore always execute unconditionally with zero plugin-table
       overhead.
     - If `plugin_name is not None` and the runtime gate is wired, invoke
       `await gate(session, plugin_name, lambda h=handler: h(session, ...))`
       (binding `h=handler` explicitly to avoid loop-variable capture `B023`).
5. Inside `execute_with_plugin_gate_unchecked(session, plugin_name, invoke)`:
   - Resolves `(is_enabled, validated_config_model)` for
     `(require_current_company_id(), plugin_name)` without checking
     `platform.plugin.read` (since callers are business operations or
     `replay_outbox.py`), where `validated_config_model` is the validated
     `BaseModel` instance produced by `manifest.config_schema.model_validate(raw_config)`
     (or `None` when `manifest.config_schema is None`).
   - If `not is_enabled`: returns immediately (`None`).
   - If `is_enabled`: enters `with plugin_config_context(validated_config_model):`
     and awaits `invoke()`.

---

### 5. Public-surface-only plugin boundary and `credit_limit.py` refactor (Option A)

#### 5.1 Internal `_unchecked` public service helpers
Per ADR-009 Decision 7 (the Slice D checked/unchecked service-split doctrine),
intra-transaction helpers invoked by a plugin after the entry-point service
(such as `sales.service.confirm_order`) has already authorized the business
action must use `*_unchecked` functions that do **not** call
`require_permission(...)` (a user confirming a sales order holds
`sales.order.confirm`, not necessarily `masterdata.customer.read` or
`receivables.invoice.read`), while **strictly enforcing fail-closed tenancy**
via `require_current_company_id()`:

1. **`app.modules.masterdata.service.lock_company_unchecked(session: AsyncSession, company_id: uuid.UUID) -> Company`:**
   - Executes `SELECT * FROM companies WHERE id = :company_id FOR UPDATE`,
     raising `NotFoundError("Company", company_id)` if absent.
2. **`app.modules.masterdata.service.lock_customer_credit_limit_unchecked(session: AsyncSession, customer_id: uuid.UUID) -> Decimal`:**
   - Calls `require_current_company_id()` and executes
     `select(Customer.credit_limit).where(Customer.id == customer_id).with_for_update()`
     (automatically tenant-scoped by `TenantScopedMixin` + `do_orm_execute`).
   - Raises `DomainValidationError(f"customer {customer_id} not found or does not belong to this company")`
     if no row matches, preserving the exact error contract of Phase 1's
     `credit_limit.py`.
3. **`app.modules.receivables.service.get_customer_exposure_unchecked(session: AsyncSession, customer_id: uuid.UUID) -> CustomerExposure`:**
   - Returns `CustomerExposure(uninvoiced_orders_total: Decimal, open_invoices_total: Decimal)`
     (defined in `app.modules.receivables.schemas` and re-exported by
     `app.modules.receivables.service`).
   - Calls `company_id = require_current_company_id()`.
   - Executes the single-`SELECT` dual-scalar-subquery snapshot from ADR-008 R2
     using `receivables.service._SALES_ORDERS` (Core table reference) and
     `Invoice` (ORM model).
   - **Critical tenancy & PostgreSQL type invariants on `_SALES_ORDERS`:**
     1. In `app/modules/receivables/service.py`, `_SALES_ORDERS` must type
        `sa_column("total", AMOUNT)` so `func.coalesce(func.sum(_SALES_ORDERS.c.total), _ZERO)`
        preserves `NUMERIC(20, 6)` scale.
     2. Because `sales_orders.status` in PostgreSQL (migration `0004`) is a
        native PostgreSQL `ENUM` (`sales_order_status`) whose labels are
        **uppercase** `'CONFIRMED'` and `'SHIPPED'`, comparing an untyped
        `sa_column("status")` against strings under `asyncpg` raises
        `UndefinedFunctionError: operator does not exist: sales_order_status = character varying`.
        The subquery therefore filters status via
        `sa_cast(_SALES_ORDERS.c.status, String).in_(("CONFIRMED", "SHIPPED"))`
        (matching the `status::text` cast pattern in `app/modules/sales/models.py`).
     3. Because `_SALES_ORDERS` is a bare `sqlalchemy.table("sales_orders", ...)`
        Core construct and is **not** intercepted by ORM
        `with_loader_criteria(TenantScopedMixin, ...)`, all three query scopes
        (the uninvoiced orders subquery, its nested `select(Invoice.order_id)`
        exclusion subquery, and the open invoices subquery) **must** explicitly
        include `_SALES_ORDERS.c.company_id == company_id` and
        `Invoice.company_id == company_id`.

#### 5.2 Refactoring `app/plugins/credit_limit.py` with `CreditLimitPluginConfig` (Option A)
`app/plugins/credit_limit.py` is refactored so that it imports **only**
`app.core.*`, `app.modules.masterdata.service`, and
`app.modules.receivables.service` (zero imports of `*.models` or `*.router`):

- Defines `CreditLimitPluginConfig(BaseModel)`:
  ```python
  class CreditLimitPluginConfig(BaseModel):
      model_config = ConfigDict(extra="forbid")

      grace_amount: Decimal = Decimal("0.000000")

      @field_validator("grace_amount", mode="before")
      @classmethod
      def _validate_grace_amount(cls, v: Any) -> Decimal:
          # Rejects float/bool, enforces >= 0, and rounds HALF_EVEN to 6 decimal places
          ...
  ```
- Declares `PLUGIN_MANIFEST = PluginManifest(...)`:
  - `name="credit_limit"`
  - `version="1.0.0"`
  - `title="Customer Credit Limit Guard"`
  - `description="Blocks sales order confirmation when customer exposure exceeds credit_limit plus optional tenant grace_amount."`
  - `default_enabled=True`
  - `requires=()`
  - `config_schema=CreditLimitPluginConfig`
  - `hooks=(HookBinding(hook_name="sales.order.validate_confirm", required_version="1.0.0", handler=check_credit_limit),)`
  - `events=()`
- In `check_credit_limit(session, context)`:
  - Calls `credit_limit_amount = await masterdata_service.lock_customer_credit_limit_unchecked(session, context.customer_id)`.
  - If `credit_limit_amount == 0`: returns (`0` sentinel means unchecked).
  - Reads active config via `cfg = get_current_plugin_config(CreditLimitPluginConfig)`
    (falling back to `CreditLimitPluginConfig()` when invoked directly in unit
    tests that bypass the runtime gate).
  - Calls `exposure_snapshot = await receivables_service.get_customer_exposure_unchecked(session, context.customer_id)`.
  - Computes `exposure = exposure_snapshot.uninvoiced_orders_total + exposure_snapshot.open_invoices_total + context.total`.
  - If `exposure > credit_limit_amount + cfg.grace_amount`: raises
    `CreditLimitExceededError("Sales order exceeds the customer's credit limit")`.

---

### 6. Import-linter contracts (`pyproject.toml`)

Two new `forbidden` contracts are added to `pyproject.toml`:

```toml
[[tool.importlinter.contracts]]
name = "platform.plugins may depend on core and masterdata only"
type = "forbidden"
source_modules = ["app.platform.plugins"]
forbidden_modules = [
    "app.modules.sales",
    "app.modules.inventory",
    "app.modules.receivables",
    "app.modules.ledger",
    "app.plugins",
    "app.platform.permissions",
    "app.platform.customfields",
    "app.platform.workflow",
    "app.platform.integration",
    "app.main",
    "app.cli",
]

[[tool.importlinter.contracts]]
name = "plugins must not import platform or module internals (models or routers)"
type = "forbidden"
source_modules = ["app.plugins"]
forbidden_modules = [
    "app.platform",
    "app.main",
    "app.cli",
    "app.modules.masterdata.models",
    "app.modules.masterdata.router",
    "app.modules.sales.models",
    "app.modules.sales.router",
    "app.modules.inventory.models",
    "app.modules.inventory.router",
    "app.modules.receivables.models",
    "app.modules.receivables.router",
    "app.modules.ledger.models",
    "app.modules.ledger.router",
]
```

And to close the parent-package attribute-traversal loophole (where a plugin
imports a bare business-module package root such as `from app.modules import sales`
and then reaches `sales.models.SalesOrder` via an already-populated module
attribute without triggering `ForbiddenContract`), the existing
`"no bare imports of app / app.modules / app.platform package roots"` `protected`
contract (`as_packages = false`, `allowed_importers = []`) is extended to
protect all business-module and platform subpackage roots as well:

```toml
[[tool.importlinter.contracts]]
name = "no bare imports of app / app.modules / app.platform package roots"
type = "protected"
protected_modules = [
    "app",
    "app.modules",
    "app.modules.masterdata",
    "app.modules.sales",
    "app.modules.inventory",
    "app.modules.receivables",
    "app.modules.ledger",
    "app.platform",
    "app.platform.permissions",
    "app.platform.plugins",
    "app.plugins",
]
allowed_importers = []
as_packages = false
```

---

### 7. Management HTTP API and permission catalog expansion

#### 7.1 Permission catalog (Migration `0011`)
Migration `0011` seeds two new permission codes into `permissions` and grants
both to the seeded `admin` role in `role_permissions` (increasing the total
seeded permission catalog from 45 to 47):
- `platform.plugin.read` — View discovered plugins and their effective state/config for the active company.
- `platform.plugin.manage` — Enable, disable, or configure plugins for the active company.

#### 7.2 HTTP Endpoints (`/api/v1/plugins`)
Mounted by `app.platform.plugins.router`:

| Method & Path | Route Dependency | Service Function | Behavior |
|---|---|---|---|
| `GET /api/v1/plugins` | `RequiresPermission("platform.plugin.read")` | `list_company_plugins(session)` | Returns all loaded plugins in topological order with their effective `(is_enabled, config)` for `require_current_company_id()`. |
| `GET /api/v1/plugins/{plugin_name}` | `RequiresPermission("platform.plugin.read")` | `get_company_plugin(session, plugin_name)` | Returns a single loaded plugin's metadata and effective `(is_enabled, config)` for the active company. Raises `404 NotFoundError` if `plugin_name` is not a loaded plugin. |
| `PATCH /api/v1/plugins/{plugin_name}` | `RequiresPermission("platform.plugin.manage")` | `update_company_plugin(session, plugin_name, data)` | Accepts `CompanyPluginUpdate(is_enabled: bool \| None = None, config: dict[str, Any] \| None = None)` (`extra="forbid"`, at least one field non-`None` else `422`). Merges omitted fields with existing row values (or `(manifest.default_enabled, manifest.default_config_dict)` on first upsert), validates `config` against `manifest.config_schema` (or rejects non-empty `config` if `config_schema is None` with `422`), locks the company row `FOR UPDATE`, verifies runtime dependency invariants (`422` on missing prerequisite, `409` on active dependent), upserts `company_plugins`, commits, and returns `CompanyPluginRead`. |

---

## Options Considered

### A. Where `PluginManifest` lives: `app.core.plugins` (chosen) vs. `app.platform.plugins`
- **Alternative:** Define `PluginManifest` inside `app.platform.plugins.manifest`.
- **Why rejected:** Every plugin in `app.plugins.*` must import `PluginManifest`
  to declare its `PLUGIN_MANIFEST`. If `app.plugins` imported
  `app.platform.plugins`, then plugins would depend on the platform layer; and
  if `app.platform.plugins` also imported `app.plugins`, they would form a
  package dependency cycle. Placing the pure data contract and runtime gate port
  in `app.core.plugins` mirrors `app.core.authorization` (ADR-009 Decision 4)
  and keeps `app.plugins` and `app.platform.plugins` completely decoupled in both
  directions.

### B. Runtime tenant gating: Stamp `__plugin_name__` + `PluginRuntimeGate` port (chosen) vs. closure wrappers in `hooks.register`
- **Alternative:** Have `loader.py` wrap each plugin handler in an `async def _wrapper(session, ctx)` closure and register `_wrapper` in `app.core.hooks` / `app.core.events`.
- **Why rejected:** Wrapping replaces the function reference in `hooks._handlers`.
  Existing tests (`tests/sales/test_credit_limit_plugin.py` lines 226–232) call
  `hooks.unregister(..., credit_limit.check_credit_limit)` and then re-register
  `hooks.register(..., credit_limit.check_credit_limit)` in `finally`. Under a
  closure-wrapper design, `finally` would re-register the raw unwrapped function,
  silently breaking tenant gating for all later tests in the process. Stamping
  `__plugin_name__` on the handler at manifest construction time (after checking
  for handler-sharing collisions) and invoking `PluginRuntimeGate` inside
  `hooks.run` / `events._dispatch` preserves function identity while keeping
  `app.core` 100% free of `app.platform` imports.

### C. Concurrency serialization for `company_plugins` mutations: Lock `companies` row `FOR UPDATE` (chosen) vs. locking `company_plugins` rows
- **Alternative:** `SELECT ... FOR UPDATE` on `company_plugins WHERE company_id = :cid`.
- **Why rejected:** Because `company_plugins` uses default-state fallback, a
  newly created company has **zero rows** in `company_plugins`. Under PostgreSQL
  `READ COMMITTED`, `SELECT ... FOR UPDATE` on zero matching rows acquires no row
  locks at all, allowing two concurrent transactions (one disabling default-enabled
  plugin $A$, one enabling dependent plugin $B$) to both insert without blocking
  each other. Locking the parent `companies` row via
  `masterdata_service.lock_company_unchecked(session, company_id)` always locks an
  existing row and serializes all plugin state changes for that tenant.

### D. `credit_limit` configuration schema: Option A `CreditLimitPluginConfig(grace_amount)` (chosen) vs. Option B `config_schema = None`
- **Option A (chosen):** Provide `CreditLimitPluginConfig(grace_amount: Decimal = Decimal("0.000000"))`
  on `credit_limit`.
- **Rationale:** Exercises the full per-tenant `config_schema` validation,
  persistence, and `plugin_config_context` injection pipeline on a real production
  plugin (not only synthetic test plugins), while keeping the default
  `grace_amount = 0` behavior 100% identical to Phase 1.

---

## Consequences & Non-Goals

### Positive
- Deterministic, cycle-free, SemVer-checked plugin loading at startup.
- Complete tenant isolation for plugin enablement and configuration, backed by
  mathematical induction from startup invariants through `FOR UPDATE`-serialized
  runtime transitions.
- `app/plugins/credit_limit.py` no longer reaches into `masterdata.models`,
  `sales.models`, or `receivables.models`; `import-linter` enforces public-surface
  access across all plugins in CI.

### Trade-offs
- Every execution of a **plugin** hook/event handler performs one indexed lookup
  on `company_plugins` (`UNIQUE (company_id, plugin_name)`) within the active
  `AsyncSession`. Core (non-plugin) handlers incur zero extra queries.

### Explicit Non-Goals
- **No untrusted plugin sandboxing or remote code upload:** Plugins remain
  operator-deployed Python code.
- **No dynamic process-wide hot-loading without restart:** Discovering newly
  installed Python packages or changing `PluginManifest` definitions requires an
  application restart; runtime APIs only toggle per-company `is_enabled` and
  `config` for already-loaded plugins.

---

## Action Items (Week 10 Implementation Sequence)

1. **Slice A — Core plugin port, versioned hooks/events, and deterministic loader:**
   - Implement `app/core/plugins.py` (`PluginManifest`, `HookBinding`,
     `EventBinding`, `PluginDependency`, `PluginLoadError`,
     `plugin_config_context`, `get_current_plugin_config`,
     `set_plugin_runtime_gate`, `is_semver_compatible`).
   - Extend `app/core/hooks.py` (`define_hook_point`, `defined_hook_points`,
     gate-aware `run`) and `app/core/events.py` (`registered_event_types`,
     gate-aware `_dispatch`).
   - Implement `app/platform/plugins/__init__.py` and
     `app/platform/plugins/loader.py` (`discover_entrypoint_manifests`,
     `resolve_and_validate_plugins`, `bootstrap_plugins`).
   - Add `enable_plugin_entrypoints` and `disabled_plugins` to `app/core/settings.py`.
   - Unit-test topological sort, lexicographical tie-breaking, cycle detection,
     SemVer compatibility, and startup inductive invariants in
     `tests/platform/test_plugin_loader.py`.
2. **Slice B — Migration `0011`, `company_plugins` persistence, and management API:**
   - Add Alembic migration `0011_platform_plugins.py` (`company_plugins` table +
     `platform.plugin.read` and `platform.plugin.manage` permissions granted to
     `admin`).
   - Implement `masterdata.service.lock_company_unchecked`.
   - Implement `app/platform/plugins/{models,schemas,service,router}.py` and
     wire `/api/v1/plugins` + runtime gate in `app/main.py`.
   - Update `tests/conftest.py` (`_clean_tenant_tables` truncation list) and
     existing permission-count assertions (45 $\rightarrow$ 47).
3. **Slice C — Public-surface-only refactor of `credit_limit.py` and `import-linter` enforcement:**
   - Implement `masterdata.service.lock_customer_credit_limit_unchecked` and
     `receivables.service.get_customer_exposure_unchecked` (typing
     `_SALES_ORDERS.c.total` as `AMOUNT`, casting `_SALES_ORDERS.c.status` to
     `String` for uppercase `'CONFIRMED'`/`'SHIPPED'`, and explicitly filtering
     `company_id` across all three subquery scopes).
   - Refactor `app/plugins/credit_limit.py` to declare `PLUGIN_MANIFEST` and
     `CreditLimitPluginConfig(grace_amount)` and use only public service ports.
   - Add the two new `forbidden` contracts and extend the `protected` contract
     in `pyproject.toml`; verify `import-linter` passes with zero violations.
4. **Slice D — End-to-end tenant plugin governance, concurrency proof, and documentation closure:**
   - Add integration and concurrency tests in `tests/platform/test_plugins_api.py`
     covering per-company enable/disable of `credit_limit`, `grace_amount`
     configuration, runtime dependency `422`/`409` guards, concurrent
     enable/disable serialization on `lock_company_unchecked`, and `replay_outbox`
     compatibility.
   - Reconcile `README.md`, `README.zh-TW.md`, `HANDOFF.md`, and the D.5 security
     gate manifest (`docs/security/d5-shards.json`, `docs/security/scan-policy.md`,
     `tests/security/test_d5_security_gate.py`).

---

## Consensus Revisions

### Round 1 (2026-10-30) — 6 Findings (3 High / P1, 3 Medium / P2), all resolved

1. **R1 (P1 / High) — Import-linter parent-package attribute-traversal bypass (§6):**
   A `ForbiddenContract` on `app.modules.<mod>.models` does not prevent a plugin
   from importing the bare package root `from app.modules import sales` and
   accessing `sales.models.SalesOrder` at runtime. Resolved by extending
   `protected_modules` in `"no bare imports of app / app.modules / app.platform package roots"`
   (`as_packages = false`, `allowed_importers = []`) to also list all five
   `app.modules.<mod>` package roots, `app.platform.permissions`,
   `app.platform.plugins`, and `app.plugins`.
2. **R2 (P1 / High) — `company_plugins` schema alignment with `TimestampAuditMixin` and `TenantScopedMixin` (§3.1):**
   Added `created_by: UUID NULL` and `updated_by: UUID NULL` to migration `0011`'s
   `company_plugins` specification to match `TimestampAuditMixin` (`app/core/db.py`),
   and aligned `company_id` foreign key semantics to `ON DELETE RESTRICT` to
   match `TenantScopedMixin` (`app/core/tenancy.py`).
3. **R3 (P1 / High) — PostgreSQL native `sales_order_status` enum & `AMOUNT` typing on `_SALES_ORDERS` (§5.1):**
   Specified that `receivables.service._SALES_ORDERS` must type
   `sa_column("total", AMOUNT)`, filter status via
   `sa_cast(_SALES_ORDERS.c.status, String).in_(("CONFIRMED", "SHIPPED"))`
   (uppercase PostgreSQL enum labels), and explicitly apply `company_id` filtering
   across the uninvoiced orders subquery, the nested `Invoice.order_id` exclusion
   subquery, and the open invoices subquery.
4. **R4 (P2 / Medium) — Non-destructive `__plugin_name__` stamping on handlers (§2.4, §4.2):**
   Specified that `PluginManifest.__post_init__` checks `getattr(binding.handler, "__plugin_name__", None)`
   *before* calling `setattr`, raising `PluginLoadError` immediately without
   mutating the shared function object if it is already bound to another plugin,
   and also rejects duplicate `(hook_name/event_type, handler)` bindings within
   a single manifest.
5. **R5 (P2 / Medium) — Unconditional `config_schema.model_validate({})` viability & partial-`PATCH` merge rules (§2.4, §3.2, §7.2):**
   Required `config_schema.model_validate({})` to succeed at startup for **all**
   plugins with `config_schema is not None` (regardless of `default_enabled`),
   and specified explicit partial-`PATCH` merge rules on first upsert vs.
   existing row update.
6. **R6 (P2 / Medium) — Codebase factual alignment (§2.1, §4.2, §7.1, Action Item 2):**
   Updated seeded permission count from `43 -> 45` to `45 -> 47`, aligned
   `hooks._handlers` and `hooks.run(hook_name, session, context)` signature order,
   referenced `inventory_service.handle_goods_shipped` as the core subscriber
   example, and bound `h=handler` in the gate closure (`B023`).

### Round 2 (2026-10-30) — `0 Findings (APPROVED)`

Verified all 6 Round 1 remediations (`R1`–`R6`) and completed a full pass across
tenancy, concurrency, transaction boundaries, and codebase contracts with zero
remaining findings. Status promoted from `PROPOSED` to `ACCEPTED`.
