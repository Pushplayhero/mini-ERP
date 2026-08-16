# ADR-009: `platform.permissions` — identity, RBAC, and the authorization port

**Status:** **ACCEPTED** — Codex architecture consensus APPROVED, round 8
(2026-08-16). Rounds 1–7 REJECTED (5, 3, 3, 5, 4, 1, and 1 findings
respectively); rounds 3 and 4 each surfaced a real scope question, settled
with the scope owner via a dedicated Codex advisory discussion before the
next revision — see "Consensus Revisions" for the full, round-by-round
history. Implementation may begin per the sequencing implied by Action
Item 1 onward.
**Date:** 2026-08-16 (consensus completed 2026-08-16/17)
**Deciders:** Ryan (project owner), Codex reviewer (consensus gate)
**Depends on:** `docs/adr/PHASE2-platform-architecture-overview.md` §7
(ACCEPTED) — this ADR implements §7.1's Q1/Q2/Q3 decisions; it does not
relitigate them.

> Numbering note: ADR-009 is the first Phase 2 ADR, per the master plan's
> "each of the five platform modules gets its own ADR" and per §7.2's Q4
> decision (`platform.permissions` first, contract-before-feature).

## Context

Phase 1 shipped with a deliberate, documented Non-Goal: `X-Company-Id` is a
trusted-header stand-in for "a verified JWT/session claim" (`app/main.py`'s
`tenancy_middleware` docstring says so explicitly — the phrase "JWT/session
claim" already exists as a *forward-looking comment* there and in
`app/core/settings.py`, anticipating this ADR; no such claim is actually
verified anywhere today). Multi-company **data isolation** is real and
well-tested (`app/core/db.py`'s `do_orm_execute` + `with_loader_criteria`,
fail-closed via `TenancyContextError`) — but nothing today verifies a caller
is actually *entitled* to claim a given company, and there is no `User`
model, no credential storage, and no authorization *implementation* of any
kind anywhere in the codebase (confirmed by grep at ADR-drafting time — zero
hits for a `class User`, password handling, a login endpoint, or any
`Depends(...auth...)` pattern in `app/`).

The Phase 2 overview (§7.1) already settled the three keystone questions this
ADR must honor, not re-decide:

- **Q1 — authorization depth**: pure RBAC (action-level) + a **mandatory**
  company-entitlement gate. Row-level and field-level authorization are
  explicitly deferred. Amount-threshold approval (workflow) is handled by
  "routing picks the step, RBAC decides who may approve it" — not a
  generic per-actor ceiling engine.
- **Q2 — identity/deployment constraint**: **zero-external-dependency login
  is mandatory**; a local user+credential store is the default; external
  OIDC is an optional, never-required add-on. **The specific request-auth
  mechanism (session vs. opaque token vs. JWT) was explicitly left to this
  ADR** — that is Decision 3 below.
- **Q3 — permissions placement**: **dependency inversion.** Business modules
  depend only on a narrow authorization port owned by `app.core`;
  `platform.permissions` implements it; the composition root (`app.main`)
  wires implementation to abstraction — the same pattern already used to
  keep `sales` ignorant of concrete plugins (`app.main:212`). Authorization
  checks live at the **public service/command boundary**, not the router
  edge, because CLI/replay/worker paths bypass router-only enforcement and —
  concretely, in this codebase — `sales.service.confirm_order` only knows
  the *authoritative* order total after repricing inside a locked
  transaction it does not itself own or commit (`service.py:335`, caller
  owns the transaction per ADR-003 R1's flush-only-core convention); a
  router cannot safely authorize against a possibly-stale pre-repricing
  amount.

This ADR's job: turn those three constraints into an actual data model,
request-auth mechanism, import-linter contract set, and migration/rollout
plan — at the same rigor as ADR-005 (ledger) or ADR-006 (sales/hooks), before
any implementation begins.

## Decision

1. **Global RBAC data model**: `users` (login identifier = `email`, unique,
   case-insensitive; `password_hash`; `is_active`), `roles` (code-seeded
   catalog), `permissions` (code-seeded catalog), `role_permissions`
   (roles↔permissions, many-to-many), and `user_company_roles` (which user
   may act as which company, under which role(s) — the entitlement table;
   **deliberately NOT `TenantScopedMixin`** — see this decision's own
   "Options Considered" entry below for why). Roles and permissions are
   **global** (shared across all
   companies), not per-company-customizable, in this ADR's scope. `users` is
   a tenant-root table like `companies`, never itself tenant-scoped.
2. **Credential storage: Argon2id** password hashing (`argon2-cffi`), the
   current OWASP-recommended default — no bespoke crypto.
3. **Request-auth mechanism: opaque, server-side-validated bearer tokens**,
   stored in a new `sessions` table. The raw token is a high-entropy random
   value (`secrets.token_urlsafe(32)`); **the *stored* lookup key is its
   SHA-256 hash, not an Argon2id hash** — Argon2id's deliberate slowness
   defends low-entropy *passwords* against offline brute force, which a
   256-bit random token does not need, and a fast deterministic hash is
   required for an indexed per-request lookup (`WHERE token_hash = ?`).
   Not a cookie session, not a stateless JWT (Decision 3 below).
4. **The `app.core.authorization` port — a context carrier, not a swappable
   interface.** `ActorContext` (`user_id`, resolved `permission_codes:
   frozenset[str]`, `is_system_actor: bool`) bound via a `ContextVar`,
   mirroring `app.core.tenancy` exactly. `require_permission(action)` raises
   fail-closed (`ActionDeniedError`) if no context is bound or the action
   isn't in the set. **This is deliberately not a `Protocol`/ABC that
   `platform.permissions` "implements"** — there is exactly one producer of
   `ActorContext` (the resolution service in `platform.permissions`) and
   exactly one binding point per entry path, so a formal interface would be
   premature abstraction with nothing to swap. (It becomes a real candidate
   for a `Protocol` if/when external-OIDC resolution — an optional Q2
   add-on — needs to plug in as an alternative resolver; not needed yet.)
   `app.core` never imports `platform.permissions` or `masterdata`.
   **Non-HTTP paths that DO exercise real service-layer authorization
   (currently: `seed_demo.py` only) bind a purpose-specific `ActorContext`
   constant** with a narrow, enumerated permission set matching exactly what
   that path legitimately needs (e.g. `DEMO_SEED_ACTOR`) — **not** one
   shared, all-permissions `SYSTEM_ACTOR`, which would make `is_system_actor`
   an unconditional bypass and defeat the point of scoping permissions at
   all. Paths that never reach a service-layer check in the first place
   (the maintenance tools covered by Decision 10) don't bind an
   authorization actor for this purpose — see Decision 10 for why binding
   one there would be theater, not protection.
5. **Request binding**: a new `AuthenticationMiddleware` **replaces**
   `tenancy_middleware`'s header-reading code entirely (the *function* that
   reads `X-Company-Id` and calls `set_current_company_id` is deleted) but
   **reuses `app.core.tenancy.company_context(...)` unchanged** as the
   underlying binding primitive — only *how* the active company id is
   determined changes, from a raw trusted header to a verified
   `sessions.active_company_id` looked up via a valid bearer token. The new
   middleware resolves `Authorization: Bearer <token>` via
   `platform.permissions`, then binds *both* `company_context(...)` and the
   new `ActorContext` for the request. `X-Company-Id` as a trust boundary is
   retired entirely; the active company comes only from the session,
   changeable via a `switch-company` endpoint that re-checks entitlement.
6. **Bootstrap: one idempotent CLI command, ordered for a genuinely empty
   database.** `app/cli/bootstrap_admin.py` runs the full first-install
   sequence in one script, mirroring `seed_demo.py`'s existing
   `_ensure_reference_data` → `_ensure_company` ordering: (a) ensure global
   reference data (currencies/UoM) exists, (b) ensure a first `Company`
   exists (create if none), (c) create the first `User` with credentials
   supplied via CLI args or an env var (never hardcoded), (d) create the
   first `user_company_roles` row entitling that user to that company under
   a seeded `admin` role. Runs outside any HTTP request; **falls under
   Decision 10's Rule 10b** (not Rule 10a — it creates real new facts, but
   runs before any user/credential/entitlement exists to authenticate
   against, so requiring `require_permission()` here would be circular).
   No public "register the first admin" HTTP endpoint exists.
7. **Fail-closed, two REAL enforcement layers, not one check + one label.**
   (a) **Router-level**: every route declares, via a FastAPI dependency,
   either `Public()` or `RequiresPermission(code)` — and `RequiresPermission`
   **actually calls** `require_permission(code)` against the
   already-request-bound `ActorContext`, so this is a genuine second
   enforcement point, not documentation. (b) **Service-level**: the
   authoritative `require_permission()` call inside the service function
   itself (Decision 4/Q3) — the only layer a non-HTTP caller can hit, and
   only if its call path actually passes through a service function in the
   first place (true for `seed_demo.py`'s `DEMO_SEED_ACTOR` path; **not**
   true for the Decision 10 maintenance tools, which have no service-layer
   call in their path at all and are authorized operationally instead, not
   by this layer). (c) **Startup safety net**: at application startup, iterate
   `app.routes` and **crash (refuse to start)** if any route carries neither
   dependency — this catches "a developer added a route and forgot to
   classify it at all," a different failure mode than "classified correctly
   but the service function's own check was forgotten." **That second
   failure mode has no structural safety net for non-HTTP callers** — (a)
   only runs for HTTP requests, and (c) only checks that a route was
   classified, not that the service it calls actually enforces anything;
   it is closed only by code review and test coverage (see Decision 7's
   "Options Considered" entry, which states this limit explicitly).
8. **Per-request entitlement revalidation; explicit 401/403 mapping.**
   `sessions.active_company_id` is only a *pointer* — every request
   re-resolves `(user_id, active_company_id) → permission_codes` fresh from
   `user_company_roles`/`role_permissions` (not cached beyond the session
   row), and re-checks `users.is_active`. A revoked entitlement or a
   disabled user therefore takes effect on the **very next request**, not
   only at the next `switch-company` call. **401** = no valid/unexpired
   session (unauthenticated). **403** = valid session, but the actor lacks
   entitlement to the active company or lacks the required permission
   (authenticated, not authorized) — the same mapping
   `TenancyContextError` already uses for the analogous tenancy case
   (`app/main.py`'s exception handler). A user with more than one company
   entitlement selects the active company explicitly at login (a `company_id`
   in the login request, validated against entitlement immediately); a user
   with exactly one entitlement gets it auto-selected. **A disabled user
   with an otherwise-valid, unexpired session token gets 403, not 401**:
   the token itself is genuine (the session row exists and hasn't expired,
   so the caller *is* authenticated), but the fresh `users.is_active` check
   this decision requires on every request fails — the same "valid
   credential, no longer entitled" shape as a revoked
   `user_company_roles` row, not a "credential doesn't even resolve" 401.
9. **New import-linter contracts** (extending `pyproject.toml`
   `[[tool.importlinter.contracts]]`):
   - `app.core must not import app.platform` (mirrors the existing
     `core must not import plugins`/`core must not import business modules`
     contracts).
   - `app.modules must not import app.platform` (broader than just
     `platform.permissions` — forward-compatible with the other four
     platform modules the overview names, so a future `platform.workflow`
     doesn't need its own new contract to get the same protection; mirrors
     the existing `business modules must not import plugins` contract's
     shape).
   - `app.platform.permissions may import app.core and
     app.modules.masterdata only`, among `app`-internal packages (it
     obviously still imports FastAPI/SQLAlchemy/`argon2-cffi`/stdlib
     normally) — it needs `masterdata` for company-existence checks per the
     Phase 2 overview §5.1's permissions→masterdata edge; it must not import
     `sales`/`ledger`/`inventory`/`receivables`, `app.plugins`, or any future
     sibling `platform.*` package.
10. **Trusted-operator maintenance exemption, explicitly scoped, with a
    SEPARATE first-install carve-out — not one rule stretched to cover
    two different justifications.**

    **Rule 10a (reconstruction/replay exemption)**: a command is exempt
    from `require_permission()` entirely — not "protected by a system
    actor," genuinely exempt — **only if all three hold**: (a) it has no
    remotely reachable entry point (no HTTP route, no scheduler trigger,
    no support-console action); (b) invoking it requires deployment/
    database access, a stronger control than any in-app permission; (c) it
    solely reconstructs derived state from an authoritative persisted
    source, or replays already-durable work — it creates no *new* business
    facts. **Any future change that gives one of these tools a remotely
    reachable trigger immediately revokes the exemption.** Under Rule 10a:
    `rebuild_ar_balances.py`, `rebuild_stock_summary.py`, and
    `replay_outbox.py` qualify — each rebuilds/replays from an
    authoritative source with no HTTP exposure, and none creates a new
    fact. **`bootstrap_admin.py` does NOT qualify under 10a** — round 4
    caught this: it creates genuine new facts (the first `Company`, `User`,
    credentials, entitlement), which criterion (c) explicitly excludes.

    **Rule 10b (first-install carve-out, a distinct justification)**:
    `bootstrap_admin.py` is exempt for a different reason — it satisfies
    (a) and (b) above, and additionally runs at a point where **the role/
    permission catalog is already seeded (Action Item 2's migration), but
    no user, credential, or entitlement capable of authenticating an actor
    exists yet** — `require_permission()` has permissions to check against,
    but no one to check them for. Requiring an authorization check here
    would be circular (checking whether the operator creating the *first*
    administrator is
    themselves an administrator). This exemption is inherently
    self-limiting in a way 10a's isn't: once a first user/entitlement
    exists, re-running this tool to create *additional* users is exactly
    the "creates new business facts" case 10a excludes, so
    `bootstrap_admin.py`'s own idempotent design (Decision 6) must refuse
    to create a second user once any user already exists, rather than
    relying on this exemption to cover repeat runs.

    **`seed_demo.py` qualifies under NEITHER rule** — it creates genuine
    new business facts (customers, orders, invoices, payments) through
    real service calls, on a system that (unlike bootstrap) already has
    permission state to check against once a first user exists. It
    therefore needs Decision 4's `DEMO_SEED_ACTOR`, not an exemption.
    `DEMO_SEED_ACTOR` additionally **refuses to run unless an explicit
    override is set** (e.g. an env var the deployment must deliberately
    enable) — a demo-data seeder running unattended against a real
    production database is a distinct risk this decision does not want
    either exemption's "shell access is enough" reasoning to quietly cover.
11. **Global reference-data writes: removed from the HTTP surface
    entirely, not merely gated.** `POST /uom`, `POST /uom-conversions`,
    `POST /currencies`, `POST /exchange-rates` — all four already shipped
    in `v0.1.0` — are **deleted**, not permission-gated. Reason: these
    tables are global (shared across every company), while this ADR's
    entitlement model (Decision 1, Q1) resolves permissions **per company**
    via `user_company_roles` — there is no company-scoped permission that
    correctly answers "may this actor change an exchange rate that also
    affects every *other* company." Rather than invent a second,
    parallel cross-company authorization concept (a real scope expansion
    Q1 did not sign up for) to protect four rarely-written endpoints, these
    writes move to a new CLI tool, `app/cli/manage_reference_data.py`.
    **This tool is NOT exempt under Decision 10** — round 5 caught that
    calling it "Decision 10-shaped" was wrong: it creates real new facts
    (an exchange rate, a UoM conversion), failing Rule 10a's criterion (c),
    and it is not a first-install-only operation, so Rule 10b doesn't fit
    either. It follows **the same pattern as `seed_demo.py` instead**: it
    genuinely calls into `masterdata.service`'s existing create functions
    (a real service-layer call path), bound to its own purpose-specific
    `REFERENCE_DATA_ADMIN_ACTOR` (Decision 4) carrying exactly the
    reference-data-write permissions and nothing else — real
    `require_permission()` enforcement at the service boundary, operated
    from a CLI instead of HTTP, not an exemption. **The corresponding `GET`
    endpoints do NOT all stay anonymous** — round 4 caught this too: §7.2
    Q5's allowlist names only `currencies`/`UoM` reads; `GET
    /uom-conversions` and `GET /exchange-rates` are real, separate
    endpoints Q5 never covered, so they move behind the auth gate
    (`RequiresPermission`, not `Public()`) like any other business read;
    only `GET /uom` and `GET /currencies` stay anonymous. **This is a
    breaking change to the already-tagged `v0.1.0` API surface** — flagged
    in Consequences, scoped into the same rollout brief (Action Item 10) as
    this ADR's other breaking changes, not designed here.
12. **Company lifecycle gets the same treatment as reference data, for the
    same reason — round 4 caught that Decision 11 stopped one endpoint
    short.** `Company` is the tenant root, deliberately never
    `TenantScopedMixin` (README "Design Decisions"), so `list_companies`/
    `get_company` today return **every** company unfiltered and
    `create_company` has no check at all — the identical "global resource,
    per-company RBAC model" gap Decision 11 exists to close, just for the
    table that matters most (creating a tenant is a bigger act than editing
    an exchange rate). Two changes, not a uniform "remove everything" like
    Decision 11: (a) **`POST /companies` is removed from HTTP**, replaced by
    a new CLI tool, `app/cli/manage_companies.py`. **Same correction as
    Decision 11's new tool** (round 5): this is not a Decision 10 exemption
    either — creating a company is a real new business fact — so it binds
    its own purpose-specific `COMPANY_ADMIN_ACTOR` (Decision 4) and calls
    `masterdata.service.create_company` for real, enforcing
    `require_permission()` at the service boundary from a CLI instead of
    HTTP; (b) **`GET /companies` and
    `GET /companies/{id}` stay on HTTP but become entitlement-filtered**,
    not globally listed — each returns only companies the calling actor
    has a `user_company_roles` row for, resolved the same way Decision 8's
    login flow already resolves a multi-company user's eligible companies
    (no new resolution logic to design, this reuses that path). (b) is
    deliberately not a removal like (a): a logged-in user legitimately
    needs to see *their own* companies (e.g. to drive the `switch-company`
    UI), which is a real, ongoing feature — unlike unrestricted writes to
    someone else's exchange rates, unrestricted *reads scoped to your own
    entitlement* is not the same class of exposure Decision 11 was
    protecting against.

## Options Considered

### Decision 1 — data model shape: global roles vs. per-company custom roles

**Option A: global, code-seeded roles/permissions catalog** — chosen

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — a fixed enum-like catalog, migration-seeded, no admin UI needed yet |
| Matches Q1 | "Pure RBAC as the smallest coherent unit" — a role editor is scope creep |
| Phase 2 path | `platform.customfields`-style admin UI for custom roles is a natural *later* increment once this ships |

**Pros:** ships fast, testable in isolation, no new admin-UI dependency.
**Cons:** an enterprise ERP customer will eventually want to define
"regional sales manager" as a custom role — deferred, documented, not
blocking Phase 2's first release (§7.2 Q5: "permissions v1" must be
demoable standalone).

**Option B: per-company custom roles from day one**

**Pros:** matches long-term enterprise expectation immediately.
**Cons:** requires a role-definition admin UI/API before permissions itself
can ship — inverts the "permissions unblocks the other four modules"
ordering from §7.2 Q4; directly contradicts Q1's "smallest coherent unit."

**`user_company_roles` and `TenantScopedMixin` — why NOT mixed in.**
`TenantScopedMixin`'s enforcement (`app/core/db.py`'s `do_orm_execute` hook)
*requires* an already-bound `company_context` to run any `SELECT` against a
tenant-scoped table, fail-closed otherwise. But entitlement resolution is
precisely the step that determines *which* company to bind — asking "is
this user entitled to company X" necessarily happens **before** `X` is
bound as the active tenancy context. Applying `TenantScopedMixin` to
`user_company_roles` would make the hook demand a company context to
even check whether one should be granted — a real chicken-and-egg fail-
closed deadlock, not a hypothetical one. `user_company_roles` is therefore
a plain (non-tenant-scoped) table, queried by explicit `WHERE user_id = ?
AND company_id = ?` predicates in the resolution service, the same way
`Company` itself (also never tenant-scoped, for the identical "it's the
root you resolve *into* a tenant context, not a thing filtered *by* one"
reason) is queried today.

### Decision 2 — credential hashing algorithm

**Option A: Argon2id (`argon2-cffi`)** — chosen

**Pros:** current OWASP-recommended default (memory-hard, tunable,
side-channel resistant); actively maintained; no dependency on the wider
`passlib` compatibility-shim ecosystem (which itself now recommends
`argon2-cffi` directly for new projects).
**Cons:** an extra native (cffi) build dependency — acceptable; this project
already depends on `asyncpg` and `pgserver`, both native.

**Option B: bcrypt**

**Pros:** extremely well-proven, simpler dependency.
**Cons:** not memory-hard (weaker against GPU/ASIC attacks than Argon2id);
no compelling reason to choose the older default for a project starting
from zero today.

### Decision 3 — request-auth mechanism (the one axis §7.1/Q2 left open)

**Option A: opaque, server-side-validated bearer token in a `sessions`
table** — chosen

| Dimension | Assessment |
|-----------|------------|
| Revocation | Trivial — delete the row. Matters for ERP: a terminated employee's access must die immediately, not "expire eventually." |
| Infra cost | Zero new infra — reuses Postgres, the only datastore this project has ever needed. |
| API-first fit | README already frames this as "an API-only kernel... `/docs` is the interactive client" — a bearer token (`Authorization: Bearer <token>`, returned from `POST /auth/login`) fits a pure-API product more naturally than a cookie-based session, and works identically for a future frontend, CLI scripts, and `curl`. |

**Pros:** simplest mechanism that satisfies Q2's zero-external-dependency
constraint *and* gives real, immediate revocation — the property that
matters most for a self-hosted SME ERP where "someone just got fired" is a
routine, urgent event.
**Cons:** every authenticated request costs one indexed `sessions` lookup
(a `SELECT ... WHERE token_hash = ? AND expires_at > now()`) — negligible at
this project's scale, and the same cost class as the existing tenancy hook's
`with_loader_criteria` injection on every query.

**Option B: stateless JWT**

**Pros:** no DB lookup per request; natural fit for a distributed
microservice mesh.
**Cons:** revocation requires either short-lived tokens + refresh-token
churn (adds a second token type and a rotation protocol to design and
document) or a server-side denylist (which reintroduces the exact DB-lookup
cost Option A already pays, while adding JWT's own complexity on top — the
worst of both). This project is a **modular monolith by explicit,
repeatedly-reaffirmed design** (master-plan §4; ADR-001) — JWT's core
selling point (stateless validation across service boundaries) has no
audience here.

**Option C: server-side cookie session**

**Pros:** familiar web-app pattern, browser handles storage.
**Cons:** couples the auth mechanism to a browser client; awkward for CLI/
script/future-integration callers that Option A serves identically to a
browser. Functionally near-identical to Option A minus the API-first fit —
not chosen, but the underlying server-side-validated-token mechanism is the
same idea Option A generalizes.

### Decision 4 — where the authorization *check* lives, and what kind of "port" this is

**Option A: `app.core.authorization` as a context carrier + `platform.
permissions` as its sole producer, dependency-inverted at the composition
root (Codex's recommendation from the §6/§7 advisory discussion)** — chosen

**Pros:** business modules stay ignorant of concrete `platform.permissions`
(users/roles/credential storage/caching) the same way they are ignorant of
concrete `platform.plugins` today — `app.main` is already the trusted
composition root for exactly this kind of wiring. Authoritative because it
sits at the service/command boundary — but this protection is only as good
as the call path actually reaching one: a non-HTTP caller whose path does
pass through a service function (`seed_demo.py`, bound to its own
purpose-specific actor) gets real protection this way; a non-HTTP caller
whose path never reaches a service function at all (Decision 10's
maintenance tools) does **not**, and is not claimed to be — the ADR is
explicit that this layer's coverage is conditional on the call path, not
automatic for every non-HTTP entry point.
**Cons:** a new `app/core/authorization.py` module — a **narrow, deliberate
exception** to the "core is primitives only" shape, exactly mirroring the
precedent `app/core/tenancy.py` already set for identity/context primitives.

**Option B: router-level FastAPI dependency/decorator only**

**Pros:** simplest to write, explicit and greppable per-endpoint.
**Cons:** rejected in the §6/§7 advisory discussion for a concrete, present-
day reason: no non-HTTP path has a router in its call chain at all —
`app.cli.seed_demo` calls service functions directly, and
`app.cli.rebuild_ar_balances`/`app.cli.replay_outbox` go further still
(straight to the ORM or to `events.redispatch()`, bypassing even the
service layer — verified precisely in Decision 10, which is why those two
end up exempt rather than actor-bound). Router-only enforcement leaves
every non-HTTP entry point unguarded regardless of which of these shapes it
takes, the same class of gap ADR-006's hook registry was careful to avoid
for validation.

**Option C: a shared policy layer business modules import directly**

**Pros:** most direct, no abstraction indirection.
**Cons:** inverts today's "modules are independent, know nothing about each
other or about `plugins`" rule (the two import-linter contracts ADR-006
added specifically to make that CI-enforced physics, not convention) —
would be the first business-module→platform edge and sets a precedent for
every future platform module to claim the same exception. Rejected by
Codex during the advisory discussion for exactly this reason.

### Decision 5 — how the request gets bound: middleware vs. per-route dependency

**Option A: a new `AuthenticationMiddleware`, replacing
`tenancy_middleware`'s header-reading code (reusing `company_context(...)`
as the binding primitive)** — chosen

**Pros:** one wiring point, mirroring the precedent `tenancy_middleware`
already set — every request either gets a bound `ActorContext` +
`company_context` or fails closed before any route handler runs, the same
shape as today's header-based binding, just with a verified source instead
of a trusted header. Company-switch and token resolution happen in exactly
one place, not duplicated per-router.
**Cons:** middleware runs for *every* request, including the small
anonymous-reference-data allowlist (§7.2 Q5 — `currencies`/`UoM` reads);
those routes need an explicit `Public()` opt-out (Decision 7), so the
middleware must skip auth *resolution* (not just enforcement) for routes
already classified public — a routing-metadata lookup the middleware itself
must perform before the handler runs.

**Option B: a FastAPI `Depends(...)` on each protected router, no
middleware**

**Pros:** fully explicit per-route, no global request-processing hook.
**Cons:** every router must remember to add the dependency — reintroduces
the exact "forgot to protect a new endpoint" silent-failure risk Decision 7
exists to close, and duplicates company-context binding logic that
`tenancy_middleware` already centralizes once. Two different binding
mechanisms (middleware for tenancy, dependency for identity) for what is
conceptually one request-scoped "who is calling, as whom" resolution step
is also just more surface to keep in sync.

### Decision 6 — bootstrap (the chicken-and-egg first-user problem)

**Option A: one idempotent CLI command covering the full fresh-install
sequence** — chosen

**Pros:** no public "register the first admin" HTTP endpoint to secure
(that endpoint is itself a classic attack surface — "anyone can create the
first admin account" if exposed even briefly); matches the existing
`seed_demo.py` convention (`_ensure_reference_data` before `_ensure_company`
— verified in the actual file) of standalone scripts binding
`company_context(...)` directly for writes outside any HTTP request (this
tool itself needs no *authorization*-actor binding at all — see Decision
10's Rule 10b specifically, added in round 4, for why); **covers a
genuinely empty database**, not
just "add a user to an already-existing company" — round 1 of this ADR
under-specified exactly
this ordering, which is why it is now spelled out as one explicit sequence
in Decision 6 rather than left implicit.
**Cons:** requires shell/CLI access to the deployment for the very first
setup step — acceptable for a self-hosted product whose own positioning is
"Taiwanese SMEs can self-host" (i.e., someone already has shell access to
run `docker compose up`).

**Option B: a one-time HTTP setup endpoint, disabled after first use**

**Pros:** no CLI access required for the very first step.
**Cons:** real attack surface during the window it's live (a race between
the legitimate operator and anyone else who can reach the endpoint before
it's disabled); adds a stateful "has setup run yet" flag to get right under
concurrency. Not chosen — the CLI path costs nothing extra given this
project's existing CLI conventions.

### Decision 7 — fail-closed discipline: what each of the two layers actually catches

**Option A: router-level dependency that ACTUALLY enforces (not just labels)
+ service-boundary authoritative check + a startup crash on any
unclassified route** — chosen

**Pros:** mirrors this project's existing two-layer fail-closed pattern
almost exactly: `app.core.db`'s `do_orm_execute` hook is a structural,
can't-forget-it guarantee for tenant filtering, while individual services
still do their own explicit checks (fetch-before-mutate) as defense in
depth. Here, the two layers catch **two genuinely different failure
modes**, stated precisely (round 1 conflated them): the **startup check**
catches "a developer added a route and never thought about authorization at
all" (an unclassified route — a hard crash, impossible to ship silently);
the **router-level `RequiresPermission` dependency** is a real, executing
HTTP-layer gate (not metadata) for every classified route; the
**service-boundary `require_permission()` call** is the only layer that
protects CLI/replay/worker paths, and remains authoritative even for HTTP
requests (redundant with the router layer there, which is the point of
defense-in-depth). None of the three, individually or together, can detect
"the route is correctly classified but the *service function itself*
forgot its own `require_permission()` call" for a **non-HTTP** entry
point — that specific gap has no structural safety net and is closed only
by code review + the test suite (Action Item 11's coverage requirement),
the same honest limit this project already accepts for, e.g., a service
function that forgets to stamp `company_id` on an INSERT (ADR/HANDOFF's
existing "write-safety is a convention, not a hook" acknowledgment for
tenancy).
**Cons:** every new route needs one extra, explicit dependency
(`Public()` or `RequiresPermission("...")`) — a small, deliberate tax, the
same shape as the `TenantScopedMixin` tax every tenant-scoped model already
pays.

**Option B: service-boundary check only, no router-level net**

**Pros:** one mechanism, not two.
**Cons:** a forgotten `require_permission()` call in a new service function
fails **silently** — the endpoint just works, unprotected, until someone
notices in review or, worse, in production. Given this project's own
stated doctrine ("CI that has never actually run is not validated, no
matter how clean things look" — Week 8's headline lesson) an unenforced
"please remember" convention is exactly the kind of gap this project's
discipline exists to close.

### Decision 8 — entitlement freshness and the 401/403 boundary

**Option A: re-resolve entitlement every request; `active_company_id` is
only a pointer** — chosen

**Pros:** a revoked role or a disabled user takes effect immediately (next
request), not only when the user happens to call `switch-company` again —
matters for the same "someone just got fired" urgency Decision 3 already
argues from. Reuses the exact `sessions` lookup Decision 3 already pays for
(one extra join to `user_company_roles`/`role_permissions`, not a second
round-trip).
**Cons:** slightly more query work per request than trusting a
cached-at-login permission set — accepted for the security property.

**Option B: cache the resolved permission set in the session at login,
refresh only on `switch-company`**

**Pros:** cheaper per request.
**Cons:** a revoked entitlement or disabled account stays live until the
session naturally expires or the user switches companies — unacceptable
staleness for a permission system whose entire purpose is enforcing "who
may... right now."

### Decision 9 — import contract scope: `platform.permissions` only, or all of `platform.*`

**Option A: `app.modules must not import app.platform` (the whole
package)** — chosen

**Pros:** forward-compatible — the other four Phase 2 platform modules
(`plugins`, `customfields`, `workflow`, `integration`) get the same
protection automatically, without each needing its own new contract when
its ADR lands; mirrors the existing `business modules must not import
plugins` contract's shape (one contract, one package boundary, not one
contract per plugin).
**Cons:** slightly broader than strictly required today (only
`platform.permissions` exists yet) — accepted because a narrower contract
would just need re-widening at the next platform module's ADR, for no
benefit in the meantime.

**Option B: `app.modules must not import app.platform.permissions`
specifically**

**Pros:** minimal, states only what's true today.
**Cons:** every future `platform.*` sibling module needs its own new
import-linter contract added at its own ADR time to get the same
protection `permissions` has from day one — an easy step to forget,
unlike Option A where the protection is already there.

### Decision 10 — trusted-operator maintenance exemption: one rule, or two rules for two different justifications

**Option A: exempt, per two SEPARATE explicit qualifying rules — 10a
(reconstruction/replay) and 10b (first-install) — rather than one rule
stretched to cover both** — chosen

**Pros:** honest about where the real security boundary is, for both
shapes. A hard-coded actor that grants itself whatever permission it needs
adds no resistance — anyone who can execute these scripts with production
database credentials can already run migrations or manipulate the database
directly (matches this project's own existing, unremarked-on precedent:
`alembic upgrade head` has zero authorization today and this has never been
treated as a gap). Splitting into 10a/10b, rather than one rule, is what
round 4 forced: `bootstrap_admin.py` creates real new business facts
(fails 10a's criterion (c) honestly) but has an even *stronger*
justification than 10a's tools (no user/credential/entitlement capable of
authenticating an actor exists yet to check against, even though the role/
permission catalog itself is already seeded) — collapsing both into one
rule either falsely excludes bootstrap
or falsely weakens 10a's "creates no new facts" criterion for everyone
else. Two rules, each internally consistent, is more honest than one rule
quietly doing double duty.
**Cons:** does not, by itself, add any *new* protection to these tools —
their protection remains entirely operational (shell/deployment access,
credential scoping, audit logging of who ran what). This ADR does not
design that operational tooling; it is flagged as the actual security
control these tools rely on, for a future deployment/operations document
to specify. Two rules is also more to state and keep straight than one —
accepted because the alternative (one rule, silently wrong for one of the
four tools it's supposed to cover) is what round 4 actually caught.

**Option B: gate every non-HTTP tool uniformly with a bound system actor**

**Pros:** one rule for every non-HTTP path, no special-casing.
**Cons:** for the Rule 10a maintenance tools, there is no service-layer
call in their path for a bound actor to be checked *against* — the actor
would have to be checked at the tool's own entry point instead, which is
exactly "add ceremony without adding protection" (Question A's rejected
alternative). For `bootstrap_admin.py` specifically, checking a bound actor
when no user/credential/entitlement capable of authenticating an actor
exists yet is circular, not merely unnecessary. Also risks exactly the
failure mode Decision 4 now explicitly
avoids: a single shared, all-permissions actor that makes `is_system_actor`
an unconditional bypass in practice, even if that was never the intent.

### Decision 11 — global reference-data writes: gate with a cross-company permission, or remove from HTTP entirely

**Option A: remove the four write endpoints from HTTP, CLI-only going
forward** — chosen

**Pros:** avoids inventing a second, parallel *cross-company* authorization
concept (beyond Q1's settled per-company RBAC) just to protect four
endpoints that, per the existing test suite, are written rarely (once at
setup, occasionally thereafter) — proportionate to how seldom they're
actually used. No HTTP exposure at all — genuinely more restrictive than an
exemption, since the replacement CLI tool still enforces
`require_permission()` for real (Decision 4's `REFERENCE_DATA_ADMIN_ACTOR`
pattern, not a Decision 10 exemption — round 5's correction).
**Cons:** a real breaking change to already-tagged `v0.1.0` HTTP surface,
and the honest admission that Phase 2 does not yet have a "cross-company
operator" concept at all — if a future module (e.g. `platform.integration`
setting up cross-company webhook subscriptions) needs one for real, it will
have to be designed then, not inherited from this ADR.

**Option B: add a global/deployment-scoped operator role, gate these
endpoints with it**

**Pros:** keeps an HTTP path for what might otherwise become an
operationally annoying "always need CLI/shell access" requirement as the
product matures; a more complete-feeling permission model.
**Cons:** a real new authorization dimension (cross-company, not
company-scoped) layered onto an ADR whose entire Decision 1/Q1 premise is
"the smallest coherent unit, pure per-company RBAC" — scope creep this
ADR's own §7.2 Q4 reasoning ("permissions wants the most soak time, not the
most surface") argues against taking on now. Deferred, not rejected —
revisit if a real cross-company use case emerges.

**Option C: restrict the endpoints to a specific hardcoded "platform-operator" user/flag**

**Pros:** narrower than Option B, no new role concept.
**Cons:** a special-cased identity check outside the RBAC model entirely —
the kind of one-off exception this ADR's own Decision 4/Option C rejected
for the same reason (an escape hatch that erodes the model it's supposed
to sit inside). Not chosen.

### Decision 12 — company lifecycle: remove entirely (like Decision 11), or split creation from listing

**Option A: split — `POST /companies` removed (CLI-only); `GET /companies`/
`GET /companies/{id}` stay on HTTP but become entitlement-filtered** —
chosen (the scope owner's explicit direction, matching Decision 11's shape
for the write, but not the reads)

**Pros:** treats the write and the reads as genuinely different exposures,
not one uniform "delete everything" like Decision 11's *reference-data*
writes (which had no legitimate ongoing HTTP use case at all). Creating a
new tenant is a one-time, operator-level, CLI-appropriate act — exactly
Decision 11's reasoning. But *reading which companies you're entitled to*
is not the same shape of risk as *writing global data anyone could
corrupt* — it's a real, ongoing, legitimate feature (driving the
`switch-company` UI for a multi-company user, Decision 8), and entitlement-
filtering is a mechanism this ADR already has to build for the login flow
(Decision 8), so applying it to `GET /companies` is reuse, not new
machinery.
**Cons:** two different treatments for one table's endpoints is slightly
less uniform than "remove all four" would be — accepted because the
underlying exposures genuinely differ, and forcing uniformity here would
either over-remove a legitimate read feature or under-protect the write.

**Option B: remove all three endpoints entirely, like Decision 11's four**

**Pros:** maximal uniformity with Decision 11, simplest single rule
("global table → CLI-only, full stop").
**Cons:** removes a real feature with no CLI replacement that makes sense
— a logged-in user's own `switch-company` flow needs *some* way to learn
which companies they're entitled to; forcing that through a CLI tool
instead of the API a browser/frontend client is already talking to doesn't
serve any protection goal, since the data returned (the caller's own
entitlements) isn't sensitive to the caller who owns them.

## Trade-off Analysis

The real cost this ADR accepts is **surface area**: a new top-level package
(`app/platform/permissions/`), a new core module
(`app/core/authorization.py`), **six** new tables (`users`, `roles`,
`permissions`, `role_permissions`, `user_company_roles`, `sessions`), a new
middleware replacing the trust boundary of the existing one, and three new
import-linter contracts. That is deliberately the *heaviest* single Phase 2
module by design (§7.2 Q4's own reasoning: "permissions has the longest
security tail... it wants the most soak time"). The payoff is that every
other Phase 2 module's "who may…" question — plugin enable/configure,
custom-field admin, workflow approval, integration-trigger authority —
resolves to one `require_permission(...)` call each, with the hard design
work (data model, credential handling, request binding, fail-closed
discipline, entitlement freshness) paid exactly once, here.

## Consequences

- **Easier**: every subsequent Phase 2 module's authorization story is
  "define the permission codes, seed them, call `require_permission`" — no
  new mechanism to design.
- **Harder — this is a real breaking change to the API surface**, though
  narrower than "every endpoint" once the details are precise: every
  currently-reachable endpoint **except** the §7.2 Q5 anonymous
  reference-data-**read** allowlist (`GET /api/v1/currencies`,
  `GET /api/v1/uom` — an exact method/path list, not a vague "reference-data
  endpoints" category, per the round-4 fix below) will require a valid
  bearer token once `AuthenticationMiddleware` lands; `/health`, `/docs`,
  `/redoc`, `/docs/oauth2-redirect`, `/openapi.json`, and the new
  `/auth/login` route itself must be explicitly classified `Public()` too
  (Decision 7) — an easy category to forget precisely because they're not
  "business" endpoints, and the framework-generated four are not ordinary
  FastAPI routes with dependencies, so classifying them needs its own
  mechanism (flagged as open implementation-brief work, not resolved here).
  This directly affects: the README's "Try it" curl walkthrough (needs a
  login step prepended — Week 8 Decision 4 verified the *current*
  walkthrough is accurate; that verification will need re-doing once this
  ships, and will additionally need updating for Decision 11/12's endpoint
  removals if the walkthrough ever exercised them), every existing
  HTTP-level test **for a now-protected route** (needs an authenticated
  client fixture) — `tests/test_smoke.py`'s `/health` test and
  `tests/masterdata/test_reference_data_api.py`'s `GET /uom`/
  `GET /currencies` tests correctly stay unauthenticated; that same file's
  `test_create_uom_conversion`/`test_create_exchange_rate` (write) tests
  get **deleted** along with the endpoints they exercise (Decision 11) and
  replaced by CLI-level tests, while any existing `GET /uom-conversions`/
  `GET /exchange-rates` test coverage needs an authenticated fixture like
  any other now-protected read (round 4's correction — these two were never
  covered by §7.2 Q5, unlike `GET /uom`/`GET /currencies`). Three distinct
  non-HTTP migration shapes, not one uniform "add SYSTEM_ACTOR" fix (round
  1's error, only partly caught in round 2, fully corrected in round 4):
  (a) `seed_demo.py` binds `DEMO_SEED_ACTOR` (Decision 4) — the one script
  whose call path genuinely reaches a service-layer `require_permission()`
  check; (b) `rebuild_ar_balances.py`/`rebuild_stock_summary.py`/
  `replay_outbox.py` need **no authorization-actor change at all** — Rule
  10a's exemption, unchanged by this ADR beyond whatever operational
  logging a future deployment doc adds; `bootstrap_admin.py` is *also*
  exempt but under the separate Rule 10b, and its own idempotent design
  (Decision 6) needs an explicit "refuse to create a second user once any
  user exists" guard — round 4 caught that 10b's exemption is inherently
  first-run-only, so the tool itself must enforce that boundary, not just
  the ADR's prose; (c) `demo_o2c.py` is a real HTTP client
  (`httpx.AsyncClient`, no `company_context` binding today) and needs to log
  in and send a bearer token like any other API caller, not an
  authorization-actor change at all. The future integration worker
  (ADR-forthcoming) will need classifying against this same split once it
  exists. **This migration plan is deliberately NOT resolved in this
  ADR** — it is real implementation-brief-level work (a "Week 9" brief, in
  this project's established cadence) that should be scoped once this
  ADR's data model and mechanism are consensus-approved.
- **Harder — five already-shipped `v0.1.0` write endpoints are removed**:
  `POST /uom`, `POST /uom-conversions`, `POST /currencies`,
  `POST /exchange-rates` (Decision 11), and `POST /companies` (Decision
  12) — all become CLI-only. **Two of the four Decision 11 `GET`
  counterparts are NOT unaffected** (round 4's correction to this ADR's own
  earlier, incorrect claim): `GET /uom-conversions`/`GET /exchange-rates`
  move behind the auth gate; only `GET /uom`/`GET /currencies` stay
  anonymous. `GET /companies`/`GET /companies/{id}` (Decision 12) stay on
  HTTP but become entitlement-filtered, a genuinely different treatment
  from Decision 11's reads (reused from Decision 8's login-flow
  resolution, not new machinery) — not a removal, and not unfiltered
  either. This is a real, deliberate API-surface reduction (for the five
  writes) plus a real behavior change (for the two reclassified reads and
  the two now-filtered reads), not an oversight — flagged clearly here so
  none of it is discovered as a surprise regression during the rollout
  brief.
- **Revisit**: per-company custom roles (Decision 1 Option B, deferred);
  row-level and field-level authorization (§7.1, deferred to a later
  increment); the exact permission-code catalog per existing module (a
  by-endpoint audit is implementation-brief work, not ADR work); whether
  `app.core.authorization` ever needs to become a real `Protocol` (Decision
  4, only if/when an alternative resolver — e.g. external OIDC — is built);
  whether a real cross-company "operator" authorization concept is ever
  needed (Decision 11 Option B, deferred, not rejected, if a genuine
  cross-company use case emerges later — e.g. in `platform.integration`);
  whether any OTHER global/non-tenant-scoped resource in the codebase has
  the same gap Decisions 11/12 just closed for two instances — an explicit
  audit of every non-`TenantScopedMixin` table's HTTP surface is real
  implementation-brief work this ADR flags but does not itself perform.

## Consensus Revisions (round 1, 2026-08-16)

Round 1: **REJECTED**, 5 findings, all verified against the actual code
before being accepted as real (per this project's standing discipline —
none taken on faith):

1. **Bootstrap cycle unresolved** — round 1's Decision 6 only covered
   "add a user to an already-existing company," not a genuinely empty
   database. Verified against `seed_demo.py`'s real
   `_ensure_reference_data` → `_ensure_company` ordering. Fixed: Decision 6
   now specifies the full sequence explicitly.
2. **Decision 4's port/implementation/wiring contract underspecified**,
   and no explicit system/service actor for non-HTTP paths. Fixed: Decision
   4 now states plainly that `app.core.authorization` is a context carrier
   with exactly one producer (not a swappable interface, and says why not),
   and adds the explicit `SYSTEM_ACTOR` requirement for CLI/replay/worker,
   mirroring the existing `company_context(...)` discipline those scripts
   already follow.
3. **Decision 5 self-contradicted** ("replacing" the trust boundary in the
   summary vs. "alongside" in the chosen option) — a genuine authoring
   error, confirmed by re-reading both passages. Fixed: both now say
   "replaces the header-reading code, reuses the `company_context(...)`
   primitive," consistently. **Decision 7's router-classification safety
   net was also under-specified** — round 1 only checked that a route had
   *some* label, which does not detect a missing service-layer check. Fixed:
   Decision 7 now makes the router-level dependency a real, executing check
   (not metadata), and explicitly states which of the three layers catches
   which failure mode, including the honest limit that none of them catches
   "correctly classified route, but the service function's own check was
   forgotten" for non-HTTP paths.
4. **Data model/session invariants incomplete** for a downstream
   implementation brief: login identifier, `user_company_roles`
   tenant-scoping (a real fail-closed deadlock if mixed in, explained in
   Decision 1), session token hashing algorithm (must NOT be Argon2id — a
   fast deterministic hash is required for the token, unlike the password),
   per-request entitlement revalidation, 401/403 mapping, multi-company
   login selection. Fixed: Decision 1 gained the tenant-scoping explanation;
   Decision 3 gained the token-hashing correction; new **Decision 8** covers
   revalidation freshness and the 401/403 boundary.
5. **Import contract too narrow, missing an Options Considered section.**
   Fixed: Decision 9 (renumbered from 8) now reads `app.modules must not
   import app.platform` (not just `platform.permissions`, for
   forward-compatibility with the other four platform modules); "may
   import... only" is now explicit about being scoped to `app`-internal
   packages.

Also fixed, minor: the Context section's "zero JWT hits" claim was literally
false (two forward-looking *comments* mention JWT, correctly quoted and
contextualized now, not implied to be code); Trade-off Analysis said "five
new tables" against the ADR's own six-table Decision 1 — corrected to six;
the confirm_order citation now correctly attributes transaction ownership to
the caller (ADR-003 R1), not to `confirm_order` itself; Consequences'
"every existing endpoint/test" overstatement was scoped to account for the
Q5 anonymous allowlist (later refined further in round 2 below — the
five-CLI-scripts framing introduced here turned out itself to be
imprecise).

## Consensus Revisions (round 2, 2026-08-16)

Round 2: **REJECTED** again, 3 findings plus 2 cross-reference errors, all
verified against the actual code before being accepted as real:

1. **Decision 7's summary still self-contradicted**: it said the
   "classified correctly but the service check was forgotten" failure mode
   is guarded by the service check — but a *forgotten* check cannot guard
   anything. Fixed: the summary now states plainly that this failure mode
   has **no** structural safety net for non-HTTP callers, matching what the
   Options Considered section already said correctly.
2. **`demo_o2c.py` was wrongly grouped with the direct-service-call CLI
   scripts.** Verified against the actual file: `demo_o2c.py`'s own
   docstring says it is "an HTTP client hitting a *running* server," uses
   `httpx.AsyncClient`, and binds no `company_context` at all — unlike
   `seed_demo.py`/`rebuild_ar_balances.py`/`rebuild_stock_summary.py`/
   `replay_outbox.py`, which genuinely do call services directly. Fixed:
   Consequences and Action Item 7 now correctly split these into two
   different migration shapes (`SYSTEM_ACTOR` binding for the four
   direct-service scripts; a real login + bearer token for `demo_o2c.py`).
3. **"Every existing HTTP-level test" overstated the breaking change.**
   Verified against the actual test suite: `tests/test_smoke.py` (`/health`)
   and `tests/masterdata/test_reference_data_api.py` (currencies/UoM) are
   real, currently-anonymous tests that correctly **stay** anonymous once
   those routes are classified `Public()` (§7.2 Q5) — they were never going
   to need an authenticated fixture. Fixed: Consequences now says "every
   existing HTTP-level test for a now-protected route," not "every... test."

Two stale cross-references, also fixed: Decision 1's `TenantScopedMixin`
rationale pointed at "Decision 4" instead of its own Options Considered
entry; Decision 7's Options Considered cited "Action Item 9" for test
coverage when that's actually Action Item 10 (9 is the rollout brief).
Also added, prompted by a non-blocking Codex observation: Decision 8 now
states explicitly that a disabled user with an otherwise-valid session gets
403 (not 401) — the session is genuine, but the fresh `is_active` check
this decision already requires fails, the same shape as a revoked
entitlement.

## Consensus Revisions (round 3, 2026-08-16)

Round 3: **REJECTED**, 3 blocking findings, all verified against the actual
code before being accepted as real. Unlike rounds 1–2, one of these findings
(the CLI-classification error) exposed a genuine, unresolved **scope**
question — not just a wording/consistency bug — so the scope owner
explicitly paused further ADR edits and settled it with a dedicated
second-model Codex advisory discussion first, per this project's standing
"any decision goes through Codex consensus, report the result, wait for the
scope owner's go-ahead" discipline.

1. **Round 2's CLI classification was still wrong, and revealed a real gap,
   not just a naming error.** Verified against the actual files:
   `rebuild_ar_balances.py`/`rebuild_stock_summary.py` perform raw ORM
   queries directly (`select(Payment)`/`select(Invoice)` etc.), never
   calling into `service.py`; `replay_outbox.py` calls
   `events.redispatch()` directly, not a service function either. So
   "bind `SYSTEM_ACTOR` and the service-boundary check protects you" — this
   ADR's own core promise for non-HTTP paths — was **never true** for these
   three tools; there is no service-layer check in their call path for any
   bound actor to be checked against. This is the scope question resolved
   below as Decision 10.
2. **The HTTP-test correction still overclaimed**: round 2 said
   `test_reference_data_api.py` "remains unauthenticated" as a whole file,
   but it has two more tests round 2 missed — `test_create_uom_conversion`
   and `test_create_exchange_rate`, both **write** tests, which §7.2 Q5's
   "reads only" allowlist was never meant to cover. This surfaced the
   second scope question, resolved below as Decision 11.
3. **Decision 7's "forgotten check" framing was still self-contradictory**
   in the summary (fixed, wording-only — see the summary text now saying
   this failure mode has no structural safety net, not that it's "guarded
   by" the check that was, by definition, forgotten). Two stale
   cross-references (Decision 1 pointing at "Decision 4"; Decision 7 citing
   the wrong Action Item number for test coverage) were also fixed.

**Scope questions settled with the user** (via the advisory discussion, then
an explicit user decision — not decided unilaterally):

- **Question A** (Decision 10): are trusted-operator maintenance tools with
  no HTTP exposure exempt from `require_permission()` entirely, or gated
  with a bound actor anyway? **Resolved: exempt**, per an explicit
  three-part qualifying rule (no remote entry point; requires deployment
  access; only reconstructs/replays from an authoritative source) — Codex's
  full recommendation adopted, including treating `seed_demo.py`
  differently (it genuinely reaches service-layer checks, so it gets its
  own `DEMO_SEED_ACTOR` plus a production-run guard) and rejecting a single
  shared `SYSTEM_ACTOR` in favor of purpose-specific actors.
- **Question B** (Decision 11): should writes to global (cross-company)
  reference data — UoM conversions, exchange rates — be gated with a new
  cross-company permission concept, or removed from HTTP entirely?
  **Resolved: removed from HTTP**, CLI-only going forward — avoids
  inventing a cross-company authorization dimension this ADR's Q1 "smallest
  coherent unit, per-company RBAC" premise was never scoped to cover;
  flagged as a real breaking change to already-tagged `v0.1.0` HTTP surface
  (four endpoints deleted, not gated).

## Consensus Revisions (round 4, 2026-08-16)

Round 4: **REJECTED**, 5 findings, all verified against the actual code (or,
for `bootstrap_admin.py`, against this ADR's own explicit planned contract
— the file doesn't exist yet) before being accepted as real:

1. **Decision 10's rule didn't actually cover `bootstrap_admin.py`** — its
   own criterion (c), "creates no new business facts," directly contradicts
   Decision 6's description of what bootstrap does (creates a company,
   user, credentials, entitlement). Fixed: Decision 10 split into **Rule
   10a** (reconstruction/replay, covers the original three tools
   unchanged) and **Rule 10b** (first-install, a distinct justification —
   no permission state exists yet to check against — covering
   `bootstrap_admin.py` specifically, with an explicit note that this
   exemption is inherently first-run-only and the tool itself must enforce
   that boundary).
2. **Decision 4's Option B still contained the round-3 factual error**
   ("`rebuild_ar_balances`/`replay_outbox` call service functions
   directly") in a passage round 3's fix missed. Fixed: corrected to state
   accurately that these two bypass even the service layer (straight to
   ORM / `events.redispatch()`), which if anything strengthens the point
   being made there, not weakens it.
3. **Decision 11 overstated the anonymous-GET claim**: `GET
   /uom-conversions` and `GET /exchange-rates` are real, separate
   endpoints §7.2 Q5's allowlist never named (it names only currencies/UoM
   reads) — verified against the actual router. Fixed: Decision 11 and
   Consequences now correctly state only `GET /uom`/`GET /currencies` stay
   anonymous; the other two move behind the auth gate like any protected
   read.
4. **A new instance of the same global-vs-per-company gap, missed by
   Decision 11**: `POST /companies`/`GET /companies`/`GET /companies/{id}`
   have the identical problem Decision 11 was designed to close — `Company`
   is deliberately never tenant-scoped, so these are completely unfiltered
   and uncontrolled today. Resolved with the scope owner (not decided
   unilaterally, given its significance — creating a tenant is a bigger act
   than editing an exchange rate): new **Decision 12**, split by the scope
   owner's explicit direction — `POST /companies` removed (CLI-only, like
   Decision 11), but `GET /companies`/`GET /companies/{id}` stay on HTTP,
   reclassified as entitlement-filtered (reusing Decision 8's login-flow
   resolution) rather than removed, since reading one's own entitled
   companies is a real ongoing feature, unlike the write.
5. **Decision 7's claimed test-coverage safety net wasn't actually in the
   test list it cited.** Fixed: Action Item 11 now explicitly includes a
   test for "protected service function called with no actor bound / with
   insufficient permission both fail," plus the two new behaviors Decision
   10b/12 introduced (bootstrap's second-run refusal, entitlement-filtered
   company listing).

Also flagged, not yet resolved (added to Consequences' "Revisit" list, per
the scope owner's direction not to expand this round further): whether any
OTHER global/non-tenant-scoped table has the same gap two instances (
reference data, companies) have now surfaced — an explicit audit is real
implementation-brief work, not performed in this ADR.

## Consensus Revisions (round 5, 2026-08-16)

Round 5: **REJECTED**, 4 findings, all verified against the actual code (or
this ADR's own text) before being accepted as real. Per the scope owner's
explicit direction, the substantive one (finding 3) was fixed by applying
an already-established pattern (Decision 4's purpose-specific-actor +
real service-layer `require_permission()` model, the same one
`seed_demo.py`/`DEMO_SEED_ACTOR` already uses) rather than opening a new
advisory-discussion round — the scope owner confirmed this was the right
call before it was applied.

1. **Rule 10b's premise was imprecise**: it said "no permission system
   state exists," but Decision 6's own text says the entitlement is
   created "under a *seeded* `admin` role" — the role/permission catalog
   already exists (migration-seeded, Action Item 2); what's actually
   missing is a user/credential/entitlement capable of *authenticating* an
   actor. Fixed: Rule 10b's wording now says exactly that.
2. **Rule 10a/10b labeling wasn't applied consistently** — Decision 6's own
   prose, Decision 6's Options Considered, and Action Item 7 all still said
   generic "Decision 10" instead of specifically "Rule 10a" (the three
   reconstruction tools) or "Rule 10b" (bootstrap). Fixed in all three
   places.
3. **(Substantive) The replacement CLI tools Decisions 11/12 introduced
   had no stated authorization treatment**, and calling them
   "Decision-10-shaped" was actually wrong: `app/cli/
   manage_reference_data.py` and `app/cli/manage_companies.py` both create
   real new business facts, failing Rule 10a's criterion (c), and neither
   is a first-install-only operation, so Rule 10b doesn't fit either.
   Fixed: both now explicitly follow `seed_demo.py`'s pattern instead — a
   purpose-specific actor (`REFERENCE_DATA_ADMIN_ACTOR`,
   `COMPANY_ADMIN_ACTOR`) genuinely calling into `masterdata.service`'s
   existing functions, with real `require_permission()` enforcement at the
   service boundary (Decision 4), not a Decision 10 exemption of any kind.
4. **Action Item 11's test list covered `GET /companies` but not
   `GET /companies/{id}`**, though Decision 12 requires both to be
   entitlement-filtered. Fixed: added a test asserting a company outside
   the caller's entitlement returns 404 via the detail endpoint too.

## Action Items

1. [x] `/CODEX REVIEW ARCHITECTURE` — APPROVED, round 8 (2026-08-16).
2. [ ] Migration 0009: `users`, `roles`, `permissions`, `role_permissions`,
   `user_company_roles`, `sessions` + seed data for the initial role/
   permission catalog.
3. [ ] `app/core/authorization.py`: `ActorContext`, `ContextVar`, bind/reset
   functions, `require_permission()`, `ActionDeniedError`. No shared
   `SYSTEM_ACTOR` constant (Decision 4/10) — purpose-specific actors only,
   defined where they're used.
4. [ ] `app/platform/permissions/`: `service.py` (login incl. multi-company
   selection, token resolution + per-request entitlement re-resolution,
   switch-company), `models.py`, `schemas.py`, `router.py` (`/auth/login`,
   `/auth/switch-company`, `/auth/logout`), `Public`/`RequiresPermission`
   FastAPI dependencies.
5. [ ] `app/cli/bootstrap_admin.py`: the full fresh-install CLI sequence
   (Decision 6) — exempt per Rule 10b, no actor binding needed, but must
   itself refuse to create a second user once any `users` row exists
   (Rule 10b's first-run-only boundary, enforced in code, not just prose).
6. [ ] `app/main.py`: `AuthenticationMiddleware` replacing
   `tenancy_middleware`'s header-reading code (reusing `company_context`);
   startup route-classification check (Decision 7), including an explicit
   mechanism for the framework-generated `/docs`/`/redoc`/`/openapi.json`/
   `/docs/oauth2-redirect` routes.
7. [ ] `app/cli/seed_demo.py`: bind a new `DEMO_SEED_ACTOR` (Decision 4)
   with its own enumerated permission set, and refuse to run unless an
   explicit override is set (Decision 10). `rebuild_ar_balances.py`/
   `rebuild_stock_summary.py`/`replay_outbox.py`: **no change** — exempt
   per Rule 10a. `bootstrap_admin.py`: exempt per Rule 10b, but must gain
   the second-run refusal guard (Decision 6/10b). `demo_o2c.py`: add a
   login step + bearer token, not an authorization-actor change.
8. [ ] Three new import-linter contracts (Decision 9).
9. [ ] Remove `POST /uom`, `POST /uom-conversions`, `POST /currencies`,
   `POST /exchange-rates` from `app/modules/masterdata/router.py`
   (Decision 11); `app/cli/manage_reference_data.py` binding
   `REFERENCE_DATA_ADMIN_ACTOR`, calling the existing `masterdata.service`
   create functions for real; delete/replace
   `test_create_uom_conversion`/`test_create_exchange_rate`; reclassify
   `GET /uom-conversions`/`GET /exchange-rates` as protected reads (only
   `GET /uom`/`GET /currencies` stay `Public()`). Remove
   `POST /companies` (Decision 12); `app/cli/manage_companies.py` binding
   `COMPANY_ADMIN_ACTOR`, calling `masterdata.service.create_company` for
   real; `list_companies`/`get_company` filtered to the caller's
   `user_company_roles` entitlements, reusing Decision 8's login-flow
   resolution.
10. [ ] A dedicated migration/implementation brief for the breaking-change
    rollout across README/tests/demo scripts (flagged in Consequences, not
    scoped here).
11. [ ] Tests: login success/failure (incl. multi-company selection),
    token expiry/revocation, disabled-user immediate lockout, wrong-company
    entitlement (403), unclassified-route startup crash (Decision 7),
    cross-company entitlement isolation (mirroring the existing
    `test_cross_company_isolation.py` pattern), Argon2id hash verification,
    session-token-hash (SHA-256) lookup correctness, `DEMO_SEED_ACTOR`'s
    production-guard refusal, **a protected service function called with
    no `ActorContext` bound and with an insufficient permission both fail**
    (the specific coverage Decision 7 claims exists but round 4 found
    missing from this list), `bootstrap_admin.py` refusing a second run
    once a user exists (Rule 10b), `GET /companies` returning only the
    caller's entitled companies (Decision 12), and **`GET
    /companies/{id}` returning 404 (not the company's real data) for a
    company the caller is not entitled to** (round 5's addition — the
    collection endpoint alone doesn't prove the detail endpoint is filtered
    too).
