# Week 9 — `platform.permissions` implementation brief (for architecture consensus review)

Status: **ACCEPTED** — Codex plan consensus APPROVED, round 6 (2026-08-17).
Implementation may begin per the sequencing in Decision 1, pending the scope owner's
explicit go-ahead (this project's standing push/implementation gate).
Author: Claude (architect)
Date: 2026-08-17
Scope owner: Ryan

Consensus history: round 1 → REJECTED (6 findings — no service-boundary
`require_permission()` scoping in Slice D; `Public`/`RequiresPermission` placed
somewhere business modules can't import per Slice C's own contract; framework
public-route handling left undesigned; migration 0009 implicitly re-edited in D;
Slice B's `/auth/switch-company`/`/auth/logout` had no stated auth mechanism;
security tests all deferred to Slice F). Round 2 → REJECTED (7 findings — the
public-route mechanism still missed `GET /uom`/`GET /currencies`; "every route gets
`RequiresPermission`" contradicted the anonymous-read allowlist;
switch-company/logout had no permission classification; Slice D's cleanup of Slice
B's inline token handling was wrongly optional; the `Public`/`RequiresPermission`
relocation contradicted ADR-009's own Action Item 4 — a real inconsistency in the
already-ACCEPTED ADR, not a re-litigation; Slice A had no test list; migration 0010
granted permission codes without granting them to the `admin` role, which would
have locked the bootstrap admin out). Round 3 → REJECTED (2 findings — Slice B
attaching a real `RequiresPermission` dependency before any middleware exists would
fail-closed-reject every call; `ActorContext` has no session identifier, leaving
`switch_company()`/`logout()` unable to identify their target `sessions` row once
inline token parsing is removed). Round 4 → REJECTED (2 findings — `ContextVar`
bind/reset lifecycle imprecision: `require_permission()` reads an already-bound
context rather than taking one as an argument, and neither slice specified
token-based reset in `try`/`finally`). Round 5 → REJECTED (1 finding —
"AuthenticationMiddleware becomes the sole caller of `actor_context(...)`" was
overstated and contradicted Slice D/E's CLI scripts, which must bind their own
purpose-specific actors). Round 6 → **APPROVED, no new findings**. Every finding
across all six rounds was verified against the actual repo/ADR text before being
accepted as real, per this project's standing discipline — see "Consensus
Revisions" below for the full round-by-round record, and note the **one item that
is a real correction to the already-ACCEPTED `ADR-009-platform-permissions.md`
itself** (round 2 finding 5, not a re-litigation of any ADR-009 Decision).

This is an **implementation brief**, not a new ADR. `docs/adr/ADR-009-platform-permissions.md`
is already ACCEPTED (Codex architecture consensus, round 8) and is the sole source of
*what* to build — every Decision and Action Item cited below refers back to it verbatim,
none re-litigated here. This brief's job is narrower: **how to sequence ADR-009's 11
Action Items into independently landable, independently Codex-diff-reviewable slices**,
per ADR-009's own Action Item 10 ("a dedicated migration/implementation brief for the
breaking-change rollout ... not scoped here").

**One exception — a real correction to ADR-009 itself, not a re-litigation (round 2
finding 5)**: this brief's own round-1 revision moved `Public`/`RequiresPermission`
from `app.platform.permissions` (where ADR-009's Action Item 4 literally places them)
to `app.core.authorization`, because Action Item 4's placement is unimplementable
given ADR-009's own Decision 9 import contract (`app.modules must not import
app.platform` — a business-module router could never import them from
`platform.permissions`). This is a genuine internal inconsistency in the already-
ACCEPTED ADR, only surfaced by trying to actually sequence implementation, not a
scope change Claude decided unilaterally — flagged explicitly to the scope owner (see
`HANDOFF.md`/chat record), with a short dated correction note added directly to
ADR-009's own Action Item 4 (not silently overridden by this downstream document).

---

## 0. Context / where Phase 2 stands

Phase 1 ("Kernel") is complete and tagged `v0.1.0` on a public, CI-green GitHub remote.
Phase 2 discovery has produced two ACCEPTED design documents
(`PHASE2-platform-architecture-overview.md`, `ADR-009-platform-permissions.md`) and
**zero lines of Phase 2 application code**. See `HANDOFF.md` for the full narrative.

### Verified current-state facts (checked against the repo, not assumed)

- `app/core/` has no `authorization.py` — confirmed via `ls app/core/`.
- `app/platform/` does not exist — confirmed via `ls app/`.
- `app/main.py` still wires `tenancy_middleware` reading a trusted `X-Company-Id`
  header; no `AuthenticationMiddleware`, no route-classification startup check.
- No `users`/`roles`/`permissions`/`role_permissions`/`user_company_roles`/`sessions`
  tables in any existing migration (`alembic/versions/` has no such revision).
- `app/cli/seed_demo.py`, `app/cli/rebuild_ar_balances.py`,
  `app/cli/rebuild_stock_summary.py`, `app/cli/replay_outbox.py`, `app/cli/demo_o2c.py`
  exist today exactly as ADR-009's Decision 10/Consequences describe them (three
  direct-service/ORM/redispatch scripts, one real HTTP client) — this brief does not
  re-verify that; ADR-009 rounds 2–4 already did, against the actual files.
- `app/modules/masterdata/router.py` currently exposes `POST /uom`,
  `POST /uom-conversions`, `POST /currencies`, `POST /exchange-rates`, and
  `app/modules/masterdata/router.py`'s company endpoints expose `POST /companies`,
  `GET /companies`, `GET /companies/{id}` — all unauthenticated today, per ADR-009
  Decisions 11/12.
- `pyproject.toml`'s `[[tool.importlinter.contracts]]` has the existing
  `core must not import plugins` / `business modules must not import plugins`
  contracts (the precedent Decision 9's new contracts mirror) but none yet for
  `app.platform`.

## 1. Goal & definition of done

Land ADR-009 in full — all 11 Action Items — as six ordered, independently
Codex-diff-reviewed slices (A–F below), each leaving `main` in a fully green
(`make check`-equivalent) state. Definition of done: `platform.permissions` exists,
`AuthenticationMiddleware` is the sole request-auth path, every non-`Public()` route is
enforced at both router and service boundary, all four non-HTTP script migrations
(Decision 10/Consequences) are complete, the five breaking-change endpoint removals
(Decisions 11/12) are live, and Action Item 11's full test list passes.

**Non-DoD** (explicitly out of this brief, flagged in ADR-009's own "Revisit" list):
per-company custom roles, row/field-level authorization, a real cross-company
"operator" concept, an audit of *other* global non-tenant-scoped tables beyond
reference data and `Company`, `platform.plugins` (the next ADR).

## 2. Decisions

### Decision 1 — Slice order and what "independently landable" means here

Six slices, strict order A→B→C→D→E→F. Each slice must leave `main` green — including
D and E, which are the two breaking-change cutover points — meaning D's and E's own
scope must include fixing every test/script/doc their cutover breaks, not deferring
cleanup to F. F is verification/completeness, not a dumping ground for D/E's own
breakage.

| Slice | ADR-009 Action Items | Breaking? |
|---|---|---|
| A | 2 (migration), 3 (`app/core/authorization.py`) | No — pure additive |
| B | 4 (`platform.permissions` package + bootstrap CLI, Action Item 5) | No — login endpoint exists, nothing enforces it yet |
| C | 8 (import-linter contracts) | No |
| D | 6 (`AuthenticationMiddleware` + startup check), 7 (non-HTTP script migration) | **Yes** — auth becomes mandatory on (almost) every route |
| E | 9 (reference-data + company endpoint removal/reclassification) | **Yes** — second, narrower breaking change |
| F | 10 (README/demo rollout), 11 (test completeness) | No — verification only |

Rationale for A→B→C ordering before the D/E cutover: A and B are pure additions with
zero behavior change to any existing route — they can be built, tested, and
Codex-reviewed with no risk to Phase 1's working API, and B's `bootstrap_admin.py`
existing before D lands means the first real admin user/session can be created and
manually exercised against the *new* login endpoint before anything starts requiring
it. C is a small, independent, low-risk addition that is easiest to land whenever the
`app.platform` package exists — placed before D so the import boundary is
CI-enforced from the moment there's anything to enforce it against, not retrofitted
after D/E have already been written.

### Decision 2 — Slice A: data model + authorization primitive

Migration 0009 creates the six tables per ADR-009 Decision 1, seeded with the initial
role/permission catalog (exact codes are implementation-brief work per ADR-009's own
"Revisit" list — a by-endpoint permission-code audit happens inside this slice, not
before it, since it requires enumerating every existing protected route, which is
Slice D's job to classify — **Slice A therefore seeds a minimal placeholder catalog
sufficient for Slice A/B's own tests** (e.g. an `admin` role with a small enumerated
set), with the full by-endpoint catalog finalized in Slice D when every route actually
gets classified).

`app/core/authorization.py`: `ActorContext` (frozen dataclass), `ContextVar`, bind/
reset functions mirroring `app.core.tenancy`'s existing shape exactly, `ActionDeniedError`,
`require_permission(action: str)` raising fail-closed if no context is bound or the
action isn't in `permission_codes`. No `SYSTEM_ACTOR` constant (ADR-009 Decision 4/10
explicit prohibition) — purpose-specific actor constants are defined where they're
used (Slices B/D), not here. **Amended per round-1 review**: this module also defines
the `Public()`/`RequiresPermission(code)` FastAPI dependency factories — not
`app.platform.permissions` (see Decision 3 below for why: business modules may import
`app.core` but not `app.platform`, so any dependency a business-module router
attaches must live in `app.core.authorization`, the port, not in the concrete
`platform.permissions` implementation).

Both parts land in one slice because `authorization.py`'s tests need real
`role_permissions` rows to resolve against, and the migration alone has no code to
exercise it — reviewing them together is more coherent than an artificial split.

**Migration immutability**: 0009's seed data is a **minimal placeholder catalog only**
(Decision 5 below explains why the full catalog can't be finalized here). Once 0009
lands on `main`, it is never edited again — the full catalog arrives via a **new**
migration in Slice D, standard Alembic append-only discipline, the same rule this
project already applies to every other migration.

**0009's placeholder catalog includes one baseline code from day one (round 2
finding 3/7): `session.manage`**, granted to the seeded `admin` role in 0009 itself
(not deferred to 0010) — this is what `/auth/switch-company`/`/auth/logout` require
(Decision 3 below); it must exist and be granted before Slice D's cutover, since the
very first bootstrap admin (Slice B) needs to be able to switch company/log out
immediately after D lands, with no gap where they're locked out.

**Tests landing in this slice** (round 2 finding 6 — was missing): `ActorContext`
`ContextVar` bind/reset round-trips; `require_permission()` raises `ActionDeniedError`
fail-closed with no context bound; `require_permission()` raises with a bound context
lacking the required code; migration 0009's placeholder catalog (incl. `admin` →
`session.manage`) resolves correctly via a direct query.

### Decision 3 — Slice B: `platform.permissions` package + bootstrap CLI

`app/platform/permissions/`: `models.py` (SQLAlchemy models for the six tables from
Slice A's migration), `service.py` (`login()` incl. multi-company selection per
Decision 8, `resolve_actor(token)`, `switch_company()`, `logout()`), `schemas.py`,
`router.py` (`POST /auth/login`, `POST /auth/switch-company`, `POST /auth/logout`).

**`Public`/`RequiresPermission` do NOT live in `app/platform/permissions/`** — round
1 review caught this: Slice C's import-linter contract (`app.modules must not import
app.platform`) would make it impossible for any business-module router to use them.
They are defined in **`app/core/authorization.py`** instead (Slice A, Decision 2
below — amended), alongside `ActorContext`/`require_permission()`, since that module
*is* the port business modules are allowed to depend on (ADR-009 Decision 4). `app.
platform.permissions` supplies the concrete `ActorContext` producer that `app.main`
(composition root) wires into the `ContextVar` those dependencies read from —
`app.core.authorization` itself still never imports `app.platform` or any business
module, per the existing contract; it only defines the dependency callables and reads
the already-bound `ContextVar`.

`/auth/switch-company` and `/auth/logout` need to identify the calling session
**before `AuthenticationMiddleware` exists** (that's Slice D) — this slice's router
handlers therefore extract `Authorization: Bearer <token>` and call
`platform.permissions.service.resolve_actor(token)` **directly inline**, not via any
middleware-bound `ActorContext`. This is the same resolution service Slice D's
middleware will later call the same way, not a second, divergent authentication
mechanism — **this inline resolution is temporary and Slice D MUST remove it (round
2 finding 4 — was wrongly left optional)**: once `AuthenticationMiddleware` exists,
these two handlers are rewritten to read the already-bound `ActorContext` instead,
so `AuthenticationMiddleware` is genuinely the sole request-auth path once D lands,
matching this brief's own DoD. `/auth/login` itself needs no prior authentication
(that's its whole purpose, and it stays exempt from classification via the public
allowlist, Decision 5 below).

**Classification and enforcement timing (round 2 finding 3, amended per round 3
finding 1)**: `/auth/switch-company` and `/auth/logout`'s **intended** permission is
`RequiresPermission("session.manage")` — a baseline permission code every seeded role
(starting with `admin`, Slice A's 0009 seed — see Decision 2 above) is granted, since
switching one's own active company or logging out one's own session is self-scoped,
not a privileged action over another entity, but still requires a genuinely
authenticated caller (never `Public()`). **The `RequiresPermission(...)` FastAPI
dependency itself is NOT attached to these two routes until Slice D** (round 3
finding 1 — attaching it here would fail-closed-reject every call, since a `Depends()`
dependency runs *before* the handler body, and no `ActorContext` is bound anywhere
yet in Slice B). Slice D's classification sweep is what actually attaches
`RequiresPermission("session.manage")` as a real dependency on these two routes, at
the same moment it removes this slice's manual binding below (both changes land
together in D, not staggered). `/auth/login` alone is `Public()` (Decision 5's
allowlist) since it is the one endpoint with no caller identity yet to check.

**Session identity for `switch_company()`/`logout()` (round 3 finding 2)**:
`ActorContext` (ADR-009 Decision 4) deliberately carries only `user_id`,
`permission_codes`, `is_system_actor` — no session identifier, since which session a
request came from isn't an authorization-relevant fact and doesn't belong on the
`app.core` port. `switch_company()`/`logout()` still need to know *which* `sessions`
row to update/revoke, though. `platform.permissions` therefore owns its own,
separate `current_session_id: ContextVar[uuid.UUID | None]` (defined in
`app.platform.permissions`, not `app.core.authorization` — this is a
`platform.permissions`-internal concern, not part of the authorization port, so it
adds no new port surface).

**Bind/reset mechanism, precise (round 4 findings 1/2 — was imprecise: `require_
permission()` only ever reads an already-bound `ActorContext`, it takes no context
argument, and neither slice specified unbinding)**: `app.core.authorization` gains an
`actor_context(ctx: ActorContext)` context manager (mirroring `company_context(...)`'s
existing shape exactly) that `.set()`s the `ActorContext` `ContextVar` on enter and
`.reset(token)`s it on exit via `try/finally`, exception-safe. `platform.permissions`
gains the equivalent `session_context(session_id)` context manager for
`current_session_id`. In this slice, `/auth/switch-company` and `/auth/logout`'s
handlers do: `resolve_actor(token)` → then, nested, `with actor_context(ctx),
session_context(session_id):` → **inside** that binding, call
`require_permission("session.manage")` (which reads the just-bound `ContextVar`, not
a passed argument) → then call `switch_company()`/`logout()`. Leaving the `with`
block (success or exception) resets both `ContextVar`s via their tokens, in reverse
bind order (`current_session_id` first, then `ActorContext`) — no leakage across
requests either way. In Slice D, `AuthenticationMiddleware` becomes **the sole binder
on the HTTP request path** (round 5 finding — "sole caller" was overstated) for
`actor_context(...)`/`session_context(...)` (plus the pre-existing `company_context
(...)`, reset in the same reverse-of-bind order), wrapping the downstream call chain
for the whole request; Slice B's manual `with` blocks inside the two handlers are
removed entirely at that point, matching the mandatory-cleanup requirement above.
**This does not contradict Slice D/E's CLI scripts** (`seed_demo.py`'s
`DEMO_SEED_ACTOR`, `manage_reference_data.py`'s `REFERENCE_DATA_ADMIN_ACTOR`,
`manage_companies.py`'s `COMPANY_ADMIN_ACTOR`) — those bind `actor_context(...)`
themselves, in their own exception-safe `with` scope, exactly like Slice B's handlers
did before cleanup, since they run outside any HTTP request and have no middleware to
bind for them. `session_context(...)` stays middleware-only even after Slice B's
cleanup — a CLI invocation has no `sessions` row and represents no session to track,
only an `ActorContext`.

`app/cli/bootstrap_admin.py`: the full fresh-install sequence per Decision 6 — ensure
reference data → ensure first `Company` → create first `User` (credentials via CLI
arg/env var, never hardcoded) → create the `user_company_roles` row under the seeded
`admin` role. Exempt per Rule 10b (no actor binding); **must itself refuse to create a
second user once any `users` row exists** (Rule 10b's first-run-only boundary,
Decision 6/10b — enforced in code this slice, not deferred).

Router is wired into `app.main` but **no route outside `/auth/*` requires auth yet** —
`AuthenticationMiddleware` doesn't exist until Slice D, so this slice's own router is
reachable but every other existing endpoint is unaffected. This is deliberately the
"build and manually verify the new mechanism in isolation before flipping the switch"
slice.

**Tests landing in this slice** (round 1 finding 6 — not deferred to Slice F): login
success/failure incl. multi-company selection, Argon2id hash verification,
session-token SHA-256 lookup correctness, `switch-company`/`logout`'s inline token
resolution (valid/expired/missing token), `bootstrap_admin.py` refusing a second run
once a user exists, and **(round 4 addition)** `actor_context(...)`/
`session_context(...)` leave no bound `ContextVar` after a successful request and
after a request that raises (e.g. an insufficient-permission `ActionDeniedError`
mid-handler) — asserted by reading the `ContextVar`'s value directly after the
handler returns/raises in a test.

### Decision 4 — Slice C: import-linter contracts

The three contracts from Decision 9, added once `app/platform/permissions/` exists
(Slice B): `app.core must not import app.platform`, `app.modules must not import
app.platform` (the whole package, per Decision 9's Option A), `app.platform.permissions
may import app.core and app.modules.masterdata only`. Run `uv run lint-imports`
locally before commit — this slice's only acceptance criterion.

### Decision 5 — Slice D: the cutover (`AuthenticationMiddleware` + non-HTTP migration)

This is the slice ADR-009's Consequences section calls "a real breaking change to the
API surface." Scope, all landing together because splitting them would leave `main` in
a broken intermediate state (middleware live but scripts not yet migrated = every demo
script broken on `main`):

- **Migration 0010**: the full by-endpoint permission-code catalog (see below),
  additive-only against 0009's placeholder rows — never edits 0009 (round 1 finding 4).
  **Must also insert the `role_permissions` rows granting every new code to the
  `admin` role** (round 2 finding 7 — a code with no grant is unusable by the
  bootstrap admin created in Slice B, which would lock that admin out of every
  newly-protected route the moment this slice's middleware goes live).
- `app/main.py`: `AuthenticationMiddleware` replaces `tenancy_middleware`'s
  header-reading function entirely (deleted, not deprecated), reuses
  `company_context(...)` as the binding primitive per Decision 5. Resolves
  `Authorization: Bearer <token>` via `platform.permissions`, binds
  `company_context(...)`, `ActorContext`, **and `platform.permissions`'s own
  `current_session_id` ContextVar** (round 3 finding 2 — Decision 3 above
  introduces this; middleware is the second and final place it gets bound, replacing
  Slice B's own manual binding inside the `/auth/switch-company`/`/auth/logout`
  handlers, which this slice removes per the mandatory cleanup above).
- `/auth/switch-company` and `/auth/logout` finally get the real
  `RequiresPermission("session.manage")` FastAPI dependency attached here (round 3
  finding 1 — Slice B could only do the equivalent check manually, since no
  `ActorContext` was bound anywhere until this middleware exists).
- **Public-route mechanism, two tiers (round 2 findings 1/2 — the round-1 fix only
  covered framework paths, missing `GET /uom`/`GET /currencies`)**:
  - **Tier 1 (static allowlist)**: `/docs`, `/redoc`, `/openapi.json`,
    `/docs/oauth2-redirect` only — the framework-generated paths that genuinely
    cannot carry an ordinary FastAPI dependency the same way a normal route can.
    `AuthenticationMiddleware` checks the request path against this fixed list
    **before** attempting token resolution and skips auth entirely on an exact match.
    The startup route-classification check (below) exempts exactly this same list
    from its crash — one shared constant both mechanisms read, not two lists that
    could drift.
  - **Tier 2 (routing-metadata lookup, covers every ordinary route — `/health`,
    `/auth/login`, `GET /uom`, `GET /currencies`, and every business route)**: for any
    path not on the Tier 1 list, the middleware resolves the matching route by
    iterating `app.routes` and calling each route's `.matches(scope)` (the same
    mechanism Starlette's own router uses internally), then inspects whether that
    route's `dependencies` include the `Public()` marker. If so, skip token
    resolution — no `Authorization` header is required or read for that request. If
    not (i.e. it carries `RequiresPermission(code)`), proceed to resolve the bearer
    token and bind `ActorContext`/`company_context(...)`, and let the
    `RequiresPermission(code)` FastAPI dependency perform the actual per-route
    enforcement during normal request handling — this is the real, executing
    router-level gate ADR-009 Decision 7(a) requires, not metadata. A path matching no
    route in `app.routes` at all is never treated as public by default (fail-closed).
- Startup route-classification check (Decision 7c): iterate `app.routes`, **excluding
  the Tier 1 allowlist above** (those routes are never FastAPI-dependency-classified
  and are trusted public purely by the Tier 1 mechanism), crash if any remaining route
  carries neither `Public()` nor `RequiresPermission(code)`.
- **Every route gets classified as either `Public()` or `RequiresPermission(code)`
  (round 2 finding 2 — was overstated as "every route gets `RequiresPermission`",
  self-contradicting the anonymous-read allowlist)**: `GET /uom` and `GET /currencies`
  get `Public()` (ADR-009 Decision 11/§7.2 Q5's allowlist, unchanged from today's
  anonymous access); `/auth/login` gets `Public()` (Decision 3 above); `/health` gets
  `Public()`; `/auth/switch-company`/`/auth/logout` get
  `RequiresPermission("session.manage")` (Decision 3 above); every other existing
  business-module route gets `RequiresPermission(code)` using the full permission-code
  catalog finalized here (Migration 0010 above) — this is the by-endpoint audit
  ADR-009 flags as implementation-brief work. **The dependency itself is attached at
  each router's route decorator** (`@router.post(..., dependencies=[Depends(
  RequiresPermission("sales.order.create"))])` or equivalent), importing
  `RequiresPermission`/`Public` from `app.core.authorization` (Slice A, amended — see
  the ADR-009 correction note below; never from `app.platform.permissions`, which
  every business module is contractually forbidden from importing per Slice C).
- **Service-boundary enforcement (round 1 finding 1 — was missing)**: per ADR-009
  Decision 7's two-real-layers requirement, this slice also adds a
  `require_permission(code)` call inside the corresponding `service.py` function for
  every route classified above, using the same permission codes — not only the router
  dependency. This is the layer that protects `seed_demo.py`'s `DEMO_SEED_ACTOR` path
  (next bullet) and is authoritative even for HTTP requests. Scoped per business
  module: `masterdata`, `sales`, `ledger`, `inventory`, `receivables` each gain
  `require_permission()` calls in their existing `service.py` write (and, where a
  permission code is defined for it, read) functions — enumerating the exact
  function-by-function list is this slice's own implementation work, not restated
  here field-by-field, but it is explicitly in scope, not optional or deferred.
- `app/cli/seed_demo.py`: binds new `DEMO_SEED_ACTOR` (enumerated permission set
  matching exactly what it needs), refuses to run unless an explicit override env var
  is set (Decision 10's production-run guard).
- `app/cli/rebuild_ar_balances.py`, `app/cli/rebuild_stock_summary.py`,
  `app/cli/replay_outbox.py`: **no change** — Rule 10a exemption, unchanged.
- `app/cli/demo_o2c.py`: add a login step (`POST /auth/login`) + attach the returned
  bearer token to every subsequent request — not an actor-binding change, a real HTTP
  auth flow like any other API caller.
- README's "Try it" curl walkthrough: updated with a login step prepended (Week 8
  Decision 4 verified the *current* walkthrough is accurate; this slice re-verifies it
  by actually running the updated commands, per this project's "run the real command"
  testing doctrine — not deferred to Slice F, since a broken walkthrough on `main`
  between D and F would misrepresent the shipped state).
- Every existing HTTP-level test **for a now-protected route** gains an authenticated
  client fixture (`tests/test_smoke.py`'s `/health` test and
  `tests/masterdata/test_reference_data_api.py`'s `GET /uom`/`GET /currencies` tests
  correctly stay unauthenticated, per ADR-009's own round-2 correction).

**Tests landing in this slice** (round 1 finding 6 — not deferred to Slice F):
`AuthenticationMiddleware` behavior (valid/expired/missing/malformed token; disabled
user gets 403 not 401 per Decision 8), the allowlist skip actually bypasses auth for
every listed path and nothing else, startup route-classification crash (an
intentionally-unclassified test route triggers it), wrong-company entitlement → 403,
cross-company entitlement isolation, `DEMO_SEED_ACTOR`'s production-guard refusal, a
protected service function called with no `ActorContext` bound **and** with
insufficient permission both failing (the specific gap ADR-009 round 4 flagged),
`demo_o2c.py`'s login-then-call flow against a live server, and **(round 4 addition)**
`AuthenticationMiddleware` itself leaves no bound `ActorContext`/`current_session_id`/
`company_context` after a request completes or raises — same leak-check as Slice B's,
now against the middleware that has become the sole binder.

### Decision 6 — Slice E: reference-data + company endpoint changes

Remove `POST /uom`, `POST /uom-conversions`, `POST /currencies`,
`POST /exchange-rates` from `app/modules/masterdata/router.py` (Decision 11). New
`app/cli/manage_reference_data.py` binding `REFERENCE_DATA_ADMIN_ACTOR`, calling the
existing `masterdata.service` create functions for real (real
`require_permission()` at the service boundary, not a Rule 10 exemption — ADR-009
round 5's correction). Delete `test_create_uom_conversion`/`test_create_exchange_rate`
(the endpoints they exercise no longer exist), replace with CLI-level tests.
Reclassify `GET /uom-conversions`/`GET /exchange-rates` as protected reads (only
`GET /uom`/`GET /currencies` stay `Public()`, per ADR-009 round 4's correction).

Remove `POST /companies` (Decision 12); new `app/cli/manage_companies.py` binding
`COMPANY_ADMIN_ACTOR`, calling `masterdata.service.create_company` for real.
`list_companies`/`get_company` filtered to the caller's `user_company_roles`
entitlements (reusing Decision 8's login-flow resolution — no new resolution logic).

**Tests landing in this slice** (round 1 finding 6 — not deferred to Slice F):
`manage_reference_data.py`/`manage_companies.py` CLI-level tests replacing the deleted
`test_create_uom_conversion`/`test_create_exchange_rate`, `GET /uom-conversions`/
`GET /exchange-rates` now requiring auth, `GET /companies` returning only the
caller's entitled companies (Decision 12), and **`GET /companies/{id}` returning 404
(not the company's real data) for a company the caller is not entitled to** (round 5's
addition to ADR-009 — the collection endpoint alone doesn't prove the detail endpoint
is filtered too).

Landed as its own slice, after D, because it depends on D's `RequiresPermission`
machinery and permission-code catalog being live, and is a second, independently
reviewable breaking change distinct from D's "auth becomes mandatory" cutover — D
makes existing reads/writes require *a* valid token; E removes/reshapes specific
endpoints regardless of token validity.

### Decision 7 — Slice F: genuinely cross-cutting completeness only

**Narrowed per round-1 finding 6**: every test tied to behavior a specific slice
introduces now lands in that slice (A/B/C/D/E above), not here. Slice F is left with
only what doesn't naturally belong to a single slice: a final, whole-rollout re-run of
README's "Try it" walkthrough end-to-end (distinct from Slice D's own re-verification,
which only proves D's change didn't break it — this run proves D+E *together*, the
actually-shipped state, one last time), a full `make check` green confirmation, and a
cross-check of Action Item 11's list against what actually landed in A–E to confirm
nothing was silently dropped (a checklist review, not new test-writing — if this
check finds a gap, the fix belongs back in the slice that should have carried it, not
patched into F).

## 3. Scope / non-scope

**Scope**: ADR-009's Action Items 1–11 in full, via Slices A–F above.

**Non-scope**: `platform.plugins` (next ADR per §7.2 Q4); any of the items ADR-009's
own Consequences/"Revisit" section explicitly defers (per-company custom roles,
row/field-level auth, a cross-company operator concept, an audit of other global
tables beyond reference data/`Company`); any Phase 2 module beyond `permissions`.

## 4. Validation

Every slice: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy app`,
`uv run lint-imports`, `uv run pytest` all green, plus a real Codex diff review before
commit (per this project's standing workflow — no exceptions for "just a migration" or
"just a CLI script"). Slices D and E additionally require actually running the
updated README walkthrough commands against a live local server (this project's
established "run the real command, don't just re-read the code" doctrine), not just
passing automated tests.

## 5. Risks

- **Slice D is the highest-risk single slice in this project's history to date** — it
  simultaneously changes the trust boundary for every route, deletes existing
  middleware code, and requires every downstream script/test/doc to be migrated in the
  same commit to avoid a broken intermediate `main`. Mitigated by: Slices A–C landing
  everything Slice D depends on beforehand with zero behavior change, so Slice D's own
  diff is "wire it up and flip the switch," not "invent the mechanism and flip the
  switch" in one commit.
- **Permission-code catalog finalization is deferred to Slice D** (Decision 2/5) rather
  than designed upfront in Slice A — a deliberate choice (classifying routes requires
  enumerating them, which is Slice D's job), but means Slice A's migration seed data
  will need a small follow-up seed-data addition inside Slice D rather than being
  complete on first landing. Flagged here so it isn't mistaken for scope creep when it
  happens.
- **Two separate breaking changes (D, E) close together** risk being perceived as one
  large disruptive release rather than two reviewable steps — mitigated by keeping them
  as fully separate slices/PRs/Codex reviews, each independently revertable.
- **This brief's own slice boundaries could turn out wrong once implementation starts**
  (e.g. Slice D turns out to need further splitting) — if so, per this project's
  standing discipline, that's a real scope question to bring back to the user before
  proceeding, not a unilateral re-slice.

## 6. Open decisions — resolved with the user (not Claude's or Codex's to make)

- Slice boundaries (A–F above): confirmed by the user before this brief was drafted.
- Whether to formalize this as a `docs/adr/` brief (mirroring Week 7/8) rather than an
  ad-hoc plan: following established precedent, not re-asked.

## 7. Consensus Revisions

The Status header above records the full round-by-round summary (6 rounds: 6, 7, 2,
2, 1, then 0 findings). Rather than duplicate that narrative here, every individual
fix is annotated in-place throughout Sections 2's Decisions, tagged with which round
and finding number produced it (e.g. "round 2 finding 3", "round 4 findings 1/2") —
searching this document for "round" surfaces the complete fix history at the exact
point each fix landed, which this project's own ADR-009 precedent also does
extensively. The one item that reaches outside this document — the correction to
`ADR-009-platform-permissions.md`'s own Action Item 4 (round 2 finding 5) — is
recorded in both this file's opening section and as a dated "Post-acceptance
correction" note directly in `ADR-009-platform-permissions.md` itself.
