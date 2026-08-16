# Phase 2 ("Platform") — architecture overview

Status: **ACCEPTED** — Codex architecture consensus APPROVED (3 rounds,
2026-08-16); the §6 scope-owner decisions (and the §3 keystone questions)
were resolved with the user on 2026-08-16, with second-model Codex advisory
discussions on the harder questions (Q1, Q3, and Q4–Q7). **The resolutions are recorded in §7 below; read §7
alongside §3–§6, since it settles several questions §3–§6 deliberately left
open.** §3–§6 are preserved as the analysis that led to those decisions,
not superseded by them. What remains open after §7 is per-module ADR/brief
detail, not phase-shape scope.
Author: Claude (Opus, architecture discovery)
Date: 2026-08-16
Scope owner: Ryan

This is a **high-level architecture overview** of the whole Phase 2 platform
layer — a map and a set of load-bearing decisions, at the altitude of the
master plan's own §3 (module map) and §4 (architecture style). It is
deliberately *not* a per-module implementation brief and *not* code. Each of
the five platform modules will get its own ADR and its own Week-N brief (the
Week 7/Week 8 pattern) once the foundational decisions in §3 are resolved.
Its job is to frame the shape of the phase and the handful of decisions that
must be gotten right first because they constrain everything else — not to
resolve them.

Grounding note: every claim below about the *current* system was checked
against the Phase 1 code at this repo's HEAD, not assumed. Where this
document says "there is no X today," it is because a search found none.

---

## 0. Where Phase 1 sits

Phase 1 ("Kernel"), tagged `v0.1.0`, is a five-module modular monolith
implementing one order-to-cash line — `masterdata`, `ledger`, `sales`,
`inventory`, `receivables` — with these kernel primitives already in place
and Codex-reviewed:

- **Multi-company data isolation** (`app/core/tenancy.py`, master-plan §10.2):
  a `contextvars` company context, a `do_orm_execute` hook that injects a
  `with_loader_criteria` filter on every SELECT touching a `TenantScopedMixin`
  model, **fail-closed** — a query with no bound company context raises
  `TenancyContextError` rather than returning all or zero rows.
- **A synchronous, in-process event bus with an outbox** (`app/core/events.py`,
  ADR-004): `publish()` writes one `outbox` row *and* runs subscribers inline
  in the caller's transaction; a `redispatch()` replay path + replay CLI
  already exist. Delivery beyond the write side (a dispatcher, webhooks) does
  not.
- **A minimal hook registry** (`app/core/hooks.py`, ADR-006): named,
  synchronous, in-process veto/augment points (`register`/`run`/`unregister`),
  consumed today by exactly one hard-wired plugin, `app/plugins/credit_limit.py`.
- **CI-enforced module boundaries** (`pyproject.toml` `[tool.importlinter]`):
  business modules are mutually independent; `app.core` may not import
  `app.modules` or `app.plugins`; `app.modules` may not import `app.plugins`.
  Plugins may import core and modules but nothing may import them back. This
  is *physics, not convention* — a violation fails the build.

Two pieces of Phase 2 groundwork are **already in the schema**, deliberately
laid down early per master-plan §2 so they need no later table rewrite:

- `CustomDataMixin` (`app/core/db.py`): a `custom_data JSONB` column already
  hangs on the business tables (e.g. `Account`), with no central definition or
  validation layer yet — the seat `platform.customfields` fills.
- `TimestampAuditMixin` (`app/core/db.py`): `created_by` / `updated_by` UUID
  columns already exist, **nullable and currently never populated** — because
  there is no identity to populate them with. They are pre-placed seats for the
  actor that Phase 2's authentication layer introduces.

## 1. What Phase 2 must add — the gap

Phase 2 ("Platform") is defined by the master plan (§1, §3) as the phase that
"lets other people build things on top": plugins, custom fields, workflow,
permissions, integration. Five modules (§2 below). But one gap dominates the
others and reframes the whole phase.

**Phase 1 has data isolation but no identity and no entitlement.** These are
three distinct things, and only the first exists:

1. **Data isolation** (exists): *given* an active company, queries cannot leak
   across companies. Real, tested, fail-closed.
2. **Authentication** (does not exist): there is **no `User` model anywhere**
   in the codebase, no password hashing, no session or token machinery, no
   `passlib`/`bcrypt`/`argon2`/JWT/OAuth dependency in `pyproject.toml`. Nobody
   is *who* they claim to be, because nobody claims to be anybody.
3. **Authorization / entitlement** (does not exist): `TenancyMiddleware` in
   `app/main.py` reads the `X-Company-Id` header and **believes it
   unconditionally** — its own docstring calls the header "the documented
   stand-in for a verified JWT/session claim." Anyone who knows or guesses a
   company UUID can act as that company, with full read/write. Nothing checks
   that a caller is *entitled* to the company they name, and there are no roles,
   permissions, or per-action gates of any kind.

So the honest one-line statement of the gap: **Phase 1 proved a company's data
cannot leak to another company; Phase 2 must establish who the caller is and
what they are allowed to do — and it must do so without weakening the
fail-closed, CI-enforced discipline Phase 1 established.** This is why §3
treats authN+authZ as the keystone rather than as just one of five modules:
the answer to "who may edit a custom-field definition / approve a workflow step
/ enable or configure an already-installed plugin / trigger an integration" is
an input to the other four modules' designs, not an afterthought. (Note
"enable/configure," not "install": installing a plugin is an operator/
deploy-time act outside app RBAC — see §2.2 — so it is deliberately *not* in
this list of application-entitlement questions.)

## 2. The five Platform modules

The master plan §3 names five modules under a new `platform.*` grouping. For
each: its responsibility, what it **owns** vs. **consumes**, and how it relates
to the existing kernel. These are overviews, not specs — the boundaries matter
more here than the internals.

### 2.1 `platform.permissions` — identity, roles, entitlement

- **Responsibility**: who the caller is (authentication) and what they may do
  (authorization). Introduces the first-ever `User` model and the actor
  identity the whole platform layer keys off. Per master-plan §3, its remit is
  "RBAC + row-level (company/department/amount-threshold) + field-level" — but
  *how much* of that ships in Phase 2 is the central open decision (§3).
- **Owns**: the user/credential/role/permission tables (shape TBD in §3), the
  active-company selection-and-verification step that replaces blind trust of
  `X-Company-Id`, and the enforcement mechanism (a dependency? a policy layer?
  a query-level filter? — §3).
- **Consumes**: the existing tenancy context (it must *populate and verify* the
  company context rather than replace it); the `TimestampAuditMixin`
  `created_by`/`updated_by` seats (it supplies the actor that finally fills
  them); and — a load-bearing and easily-missed edge — **masterdata's existing
  company source-of-truth**. A user's company membership/entitlement is
  anchored to real companies, which `masterdata` already owns; so
  `platform.permissions` must *consume* masterdata (an edge pointing
  permissions → masterdata), independent of and opposite to the "may a business
  module import permissions?" edge debated in §5.1. Both directions must be
  named in the import contracts, not just one.
- **Relation to kernel**: this is the module that turns the trusted-header
  stand-in into a real verified claim. It sits *in front of* tenancy, not
  beside it: authentication resolves the user, entitlement resolves which of
  the user's companies is active, and only then is the tenancy context bound
  (see §3). It is the keystone; §3 is devoted to it.

### 2.2 `platform.plugins` — the real plugin loader

- **Responsibility**: promote today's one hard-wired plugin + minimal registry
  into a real loader: discovery (entry points), inter-plugin dependency and
  ordering declarations, versioned hook-point compatibility, and *enforcement*
  (not just convention) that a plugin touches only a module's public surface.
  This is the master plan's §2.5 "護城河" and the explicit Phase 2 deferral
  target named in ADR-006 Decision 1 and `app/plugins/README.md`.
- **Owns**: plugin discovery/registration lifecycle, the hook-point version
  contract (semver, per master-plan §10.3), and plugin install/enable/disable
  state.
- **Consumes**: `app.core.hooks` and `app.core.events` unchanged — ADR-006's
  central claim is that the Week-4 minimum registry is exactly what the Phase 2
  loader "plugs into unchanged." The loader adds discovery and governance
  *around* those primitives; it does not replace them.
- **Relation to kernel**: keeps the CI-enforced dependency direction (core/
  modules never import plugins) as build-time physics; the loader's new job is
  to make the *public-surface-only* rule physics too, which today is only
  convention (`credit_limit.py` reaches into ORM models directly, documented as
  a Phase-2-enforced trade-off).
- **Entitlement seam — two distinct boundaries, not one** (master-plan §10.3):
  plugins are trusted, *administrator-installed* Python code, and entry-point
  discovery implies **operator/deploy-time package installation** — a supply-
  chain / ops boundary (who can add a package to the deployment), *not*
  something in-app ERP RBAC grants a user. What in-app `platform.permissions`
  can legitimately govern is the **company-scoped enable/configure** of an
  *already-installed* plugin. Keeping these separate matters for §4's ordering
  argument: only the enable/configure half is permissions-gated; the install/
  discovery half sits outside app RBAC entirely.

### 2.3 `platform.customfields` — the custom-field engine

- **Responsibility**: turn the already-present `custom_data JSONB` columns into
  a governed feature: a central field-definition registry (type, validation,
  display label, per master-plan §2.4), validation of `custom_data` writes
  against those definitions, and exposure of custom fields through the API/
  schema layer. This is the master plan's "first pillar of customize-without-
  forking."
- **Owns**: the field-definition tables and the validation/serialization of
  `custom_data` payloads. Note the P3-deferred item from master-plan §10:
  "custom_data indexing and validation strategy" was explicitly left to
  Phase 2 design time — this module is where that gets decided.
- **Consumes**: the existing `CustomDataMixin` column (no schema rewrite
  needed — the seat exists), tenancy (field definitions are themselves
  company-scoped data), and permissions (who may *define* a field vs. who may
  *fill* one — an entitlement question).
- **Relation to kernel**: additive. The kernel already carries the column;
  this module governs it. It should not require touching the five business
  modules' models.

### 2.4 `platform.workflow` — approval / state-machine engine

- **Responsibility**: a configurable approval-chain / state-machine engine
  (master-plan §3), so that state transitions like `sales_order: draft →
  confirmed` can be gated by a configured approval chain instead of hard-coded
  logic.
- **Owns**: workflow/state-machine definitions, approval-chain configuration,
  and the runtime state of in-flight approvals.
- **Consumes**: the existing hook points are the natural integration seam —
  `sales.order.validate_confirm` already demonstrates a *veto point before a
  state transition*, which is structurally what an approval gate is. Whether
  workflow rides the existing hook mechanism, the event bus, or introduces its
  own transition registry is a design question for its own ADR. It also
  consumes permissions heavily: *who may approve step N* is the core
  entitlement question of the whole module.
- **Relation to kernel**: this is the module most entangled with
  `platform.permissions` — an approval engine with no notion of *who is
  authorized to approve* is not an approval engine. Hence it sequences after
  permissions (§4).

### 2.5 `platform.integration` — outbox → webhook, Excel import/export

- **Responsibility**: outbound delivery of domain events to external systems
  (webhooks), and batch Excel import/export ("Excel 是台灣企業的血液" —
  master-plan §3). This is where the outbox *write* side that Phase 1 built
  finally gets a *delivery* side.
- **Owns**: webhook subscription/endpoint configuration, delivery attempt and
  retry state, and the Excel import/export mapping layer.
- **Consumes**: the existing `outbox` table as the **immutable event source**,
  and `app.core.events` — master-plan §2.6 designed for exactly this: "Phase 2+
  要接 message queue、webhook 時，直接從 outbox 讀，核心不用改." The at-least-
  once + idempotent-consumer + exponential-backoff semantics are already
  specified (§10.4). But "reuse *unchanged*" is too strong for the *delivery*
  side (see §5.2): this module builds a **new, consumer-specific delivery-state
  layer** on top of the shared event source. It reuses the outbox as the source
  of truth for *what happened*; it does not reuse the outbox row's own
  `dispatched_at` as its delivery cursor.
- **Relation to kernel**: still the cleanest reuse story in the phase — the
  outbox pattern was laid down in Phase 1 *specifically* so integration could
  read events without touching the core. What needs decisions are the
  **delivery-state boundary** (§5.2), the **delivery mechanism and its Phase 2/
  Phase 3 line** (§5.3), and whether a broker enters the picture in Phase 2 at
  all (§5.3, §6).

## 3. The keystone decision: authentication + authorization + company entitlement

This is the load-bearing decision of Phase 2, framed here for the
Codex-consensus and user-decision process — **not resolved.** It is the
keystone because it constrains the other four modules: every "who may…"
question in §2 resolves here. The Phase 1 design (§10.2) deliberately left this
as a documented Non-Goal with a stand-in header; Phase 2 must replace the
stand-in without breaking the fail-closed discipline that made the stand-in
safe to ship.

Three sub-decisions, each with the trade-offs laid out, none pre-answered.

### 3a. Authentication — three orthogonal axes, not competing options

A common framing trap here is to line up "self-hosted user table vs. JWT vs.
OAuth2/OIDC" as if they were three choices of one thing. They are not: a
credential *source*, a request-token *format*, and an external-IdP *delegation*
are different axes that combine (a local user store can issue JWTs; an OIDC
deployment still needs local user linking, entitlement records, and app
sessions; OAuth2 by itself is authorization-delegation, not authentication).
Separating them keeps the decision honest:

- **Axis (i) — credential / identity authority** (where the source of truth for
  *who a principal is* lives): a **local user+credential table**, an **external
  OIDC provider**, or a **hybrid/pluggable** arrangement. This is the axis the
  master-plan positioning bears on directly: "the Digiwin Taiwanese SMEs can
  self-host" argues for a local store working with zero external dependency as
  at least the default, with external IdP as an option — not the reverse.
- **Axis (ii) — request-authentication mechanism** (how each HTTP request proves
  it belongs to an authenticated principal): a **server-side session**, an
  **opaque bearer token**, or a **signed JWT**. This is largely independent of
  axis (i) — the tenancy middleware's own docstring already anticipates it,
  framing `X-Company-Id` as a stand-in for "a verified JWT/session claim." The
  trade-offs here (statelessness vs. revocation/rotation cost) are a
  security-ADR concern, not a user-facing one (see §6 Q2).
- **Axis (iii) — user provisioning/linking + entitlement ownership** (who
  creates users, links an external identity to a local principal, and owns the
  user↔company entitlement records): required under *every* combination of
  (i)/(ii) — even a pure-OIDC deployment must still map an external subject to a
  local principal and store which companies it may act as. This axis is where
  the multi-company entitlement records that §3 keeps returning to actually
  live, and it is never eliminated by delegating (i).

The interaction that must be designed regardless of which point in that
three-axis space is chosen: **a user may
belong to multiple companies.** So identity alone is insufficient — the flow
must be (1) authenticate the user, (2) determine the *active* company for this
request, (3) **verify the user is entitled to that company**, and only then
(4) bind the tenancy context. Today step 4 happens directly from an unverified
header with steps 1–3 absent. Whether the active company arrives as a header,
a token claim, or a session attribute, the entitlement check in step 3 is the
new load-bearing gate. The existing fail-closed `TenancyContextError` becomes a
*second* line of defense behind a new "not entitled to this company →
401/403" gate, rather than the only line.

### 3b. Authorization model — how deep, and how enforced

Two independent questions.

**How deep?** Master-plan §3 specifies "RBAC + row-level (company/department/
amount-threshold) + field-level." That is a large surface. The real decision is
how much lands in Phase 2 vs. is scoped as a later increment:

- **Pure RBAC** (roles → permissions, action-level): smallest coherent unit,
  ships a real auth story, unblocks the other four modules' "who may…"
  questions. Row-level and field-level deferred.
- **RBAC + row-level** (company / department / amount-threshold): the
  amount-threshold dimension of *authorization* — "who may approve/perform an
  action above value N" — is about the **actor** (role, permission tier) and
  should not be confused with a business-policy check that inspects no actor at
  all. The existing `credit_limit` plugin is exactly such a business-policy
  veto: it compares a customer's commercial credit exposure against a credit
  limit and inspects no user, role, or permission whatsoever. Amount-based
  *approval authority* (workflow) and amount-based *credit control* (a business
  rule) may share low-level policy-evaluation primitives, but they are
  structurally different concerns and must stay distinct — collapsing
  permissions, workflow, and business rules into "one amount-threshold engine"
  is a trap this phase should avoid.
- **+ field-level**: interacts directly with `platform.customfields` (who may
  see/edit which fields) — arguably co-designed with that module rather than
  built up front.

**How enforced?** This is a genuine architecture choice with a strong Phase 1
precedent to weigh:

- **A FastAPI dependency / decorator at the router edge**: explicit,
  greppable, per-endpoint. Simple; but enforcement lives in the web layer, and
  service functions called from CLI/replay paths are unguarded.
- **A policy/service layer**: checks live in service functions, so every entry
  path (HTTP, CLI, replay) is covered. More uniform; more invasive.
- **Reuse the `do_orm_execute` / `with_loader_criteria` pattern that tenancy
  already uses** — for row-level authorization specifically. This is the most
  interesting option because it would make row-level entitlement *the same kind
  of physics* as company isolation: a query-level filter that fails closed. It
  fits the established pattern well for read-path row filtering; it fits less
  obviously for action-level (can-this-user-confirm-an-order) checks, which are
  not SELECT-shaped. A likely answer is a *combination* — but that is exactly
  the decision to make deliberately, not by default.

### 3c. Staying consistent with Phase 1's discipline

Whatever is chosen, it must preserve the three properties that define this
codebase:

- **Fail-closed**: no authorization path may default to "allow" when context is
  missing — mirroring tenancy's "no context → raise," not "no context → allow."
- **CI-enforced boundaries**: `platform.permissions` must fit the import-linter
  contract story (§5). Note a real tension to resolve: enforcement that lives in
  a shared policy layer which *every* business module must call inverts today's
  "modules are independent and know nothing about each other" rule. Whether
  permissions is a dependency the modules may import, or a middleware/context
  layer they remain ignorant of (like tenancy is today), is both an
  architecture and an import-contract decision.
- **Testable in isolation**: tenancy was built to work from plain scripts (no
  FastAPI required) so the replay CLI and Alembic could reuse it. The identity/
  authz primitives should hold the same line, or the replay/CLI/test paths
  break.

## 4. Dependency ordering across the five modules

Codex's standing advice — treat as a strong prior to argue with, not an axiom —
is: **permissions first, then plugins, then the rest.** This document endorses
it, with the argument made explicitly rather than asserted:

1. **`platform.permissions` first.** It is the keystone (§3): the "who may…"
   answer is a design *input* to the other four, not something bolted on after.
   Custom-field admin authority, workflow approval authority, plugin
   enable/configure authority (not install — §2.2), and integration-trigger
   authority all resolve to entitlement checks that do not exist yet. Building any of the other four first means
   either hard-coding an authority model you then rip out, or shipping a
   feature with an admin surface anyone can call — repeating the exact gap
   Phase 2 exists to close. Permissions also has the longest security tail
   (credential storage, session lifecycle), so it wants the most soak time.
2. **`platform.plugins` second.** Once identity exists, the plugin loader is the
   next *hard contract* to lock, because it defines the versioned, enforced
   extension surface every future contributor (and the Phase 4 localization
   packs — master-plan §3) builds against. It is contract-heavy and
   foundational; the two "feature" modules below are better built once the
   extension surface underneath them is stable. Note the ordering argument here
   is **weaker than for the feature modules**, and honesty requires saying so:
   the loader's *install/discovery* half is an operator/deploy-time concern
   outside app RBAC (§2.2), so it does *not* depend on `platform.permissions`
   at all and could in principle be built first. Only the *company-scoped
   enable/configure* half needs a permission gate. So plugins is placed second
   not because it strictly *must* follow permissions, but because (a) its
   enable/configure surface wants the permission model to exist, and (b) it is
   the phase's next foundational contract regardless — contract-before-feature
   again. A defensible alternative is to build the install/discovery loader
   early (it is unblocked) and add the enable/configure gate once permissions
   lands.
3. **`platform.customfields` and `platform.workflow`** follow, in either order,
   both gated on permissions for their admin/approval authority. `customfields`
   is the more self-contained (its column already exists; its main external
   dependency is "who may define a field"). `workflow` is the most
   permissions-entangled of all five (an approval engine *is* an authority
   model in motion), so it benefits most from permissions being settled and
   arguably should come *after* customfields for that reason.
4. **`platform.integration` last** (of the five feature modules), though it is
   the *least* coupled to permissions and could in principle move earlier. It
   sits last because it is the cleanest reuse of an already-built foundation
   (outbox → delivery, §2.5), carries the lowest architectural risk, and its
   main open question (§5) is a scope-line decision rather than a new-contract
   decision. Its one real entitlement dependency — who may configure a webhook
   endpoint or trigger an export — is small and late-binding.

Counter-argument worth surfacing for the review: `integration` and
`customfields` are both largely additive and could be built in parallel with or
before parts of the harder modules to keep momentum (the master plan's stated
#1 risk is "burning out," §7). The ordering above optimizes for
*contract-before-feature*; a momentum-first reading might interleave a
low-risk additive module earlier. That trade-off is a scope-owner call, not a
purely technical one — flagged in §6.

## 5. Cross-cutting concerns for the whole phase

### 5.1 Preserving the modular-monolith + import-linter discipline

Master-plan §4 is emphatic: Phase 1–3 stay a modular monolith; import-linter
keeps it "always splittable." Phase 2 must extend that discipline, not dilute
it. Open design points for the review:

- **Does `platform.*` become a new top-level package** (`app/platform/…`)
  alongside `app/core`, `app/modules`, `app/plugins`? A natural reading: yes,
  with its own independence-and-direction contracts. But the *direction* needs
  deciding — may `app.modules.*` import `app.platform.permissions` (so a service
  can ask "may this actor do this")? That would make `permissions` a dependency
  the business modules point *at*, which is a new edge in the graph. Or does
  permissions stay a context/middleware layer the modules remain ignorant of,
  the way `tenancy` does today (business models just inherit a mixin; they never
  import the enforcement)? The tenancy precedent is the strongest argument for
  "context layer, not imported dependency" — and it is the cleaner story — but
  action-level checks may not fit that mold as neatly as row filtering does
  (§3b). This is the single most important import-contract decision of the
  phase and should be written as explicit contracts, the way §10.2/ADR-006
  were. And it is not the only edge: independently of whether *modules import
  permissions*, `platform.permissions` itself must import/consume `masterdata`
  for the company source-of-truth its entitlement records anchor to (§2.1) — a
  permissions → masterdata edge. Both edges (and their directions) belong in
  the contracts explicitly; naming only the modules-import-permissions question
  would leave the reverse dependency undeclared.
- **`platform.customfields` and the business modules**: it should govern the
  existing `custom_data` column *without* the five modules importing it —
  keeping their independence intact.

### 5.2 The outbox is the event *source* — delivery state is a new layer

`platform.integration` reuses the existing `outbox` and `app.core.events`
(§2.5), and the write side, replay/`redispatch` path, at-least-once +
idempotent-consumer + backoff semantics, and 30-day cleanup are settled
foundation, not new invention (master-plan §2.6, §10.4; ADR-004). But the
common shorthand "reuse the outbox *unchanged*" hides a real boundary that must
be drawn, and there is a concrete conflict in today's code proving it:

- The `outbox` row's own `dispatched_at` column is **already claimed** by
  internal dispatch. The replay CLI (`app/cli/replay_outbox.py`) reads rows
  `WHERE dispatched_at IS NULL` and, after replaying the row's *internal
  subscribers*, sets `dispatched_at = func.now()`. So a single outbox-level
  `dispatched_at` already means "internal subscribers have been (re)dispatched"
  — nothing more.
- A webhook dispatcher that read the same "undelivered" rows and stamped the
  same column would **collide**: a manually-replayed row would silently vanish
  from future webhook delivery, and an internally-dispatched row might never be
  delivered outward at all. Worse, per-endpoint / per-subscription delivery
  needs *independent* attempt-count and cursor state anyway — one boolean-ish
  timestamp cannot represent "delivered to webhook A, still pending for B."

The boundary to establish at this altitude (the schema itself is the
integration ADR's job, **not** this overview's): the `outbox` table is the
**immutable event source of truth for what happened**; **consumer-specific
delivery/cursor/attempt state is a separate, new layer**, one cursor set *per
consumer* (internal replay, webhook endpoint A, webhook endpoint B, …), never
the shared `dispatched_at`. Whether that is a single per-consumer cursor table,
per-delivery rows, or another shape — and whether internal dispatch's existing
`dispatched_at` is left as-is or itself migrated into the new layer — is
explicitly flagged as an integration-ADR concern, not resolved here. The
takeaway for *this* document is only that "reuse the outbox unchanged" is
accurate for the event *source* and misleading for the delivery *state*.

### 5.3 The Phase 2 / Phase 3 line — three separable things, not two

The master plan is not perfectly uniform across its sections, and the earlier
draft of this overview conflated two of them, so precision matters here. There
are **three** separable questions, with three different statuses — do not
collapse them into a single "Phase 2 vs. Phase 3" binary:

- **(a) The dispatcher itself — settled Phase 2** (per §10.4). The component
  that reads undelivered events and drives webhook delivery is explicitly a
  Phase 2 deliverable ("Phase 1 交付：寫入 + replay CLI；dispatcher 常駐程序是
  Phase 2"). So webhook delivery *and its dispatcher* are in-scope for
  `platform.integration`, plus Excel import/export.
- **(b) A separately-*deployed* integration gateway — Phase 3+** (per §4).
  Pulling the integration gateway out into its own independently deployed
  service is one of the only three extractions §4 permits, and it is explicitly
  Phase 3+ ("允許拆出去的只有三類，Phase 3+，經由 outbox 事件驅動… 整合閘道").
  So Phase 2's dispatcher lives *inside the monolith* — same deployment unit —
  not as an extracted service.
- **(c) A broker / message queue — genuinely OPEN, not settled.** This is the
  finding the earlier draft got wrong: it asserted MQ was Phase 3+, but
  master-plan §2.6 says the opposite — "**Phase 2+** 要接 message queue、
  webhook、非同步整合時，直接從 outbox 讀" explicitly *permits* a broker from
  Phase 2 onward. §4's Phase-3+ restriction is about **extracting a separately
  deployed service** (b), which is not the same thing as adopting a broker as a
  *transport* while the dispatcher still lives in the monolith. So "no broker in
  Phase 2" is **not** a settled fact — it would be a deliberate scope boundary
  this overview could propose, but only with explicit consensus. Absent that, it
  is an open decision (§6 Q7), not an assertion.

**Dispatcher shape (self-contained here, also surfaced as §6 Q7).** Even
staying inside the monolith and even without a broker, the delivery *shape* is
unpinned: an in-process background task vs. a co-deployed standalone worker
process reading the same database are both "inside the monolith" and both honor
the §4 extraction line. Which one — and whether a broker (c) is in scope — is a
`platform.integration` design decision with a scope-owner dimension, carried to
§6 Q7 rather than left as a bare forward-reference.

### 5.4 Actor / principal propagation — a cross-cutting concern in its own right

Introducing a `User` model (§2.1) does **not**, by itself, cause the
`created_by`/`updated_by` seats (§0) to be populated, nor does it make audit,
approval, or webhook payloads actor-aware. The originating actor has to be
*propagated* — deliberately threaded — across every entry path, and each path
is a distinct problem:

- **HTTP requests**: the authenticated principal is resolvable at the request
  edge and can be bound to a context (the natural sibling of the existing
  tenancy `contextvars` binding).
- **CLI, replay, and the future dispatcher**: these have **no HTTP request** to
  derive an actor from — exactly the situation the event bus already faced with
  tenancy (`register_event` forces a `company_id` onto every payload *precisely
  because* replay has no request). So these paths need an explicit
  **system/service-actor** notion; "no actor" must not silently become "null"
  or "trust whatever." Fail-closed discipline (§3c) applies here too.
- **Workflow**: an approval records *who approved*, so workflow is an actor
  producer/consumer, not just a passenger.

The load-bearing, and easily-missed, question: **must the originating actor
survive *through* an outbox event?** An audit or webhook consumer that needs
"who caused this" requires the actor to be captured *in* the event payload at
publish time — which, if adopted, **qualifies the "`app.core.events` stays
unchanged" claim** (this ties directly to §5.2: just as company_id is a
mandatory payload field today, an actor field may become one). Whether actor
belongs in the payload, in a side channel, or is deliberately *not* propagated
into events, is a real decision — flagged here so it is not discovered late,
after the audit/webhook consumers assume an actor that was never carried.

## 6. Open questions for the user (scope-owner decisions)

These are genuinely the scope-owner's calls — not Claude's and not Codex's to
make. They are separated deliberately from the technical trade-offs in §3–§5,
which *can* be analyzed and brought to consensus. This list is what a plan
document should hand up for a human decision.

1. **Authorization depth for Phase 2**: pure RBAC as the Phase 2 unit, with
   row-level and field-level scoped as a later increment? Or commit to RBAC +
   row-level (and/or field-level) up front? This sets the size of the whole
   phase. (Analysis: §3b.)
2. **Deployment/identity constraint** (not the token mechanism): is
   **zero-external-dependency login mandatory** — i.e. must the product work
   fully self-hosted with a local identity store and no external IdP as its
   default — or is delegating identity to an external OIDC provider an
   acceptable default? The user owns *this constraint*; the downstream choices
   it enables (local table vs. OIDC vs. hybrid on axis (i); session vs. opaque
   token vs. JWT on axis (ii)) are for the security ADR to recommend, not for
   the user to pick here. (Analysis: §3a's three axes.)
3. **Permissions-placement constraint** (not the mechanism): **must the business
   modules stay ignorant of permissions the way they are of tenancy today** —
   never importing the enforcement, just inheriting behavior through a context
   layer — or is it acceptable for a module to depend on (import) a permissions
   layer? This is the user-facing *constraint*; the concrete enforcement
   mechanism and the exact import contracts (including the reverse
   permissions → masterdata edge, §5.1) are an architecture-consensus/ADR
   output that the process should *produce a recommendation* for, with the user
   approving the constraint and its consequences rather than choosing the
   mechanism. (Analysis: §3b, §5.1.)
4. **Module sequencing vs. momentum**: accept the contract-before-feature
   ordering (permissions → plugins → customfields/workflow → integration), or
   interleave a low-risk additive module (integration or customfields) earlier
   to sustain momentum against the master plan's stated #1 risk of burnout?
   (Analysis: §4.)
5. **First Phase 2 release boundary**: does Phase 2 ship as one release when all
   five modules land, or as incremental releases per module (the master plan's
   "every Phase-end is a usable release" principle, §1, read at module
   granularity)? This decides how the Week-N briefs are cut.
6. **Public demo host reconsideration**: Week 8 Decision 3 formally deferred the
   public demo host "until Phase 2 ships at least a minimal auth gate." Which
   Phase 2 milestone — the first RBAC login, or a fuller entitlement model —
   is the trigger to revisit it, and does it still require a separate,
   independently Codex-reviewed security/deployment mini-brief?
7. **Integration transport + dispatcher shape**: master-plan §2.6 *permits* a
   message queue / broker from Phase 2 onward (it is **not** settled as Phase
   3+; §5.3). Does Phase 2 deliberately draw a "no broker in Phase 2, direct
   outbox reads only" boundary — a defensible scope discipline — or leave a
   broker in scope? And within the monolith, is the dispatcher an in-process
   background task or a co-deployed worker process? These set the size and shape
   of `platform.integration`. (Analysis: §5.3.)

---

## 7. Decision round — resolved (2026-08-16)

The §6 open questions **and** §3's keystone questions were resolved with the
scope owner on 2026-08-16. Each of the harder ones (Q1, Q3, and Q4–Q7 as a
batch) went through a second-model Codex advisory discussion first, in this
project's standing "never Claude-only" discipline. This section is the
normative decision record; §3–§6 above are the analysis that produced it.
Where a decision refines or corrects the framing in §3–§6, that is called out
inline below.

### 7.1 Keystone auth decisions (§3)

- **Authorization depth (Q1): pure RBAC + a mandatory company-entitlement
  gate.** Action-level RBAC (roles → permissions). Row-level and field-level
  are **deferred** to a later increment. **Refinement (Codex):** the
  authN→tenancy company-entitlement gate of §3a is **not** part of what's
  deferred — it is mandatory in Phase 2, the bridge that replaces today's
  unverified `X-Company-Id`. "Pure RBAC" means "no generalized row/resource/
  field authorization *beyond* that entitlement gate." The workflow
  amount-threshold tension (§3b) is resolved without row-level: **workflow
  routing inspects the amount and picks the required step; RBAC decides who
  may approve that step** — keeping routing and actor-authority distinct.
  What pure RBAC genuinely cannot express (a data-driven per-actor ceiling
  like "this actor may approve ≤ 47,500 across arbitrary actions") is a
  deferred, narrowly-scoped *resource-conditioned action decision* increment,
  **not** the whole company/department/amount row-level bundle. **Correction
  to §3b's grouping:** amount-threshold authority is *not* SELECT-shaped row
  filtering — it is contextual action authorization (ABAC-shaped), even
  though master-plan §3 files it under "row-level"; when it lands, it uses
  the service-boundary authorization port (below), not a query filter.
- **Identity / deployment constraint (Q2): zero-external-dependency login is
  mandatory; a local user+credential store is the default.** The product
  must run fully self-hosted with local identity and no external IdP.
  External OIDC is an optional add-on, never a prerequisite. (Rationale:
  master-plan positioning — "Taiwanese SMEs can self-host.") The specific
  request-auth mechanism (server session vs. opaque token vs. JWT — §3a
  axis ii) is **not** decided here; it is a `platform.permissions` security-
  ADR recommendation.
- **Permissions placement (Q3): dependency inversion.** Business modules
  **never import** the concrete `app.platform.permissions` package. They
  depend only on a narrow **authorization port owned by `app.core`**;
  `platform.permissions` *implements* that port and imports `masterdata` for
  the company source-of-truth; the **composition root** (`app.main`) wires
  implementation to abstraction — the same pattern `app.main` already uses to
  keep `sales` ignorant of concrete plugins. **Refinement (Codex):** this is
  preferred over the earlier "narrow direct `module → permissions` import
  exception" — explicit authorization *semantics* (a module knowing "confirm
  is privileged") and concrete permissions *coupling* are separate questions,
  and inversion gives the first without the second. The authoritative check
  lives at the **public service/command boundary**, not the router edge:
  router-only enforcement is bypassed by CLI/replay/worker paths, and the
  authoritative order total is only known *inside* the locked service
  transaction (a router would authorize against a stale pre-repricing
  amount). Fail-closed (§3c) still governs: an unclassified business action
  denies by default.

### 7.2 Phase-shape decisions (§4–§5)

- **Module sequencing (Q4): permissions first, contract-before-feature**
  (permissions → plugins → customfields/workflow → integration). The plugin
  loader's operator/deploy-time install/discovery half (which does *not*
  depend on permissions, §2.2) is kept as **fallback work only** — pulled
  forward *if* the permissions build stalls — **not** a formally-sanctioned
  parallel slice, to keep the keystone as the single critical-path focus.
- **Release boundary (Q5): incremental per-module releases.** The **first
  Phase 2 release is a complete `permissions` vertical slice** — login →
  RBAC → entitlement gate → a protected action demonstrating allow/deny —
  demoable *without* any consuming module (plugins/workflow). **Refinement
  (Codex):** a small set of genuinely non-sensitive, already-global
  reference-data reads (currencies, UoM) are an explicit **anonymous
  allowlist**; everything else goes behind the auth gate. (Avoids bloating
  the first release for a few harmless endpoints.)
- **Public demo host trigger (Q6): the first RBAC-login milestone triggers
  writing the security/deployment mini-brief — it does NOT auto-launch.**
  Going live still requires that separate, independently Codex-reviewed
  security/deployment brief with a concrete go-live checklist (the real bar
  is higher than "has login": entitlement gate + rate limiting + reset/seed
  story). **Decision:** the public demo is **read-only from the start**
  (seeded fake data, periodic reset); read-write is out of scope for the
  first public exposure. (This finalizes Week 8 Decision 3's deferral.)
- **Integration transport + dispatcher shape (Q7): no broker in Phase 2; a
  co-deployed standalone worker process.** Phase 2 deliberately draws a "no
  broker / direct outbox reads only" scope line (Postgres outbox suffices; a
  broker only adds a second operational surface). **Decision changed from the
  scope owner's initial in-process lean to a co-deployed worker, on Codex's
  recommendation:** an in-process background task under-weights the coupling
  between transaction boundaries, request-scoped `ContextVar`s, the API
  process lifecycle, and external network side-effects (a task started in a
  request handler can run before the producer commits, can't un-send a
  webhook on producer rollback, and risks inheriting the request's actor/
  company context). The worker is treated as an **outbox *consumer*, not a
  synchronous `app.core.events` subscriber** — so no conflict with the sync
  same-transaction event model: the sync bus keeps internal must-commit-
  together work; the worker handles post-commit, retryable, at-least-once
  external side-effects. It reads only committed events, claims due
  deliveries atomically (`FOR UPDATE SKIP LOCKED` / lease), sends HTTP
  *outside* any DB lock, and — like the replay CLI — has no HTTP request, so
  it **explicitly** binds `company_context` + a system/service actor and
  resets in `finally`, never relying on inherited context. This uses the
  consumer-specific delivery-state layer (§5.2), never the shared
  `outbox.dispatched_at`. **Consequence for §5.4:** two actors must stay
  distinct — the **dispatcher actor** (the system/service actor performing
  delivery) and the **originating actor** (the user who caused the domain
  event). If a webhook/audit consumer needs "who caused this," the
  originating actor must be captured *into the event envelope at publish
  time* — it cannot be reconstructed from the dispatcher's system actor.

### 7.3 Explicitly still open after this round (ADR/brief-level, not phase scope)

Deferred *down* to the per-module ADRs, not left unscoped: the request-auth
mechanism (session/opaque/JWT); the exact `app.core` authorization port
signature and the `platform.*` import contracts; the plugin manifest schema;
the integration delivery-state table shape; the event originating-actor
envelope format; the public-hosting provider and the demo go-live checklist;
and (deferred to `platform.customfields`) custom-field indexing/validation
and field-level authorization.

---

## Appendix: what is already settled vs. what this document leaves open

Matching the Week 8 brief's habit of separating settled facts from live
decisions:

**Already settled (facts / prior decisions this document builds on):**
- Phase 2 is five modules: `permissions`, `plugins`, `customfields`,
  `workflow`, `integration` (master-plan §3).
- Phase 2 stays a modular monolith under CI-enforced import boundaries
  (master-plan §4; ADR-001).
- The outbox write side, replay path, and delivery *semantics* are built and
  specified; integration reuses the outbox as its **event source** (master-plan
  §2.6, §10.4; ADR-004). (The consumer-specific *delivery-state* layer on top is
  new, not a reuse — §5.2, and in the open list below.)
- The Phase 1 minimal hook registry is the surface the Phase 2 plugin loader
  extends unchanged (ADR-006 Decision 1).
- `custom_data JSONB` and `created_by/updated_by` seats already exist in the
  schema (master-plan §2.4, §2.8; `app/core/db.py`).
- The outbox **dispatcher** is a Phase 2 deliverable (master-plan §10.4), and
  extracting the integration gateway into a *separately deployed service* is
  Phase 3+ (master-plan §4). A **message queue / broker** is permitted from
  Phase 2 by §2.6 but was **deliberately scoped OUT of Phase 2** in the §7
  decision round (no broker, direct outbox reads only) — a scope choice, not a
  master-plan constraint.

**Resolved in the §7 decision round (2026-08-16) — see §7 for the normative
record:** §3's keystone questions (authZ depth, identity constraint,
permissions placement) and all seven §6 questions, including the broker/
dispatcher-shape question (no broker; co-deployed worker). The bullets below
are what remains open *after* §7 — genuine per-module ADR/brief detail that
§7's decisions deliberately defer downward, not phase-shape scope:
- The request-auth mechanism (session vs. opaque token vs. JWT) — a
  `platform.permissions` security-ADR recommendation (§3a axis ii, §7.1).
- The exact `app.core` authorization port signature and the `platform.*`
  import contracts that realize the §7.1 dependency-inversion decision —
  including the reverse permissions → masterdata edge (§2.1, §5.1, §7.1).
- The outbox consumer-specific delivery-state layer's *shape* — a per-consumer
  cursor table vs. per-delivery rows vs. another per-consumer representation
  (never the shared `dispatched_at`, which §5.2 rules out), an integration-ADR
  concern (§5.2, §7.2).
- The event originating-actor envelope format — whether/how the originating
  actor is captured into the event payload at publish time (§5.4, §7.2).
- The plugin manifest schema; custom-field indexing/validation and field-level
  authorization (deferred to `platform.customfields`); the public-hosting
  provider and the demo go-live checklist (§7.2, §7.3).

This document fed a real Codex architecture-consensus review (APPROVED, 3
rounds) and a user decision round (§7), in the same spirit as the Week 8 brief.
It now serves as the settled phase-shape input to the per-module ADRs, starting
with `platform.permissions`.
