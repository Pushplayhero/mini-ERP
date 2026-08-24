# WEEK9 Slice D — implementation plan (pre-implementation, for Codex plan review)

Status: **ACCEPTED (Codex plan-review round 20 APPROVED, no blocking findings)**.
All 20 rounds of strict adversarial review (`gpt-5.6-sol` / `gpt-5.6-luna`, read-only repo access) concluded with full consensus. 76 total findings across all review rounds verified against actual code and resolved in place. The plan is verified complete, type-safe, secure, and internally consistent with ADR-009 and Decision 5. Ready for implementation.
Author: Claude (architect)
Date: 2026-08-17
Scope owner: Ryan


## 2026-08-24 correction — authenticated session lifecycle

This correction supersedes the earlier plan text that treated route classification
as binary or required `session.manage` for `/auth/switch-company` and
`/auth/logout`. The startup classifier now requires exactly one of `Public()`,
`Authenticated()`, or `RequiresPermission(code)`. The two session-lifecycle routes
use `Authenticated()`; service entry requires `require_current_actor()`, then
requires the ambient session id and verifies actor ownership before entitlement
validation or deletion. Migration 0009 and the `session.manage` catalog/grants are
unchanged. Earlier contradictory requirements remain below as historical review
narrative and are not current implementation requirements.
## 0. Relationship to the brief

`docs/adr/WEEK9-platform-permissions-implementation-brief.md`'s Decision 5 (ACCEPTED,
Codex plan-consensus round 6) specifies **what** Slice D must do and the mechanism's
shape (two-tier public-route classification, service-boundary enforcement, migration
0010, non-HTTP script migration). It explicitly leaves the exact permission-code
catalog, the exact function-by-function service-boundary list, and several mechanism
details as "this slice's own implementation work, not restated here." This document is
that work, done against the actual current repo state (Slices A–C are landed), so it can
be reviewed *before* any code is written — per the scope owner's explicit request this
session, mirroring how the brief itself was plan-reviewed before Slices A–C were coded.

This document does not re-litigate any Decision 5 clause; it only makes concrete
choices Decision 5 deliberately left open, and — where investigation surfaced something
Decision 5's text did not anticipate — flags it explicitly as a new finding needing
sign-off, the same way the brief's own six rounds surfaced things ADR-009 hadn't
anticipated.

**Verified against actual code, not assumed** (all read in full this session):
`app/core/authorization.py`, `app/core/tenancy.py`, `app/core/db.py`,
`app/core/exceptions.py`, `app/main.py`, `app/platform/permissions/{service,router,
context,models}.py`, migration `0009_platform_permissions_initial.py`, every
`app/modules/*/router.py` and `service.py`, all six `app/cli/*.py` scripts
(round 10 finding 4 — corrected from an earlier miscount of five: `bootstrap_admin`,
`demo_o2c`, `rebuild_ar_balances`, `rebuild_stock_summary`, `replay_outbox`,
`seed_demo`),
`pyproject.toml`'s import-linter contracts, `tests/conftest.py`.

---

## 1. Six findings beyond Decision 5's literal text

These are the substantive judgment calls this plan makes. Everything else below is
mechanical application of Decision 5. **These six are the actual review surface.**

### Finding 1 — `ActionDeniedError` has no registered exception handler today

`app/main.py` registers handlers for `NotFoundError`/`TenancyContextError`/
`ConflictError`/`DomainValidationError` only. `RequiresPermission.__call__()` raises
`ActionDeniedError` (a plain `AppError` subclass) directly inside a FastAPI dependency.
With no handler registered, this becomes an **unhandled exception → 500**, not the 403
ADR-009 Decision 7/8 requires. Decision 5 doesn't mention this because Slices A–C never
exercised it through HTTP (Slice B's manual `try/except ActionDeniedError ->
HTTPException(403)` inside the two `/auth/*` handlers is the only place this exception
has ever been caught). **Plan**: register
`@app.exception_handler(ActionDeniedError)` → 403, alongside the existing four. By the
time any `RequiresPermission` dependency runs, `AuthenticationMiddleware` has already
either bound an `ActorContext` or returned 401 — so every `ActionDeniedError` a route
dependency raises is genuinely the "authenticated but insufficient permission" case,
never the "no context bound" case; 403 is correct unconditionally here.

### Finding 2 — service-boundary placement rule (needed to resolve real ambiguity)

Decision 5 says every write/read service function gets `require_permission()` "using
the same permission codes" as router classification, but several service functions are
**shared** between a direct router/CLI entry point and an internal cascade:

- `ledger.service.post_journal_entry` is called both by `create_journal_entry` (the
  router-facing wrapper) **and** by `ledger.posting`'s event-driven posting engine
  (`sales.goods_shipped`/receivables' four posting events) — confirmed via
  `app/modules/ledger/posting.py:75,392`.
- `inventory.service.apply_stock_move`/`get_or_lock_summary` are shared internal
  helpers used by `create_adjustment` (router-facing) **and** by
  `handle_goods_shipped` (an event-bus subscriber with no router at all).
- `app/cli/replay_outbox.py` calls `events.redispatch()`, which invokes
  `inventory_service.handle_goods_shipped` and `ledger_posting.make_posting_handler`
  directly — **no `ActorContext` is ever bound in that script** (confirmed: it only
  binds `company_context`, never `actor_context`). Decision 5 lists this script as
  **"no change"** — that claim is only true if these event-cascade functions do NOT
  independently call `require_permission()`.

**Plan (the rule)**: `require_permission(code)` is added at the **outermost
public service function a router or an actor-bound CLI script calls directly** — i.e.,
every public service function exposed for router/CLI invocation gets a permission check
matching this plan's catalog (§2/§4). Shared internal helpers reused by an event-bus
subscriber or internal cascade (e.g. `_allocate_doc_no`, `apply_stock_move`,
`get_or_lock_summary`, `post_journal_entry` internal core, `handle_goods_shipped`) remain
internal/non-exported and get **no independent check** — their execution is a same-transaction
cascading consequence of an action already authorized at the outer boundary.
Concretely:
- `create_journal_entry` gets the check (`ledger.journal_entry.create`), internal `post_journal_entry` does not.
- `create_adjustment` gets the check (`inventory.adjustment.create`), internal `get_or_lock_summary` / `apply_stock_move` do not.
- `seed_demo.py` is updated to call the public query `inventory_service.list_stock_summary` (checked with `inventory.summary.read`) to read current stock rather than calling the internal mutex `get_or_lock_summary` directly.
- `get_payment_by_external_ref` is a public query function and gets the check (`receivables.payment.read`), while `create_payment`'s 409-conflict recovery calls `load_payment_by_external_ref_unchecked` to avoid double-checking `.read` during a `.create` action.
- `handle_goods_shipped` and the generic posting handler get no check at all (pure event subscribers, never called by a router or CLI).
This keeps `rebuild_ar_balances.py`/`rebuild_stock_summary.py` (raw ORM, no service.py calls at all — confirmed by reading both in full) and `replay_outbox.py` genuinely unchanged, matching Decision 5's own claim.

### Finding 3 — `demo_o2c.py`'s self-contained company creation cannot survive the cutover

`demo_o2c.py` today creates its **own** company ("MAKEDEMO") via `POST /companies` on
every run (`_ensure_company`), fully self-contained against a blank database — no other
script needs to have run first. After Slice D, every write requires an authenticated,
entitled actor, and `AuthenticationMiddleware` binds `company_context(...)` from the
**session's own `active_company_id`** (set at login, changed only via
`/auth/switch-company` against an entitled company) — not from a client-supplied
header anymore (`X-Company-Id` stops being read at all). A freshly logged-in admin has
no entitlement to a company that doesn't exist yet, and there is no HTTP endpoint in
this ADR that grants a session mid-flight to a company created *during* that same
session — `switch_company()` requires a pre-existing `UserCompanyRole`. So
`demo_o2c.py` creating "its own" company and then operating inside it in one run is no
longer achievable without a new grant endpoint, which is out of this brief's scope.

**Plan (Option A, proposed)**: `demo_o2c.py` stops creating its own company entirely.
It requires `bootstrap_admin.py` to have been run once against the target database
first (a real new precondition — but note nothing can write *anything* post-cutover
without a first bootstrap admin regardless, so this isn't a new burden, just the first
place a script needs to say so out loud). `demo_o2c.py`:
- reads `DEMO_ADMIN_EMAIL`/`DEMO_ADMIN_PASSWORD` env vars (falling back to
  `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD` if unset, so a single pair of env
  vars suffices for the common one-admin case),
- calls `POST /auth/login`, gets back `access_token` + `active_company_id`,
- **drops `_ensure_company`/`COMPANY_CODE`/`_headers(company_id)` entirely** — every
  subsequent request uses `Authorization: Bearer <token>` only; the company acted on is
  whatever the login response's `active_company_id` is (i.e., whatever company
  `bootstrap_admin.py` created — `MAIN` by default).
- `_ensure_currency_and_uom` is unaffected (global, not tenant-scoped, but now needs
  the bearer token per §2's catalog (round 11 finding 1 — corrected mislabel;
  §2 holds the catalog, "Finding 2" is the placement rule, a different thing) —
  `masterdata.currency.create`/`masterdata.uom.create` are real codes, not
  `Public()`; only the two `GET`s stay public).

**Concrete Makefile and Quick Start Sequencing (round 15 finding 1, round 16 findings 1 & 2, round 17 finding 1, round 19 finding 1)**:
- `Makefile` adds a dedicated `bootstrap` target and requires both credentials explicitly in environment (ADR-009 Decision 6: credentials are never hardcoded), passing them strictly via `exec -e` to avoid exposing secrets in container process arguments:
  ```makefile
  .PHONY: bootstrap
  bootstrap:
     @test -n "$$BOOTSTRAP_ADMIN_EMAIL" || (echo "ERROR: BOOTSTRAP_ADMIN_EMAIL environment variable must be set." && exit 1)
     @test -n "$$BOOTSTRAP_ADMIN_PASSWORD" || (echo "ERROR: BOOTSTRAP_ADMIN_PASSWORD environment variable must be set." && exit 1)
     docker compose exec -T -e BOOTSTRAP_ADMIN_EMAIL="$$BOOTSTRAP_ADMIN_EMAIL" -e BOOTSTRAP_ADMIN_PASSWORD="$$BOOTSTRAP_ADMIN_PASSWORD" app python -m app.cli.bootstrap_admin

  .PHONY: demo
  demo:
     @test -n "$$BOOTSTRAP_ADMIN_EMAIL" || (echo "ERROR: BOOTSTRAP_ADMIN_EMAIL environment variable must be set." && exit 1)
     @test -n "$$BOOTSTRAP_ADMIN_PASSWORD" || (echo "ERROR: BOOTSTRAP_ADMIN_PASSWORD environment variable must be set." && exit 1)
     DEMO_BASE_URL=http://localhost:$(APP_PORT) DEMO_ADMIN_EMAIL="$$BOOTSTRAP_ADMIN_EMAIL" DEMO_ADMIN_PASSWORD="$$BOOTSTRAP_ADMIN_PASSWORD" uv run python -m app.cli.demo_o2c
  ```
- The Quick Start sequence is strictly ordered:
  1. `make up` (starts Docker containers, runs Alembic migrations, waits for readiness)
  2. `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_password make bootstrap` (provisions initial admin user & `MAIN` company)
  3. `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_password make demo` (executes live-API O2C flow) or `make seed` (seeds masterdata/demo records)
- `bootstrap_admin.py` reads `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` from environment variables, and is idempotent-refusing on second run (exits 1 with clear message if users exist).

### Finding 4 — `DEMO_SEED_ACTOR`'s enumerated permission set, concretely

Following Finding 2's placement rule against `seed_demo.py`'s actual call graph
(re-read in full), `DEMO_SEED_ACTOR` needs exactly:

```
masterdata.currency.create, masterdata.uom.create,
masterdata.company.create, masterdata.company.read,
masterdata.account.create, masterdata.account.read,
masterdata.customer.create, masterdata.customer.read,
masterdata.product.create, masterdata.product.read,
ledger.period.create, ledger.period.read,
inventory.adjustment.create, inventory.summary.read,
sales.order.create, sales.order.read, sales.order.confirm, sales.order.ship,
receivables.invoice.create, receivables.invoice.read,
receivables.payment.create, receivables.payment.read,
receivables.payment.allocate
```
23 codes. (`seed_demo.py` queries stock via `list_stock_summary`, requiring `inventory.summary.read`, and checks existing payments via `get_payment_by_external_ref`, requiring `receivables.payment.read`.)

### Finding 5 — read functions backing a `Public()` route get no service-layer check at all

`masterdata.service.list_uoms`/`list_currencies` back `GET /uom`/`GET /currencies`,
the only two `Public()` routes among ordinary business routes. Since `Public()` means
no permission code exists for that route, there is nothing for a service-layer
`require_permission()` call to check against — these two functions get **no**
`require_permission()` call, consistent with (and required by) their router
classification. (`seed_demo.py`/`demo_o2c.py` both call these; neither needs a
`masterdata.uom.read`/`masterdata.currency.read` code, because no such code exists.)

### Finding 6 — startup route-classification check must fail-closed on unexpected route types

`app.routes` contains `APIRoute` instances (every real business/auth route) plus
plain `starlette.routing.Route` instances FastAPI adds itself for `/openapi.json`,
`/docs`, `/redoc`, `/docs/oauth2-redirect` — these four coincide exactly with the Tier
1 allowlist (confirmed: FastAPI adds them via `add_route`, not `add_api_route`).
**Plan**: the startup check skips exactly the Tier 1 path list, then requires every
*remaining* route to be an `APIRoute` carrying a `Public`/`RequiresPermission` instance
in `route.dependencies` — a route that is neither on the Tier 1 list nor an `APIRoute`
also crashes startup (there are none today, but this keeps the check fail-closed
against a future `Mount`/`WebSocketRoute` rather than silently skipping it).

---

## 2. Proposed permission-code catalog (migration 0010, additive to 0009)

Naming convention: `<module>.<entity>.<action>`, matching the brief's own example
(`sales.order.create`). One code per route *purpose*, shared across list/get where the
underlying service read function is the same (matches `sales.order.read` covering both
`GET /sales-orders` and `GET /sales-orders/{id}`).

| Module | Route(s) | Code | Description | Public()? |
|---|---|---|---|---|
| masterdata | `POST /companies` | `masterdata.company.create` | Create companies | no |
| masterdata | `GET /companies`, `GET /companies/{id}` | `masterdata.company.read` | Read and list companies | no |
| masterdata | `POST /customers` | `masterdata.customer.create` | Create customer accounts | no |
| masterdata | `GET /customers`, `GET /customers/{id}` | `masterdata.customer.read` | Read and list customer accounts | no |
| masterdata | `PATCH /customers/{id}` | `masterdata.customer.update` | Update customer accounts | no |
| masterdata | `DELETE /customers/{id}` | `masterdata.customer.delete` | Delete customer accounts | no |
| masterdata | `POST /products` | `masterdata.product.create` | Create products | no |
| masterdata | `GET /products`, `GET /products/{id}` | `masterdata.product.read` | Read and list products | no |
| masterdata | `PATCH /products/{id}` | `masterdata.product.update` | Update products | no |
| masterdata | `DELETE /products/{id}` | `masterdata.product.delete` | Delete products | no |
| masterdata | `POST /accounts` | `masterdata.account.create` | Create chart of accounts | no |
| masterdata | `GET /accounts`, `GET /accounts/{id}` | `masterdata.account.read` | Read and list chart of accounts | no |
| masterdata | `PATCH /accounts/{id}` | `masterdata.account.update` | Update chart of accounts | no |
| masterdata | `DELETE /accounts/{id}` | `masterdata.account.delete` | Delete chart of accounts | no |
| masterdata | `POST /uom` | `masterdata.uom.create` | Create units of measure | no |
| masterdata | `GET /uom` | — | — | **yes** |
| masterdata | `POST /uom-conversions` | `masterdata.uom_conversion.create` | Create unit of measure conversions | no |
| masterdata | `GET /uom-conversions` | `masterdata.uom_conversion.read` | Read and list unit of measure conversions | no |
| masterdata | `POST /currencies` | `masterdata.currency.create` | Create currencies | no |
| masterdata | `GET /currencies` | — | — | **yes** |
| masterdata | `POST /exchange-rates` | `masterdata.exchange_rate.create` | Create exchange rates | no |
| masterdata | `GET /exchange-rates` | `masterdata.exchange_rate.read` | Read and list exchange rates | no |
| sales | `POST /sales-orders` | `sales.order.create` | Create sales orders | no |
| sales | `GET /sales-orders`, `GET /sales-orders/{id}` | `sales.order.read` | Read and list sales orders | no |
| sales | `PATCH /sales-orders/{id}` | `sales.order.update` | Update draft sales orders | no |
| sales | `POST /sales-orders/{id}/confirm` | `sales.order.confirm` | Confirm sales orders | no |
| sales | `POST /sales-orders/{id}/cancel` | `sales.order.cancel` | Cancel sales orders | no |
| sales | `POST /sales-orders/{id}/ship` | `sales.order.ship` | Ship confirmed sales orders | no |
| ledger | `POST /periods` | `ledger.period.create` | Create fiscal periods | no |
| ledger | `GET /periods` | `ledger.period.read` | Read and list fiscal periods | no |
| ledger | `POST /periods/{id}/close` | `ledger.period.close` | Close fiscal periods | no |
| ledger | `POST /journal-entries` | `ledger.journal_entry.create` | Create journal entries | no |
| ledger | `GET /journal-entries`, `GET /journal-entries/{id}` | `ledger.journal_entry.read` | Read and list journal entries | no |
| ledger | `POST /journal-entries/{id}/reverse` | `ledger.journal_entry.reverse` | Reverse journal entries | no |
| ledger | `GET /reports/trial-balance` | `ledger.report.read` | Read financial reports and trial balance | no |
| inventory | `POST /inventory/adjustments` | `inventory.adjustment.create` | Create inventory stock adjustments | no |
| inventory | `GET /inventory/summary` | `inventory.summary.read` | Read inventory stock summary | no |
| inventory | `GET /inventory/moves` | `inventory.move.read` | Read inventory stock moves | no |
| receivables | `POST /receivables/invoices` | `receivables.invoice.create` | Create and issue invoices | no |
| receivables | `GET /receivables/invoices`, `GET /receivables/invoices/{id}` | `receivables.invoice.read` | Read and list invoices | no |
| receivables | `POST /receivables/invoices/{id}/void` | `receivables.invoice.void` | Void open invoices | no |
| receivables | `POST /receivables/payments` | `receivables.payment.create` | Create customer payments | no |
| receivables | `GET /receivables/payments`, `GET /receivables/payments/{id}` | `receivables.payment.read` | Read and list customer payments | no |
| receivables | `POST /receivables/payments/{id}/void` | `receivables.payment.void` | Void customer payments | no |
| receivables | `POST /receivables/payments/{id}/allocations` | `receivables.payment.allocate` | Allocate payments against invoices | no |
| receivables | `GET /receivables/reports/ar-aging` | `receivables.report.read` | Read accounts receivable aging reports | no |
| platform.permissions | `POST /auth/login` | — | — | **yes** |
| platform.permissions | `POST /auth/switch-company`, `POST /auth/logout` | `session.manage` (0009, unchanged) | Switch active company or log out session | no |
| — | `GET /health` | — | — | **yes** |
| — | `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` | — | — | **yes (Tier 1)** |

**44 new codes**, all granted to `admin` in the same migration (round-2-finding-7
discipline from the brief, applied again here).

**Resolution on query helpers (round 12 finding 2, round 14 finding 5)**: All public query service functions
(such as `inventory.service.list_stock_summary` requiring `inventory.summary.read`, and
`receivables.service.get_payment_by_external_ref` requiring `receivables.payment.read`)
are checked service functions. Internal mutexes or cascade helpers
(`get_or_lock_summary`, `apply_stock_move`, `load_payment_by_external_ref_unchecked`) remain
unexported/internal and carry no `require_permission()` calls.

## 3. Migration 0010

- New file, `down_revision` = `f19b7d4e2c85` (0009's revision id); its own revision id
  auto-generated via `alembic revision -m "platform_permissions_full_catalog"` at
  implementation time, not hand-picked.
- Inserts the 44 `permissions` rows from §2 with both `code` and non-null `description`
  (round 12 finding 3 — satisfies `permissions.description` `nullable=False`), plus 44
  `role_permissions` rows granting every one to `role_code="admin"` — in the same migration,
  not a follow-up.
- Never touches 0009's rows (migration immutability, brief Decision 2/Risk 2).
- `downgrade()` deletes exactly the 44+44 rows it inserted, by code, leaving 0009's
  `session.manage` row and grant untouched.

## 4. `AuthenticationMiddleware` (replaces `tenancy_middleware` in `app/main.py`)

```python
from starlette.routing import Match

_TIER1_PUBLIC_PATHS = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)


def _match_route(request: Request) -> APIRoute | None:
    for route in request.app.routes:
        if isinstance(route, APIRoute):
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return route
    return None


def _route_is_public(route: APIRoute) -> bool:
    return any(isinstance(dep.dependency, Public) for dep in route.dependencies)


@app.middleware("http")
async def authentication_middleware(request, call_next):
    if request.url.path in _TIER1_PUBLIC_PATHS:
        return await call_next(request)

    route = _match_route(request)
    if route is not None and _route_is_public(route):
        return await call_next(request)

    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse({"detail": "Not authenticated."}, status_code=401)
    raw_token = auth_header.removeprefix("Bearer ").strip()

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        try:
            resolved = await permissions_service.resolve_actor(db_session, raw_token)
        except permissions_service.InvalidSessionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        except permissions_service.UserDisabledError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)

    actor_token = set_current_actor(resolved.actor_context)
    session_token = set_current_session_id(resolved.session_id)
    company_token = set_current_company_id(resolved.active_company_id)
    try:
        return await call_next(request)
    finally:
        reset_current_company_id(company_token)
        reset_current_session_id(session_token)
        reset_current_actor(actor_token)
```

Notes:
- A route matching no entry in `app.routes` at all falls through to token resolution
  (never auto-public) — fail-closed per Decision 5's explicit requirement; it then
  either 401s (no/bad token) or proceeds to a real 404 from the router, whichever the
  token check allows.
- Bind order (`actor` → `session` → `company`) and reset order (reverse) mirrors the
  brief's own Decision 3 language for consistency, though no code today reads across
  more than one at unbind time.
- Uses `get_session_factory()` directly (per `app/core/db.py`), not FastAPI's
  `get_db_session` dependency — middleware runs before any route's DI is resolved.
- **Needs one empirical check before trusting it**, matching this project's own
  established discipline for non-trivial framework introspection (Slice C's
  import-linter probes): confirm `route.dependencies[i].dependency` really does yield
  the `Public`/`RequiresPermission` *instance* (not a bound-method wrapper) for a route
  declared with `dependencies=[Depends(RequiresPermission("x"))]`, with a throwaway
  print/test before writing the real logic against the assumption.

## 5. Startup route-classification check

```python
def _classify_routes_or_crash(app: FastAPI) -> None:
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in _TIER1_PUBLIC_PATHS:
            continue
        if not isinstance(route, APIRoute):
            raise RuntimeError(f"Unclassified non-APIRoute at startup: {route!r}")
        deps = [d.dependency for d in route.dependencies]
        markers = [d for d in deps if isinstance(d, (Public, RequiresPermission))]
        if len(markers) != 1:
            # (round 1 finding 4) — "at least one" allows a route mistakenly given
            # BOTH markers to pass startup while the middleware (which only checks
            # for a Public() instance to decide whether to skip token resolution)
            # would treat it as public, leaving RequiresPermission's dependency to
            # run with no ActorContext bound. Exactly one, never "any".
            raise RuntimeError(
                f"Route {route.path} carries {len(markers)} classification marker(s) "
                "(expected exactly 1: Public() xor RequiresPermission(...)) — every "
                "route must be explicitly and unambiguously classified (ADR-009 "
                "Decision 7c)."
            )
```

**Checked**: `pyproject.toml` pins `fastapi>=0.115,<0.116` (locked at `0.115.14`).
`@app.on_event("startup")` still works at this version but has been
deprecated (in favor of `lifespan`) since FastAPI 0.93 — using it here would emit a
`DeprecationWarning` on every app startup, which conflicts with this project's
"CI that looks right still needs a real run" / green-means-green discipline (a
warning that's easy to stop noticing). **Plan: use a `lifespan` async context manager
instead**:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _classify_routes_or_crash(app)
    yield


app = FastAPI(..., lifespan=lifespan)
...
app.include_router(masterdata_router, prefix="/api/v1")
...  # unchanged, still after the FastAPI(...) call, same as today
```

**Correction (round 1 finding 7)**: the earlier draft of this plan claimed router
mounting would need to move *above* the `FastAPI(...)` call. That's wrong and was
never actually necessary — `lifespan`'s body doesn't execute until the ASGI server
genuinely starts serving (Uvicorn's lifespan protocol), which happens only after the
entire module has finished executing top to bottom, including every
`app.include_router(...)` call below the `FastAPI(...)` construction. By the time
`_classify_routes_or_crash(app)` actually runs, `app.routes` is already fully
populated — no reordering needed at all, `lifespan` is just passed in at construction
time as a forward reference to a function whose body runs later.

## 6. Router changes

Every route decorator across all five business routers + `/auth/switch-company` +
`/auth/logout` gets `dependencies=[Depends(RequiresPermission("<code>"))]` per §2's
table; `GET /uom`, `GET /currencies`, `GET /health`, `POST /auth/login` get
`dependencies=[Depends(Public())]`. Imports `Public`/`RequiresPermission` from
`app.core.authorization` (never `app.platform.permissions`, per Slice C's import-linter
contract 7/8). `platform.permissions/router.py`'s `_bearer_token`/`_resolve_and_
authorize`/manual `with actor_context(...), session_context(...):` blocks in
`switch_company_endpoint`/`logout_endpoint` are **deleted** — both handlers become
plain functions calling `service.switch_company(session, payload.company_id)` /
`service.logout(session)` directly, with the `RequiresPermission("session.manage")`
dependency doing the router-level enforcement, while the service layer self-scopes
via `require_current_session_id()`.
**Clarification (round 2 finding 3)**: "become plain functions... calling service
functions directly" removes only the token-resolution/manual-binding boilerplate
(`_bearer_token`/`_resolve_and_authorize`/the manual `with actor_context(...),
session_context(...):` block) — it does **not** remove the existing per-exception
`try/except` → `HTTPException` translation each handler already has today
(`service.CompanyNotEntitledError` → 403, `service.InvalidSessionError` → 401 in
`switch_company_endpoint`; `service.InvalidSessionError` → 401 in `logout_endpoint`).
Those `try/except` blocks stay, unchanged, around the `service.switch_company(...)`/
`service.logout(...)` calls — only the *actor-resolution* code above them is deleted.
Without this, both exceptions would propagate unhandled (no global handler exists for
either) and surface as 500s instead of their documented status codes.

**Self-scoped session management (round 17 finding 4, round 18 finding 3, round 19 finding 3)**:
`current_session_id_or_none()` returns `uuid.UUID | None`, but `switch_company()`/`logout()`
require a real `uuid.UUID`. `app/platform/permissions/context.py` gains
`require_current_session_id() -> uuid.UUID`, mirroring `app.core.tenancy.require_current_company_id()`.
`switch_company` and `logout` at the service layer enforce:
1. First statement: `require_permission("session.manage")` (authoritative service boundary enforcement).
2. Derives session: `session_id = require_current_session_id()`.
3. Loads session row; raises `InvalidSessionError(f"Session {session_id} not found.")` if absent.
4. Ownership assertion helper `_assert_current_actor_owns_session(session_row)`:
   - Evaluates `actor = current_actor_or_none()`.
   - If `actor is None`: fail-closed, raises `InvalidSessionError("No actor context is bound for the current session.")`.
   - Performs strict type narrowing, then checks `if session_row.user_id != actor.user_id: raise InvalidSessionError("The current actor does not own the requested session.")`.
5. Only if all checks pass does `switch_company` verify company entitlement and update `active_company_id`, or `logout` delete the session row and commit.
This satisfies strict mypy type narrowing and structurally guarantees that an actor with `session.manage` can only manipulate their own active session.

## 7. Service-layer changes

### 7.1 Exhaustive Service Function Authorization Matrix (round 15 finding 3, round 16 findings 3 & 4, round 17 findings 2, 3 & 4)

The following matrix explicitly enumerates every service function, unchecked core, internal transaction helper, and platform utility across all modules, detailing its authorization boundary and permitted callers:

| Module | Service Function | Checked Permission Code | Checked Wrapper vs Unchecked Core / Helper | Allowed Callers / Purpose |
|---|---|---|---|---|
| `masterdata` | `create_company` | `masterdata.company.create` | Checked function | Routes (`POST /companies`) |
| `masterdata` | `list_companies` | `masterdata.company.read` | Checked wrapper | Routes (`GET /companies`) |
| `masterdata` | `list_companies_unchecked` | — | Unchecked core | `bootstrap_admin.py: _ensure_company` (Rule 10b) |
| `masterdata` | `get_company` | `masterdata.company.read` | Checked wrapper | Routes (`GET /companies/{id}`) |
| `masterdata` | `load_company_unchecked` | — | Unchecked core | Internal same-transaction reloads |
| `masterdata` | `create_customer` | `masterdata.customer.create` | Checked function | Routes (`POST /customers`) |
| `masterdata` | `list_customers` | `masterdata.customer.read` | Checked function | Routes (`GET /customers`) |
| `masterdata` | `get_customer` | `masterdata.customer.read` | Checked wrapper | Routes (`GET /customers/{id}`) |
| `masterdata` | `load_customer_unchecked` | — | Unchecked core | `update_customer`, `delete_customer` |
| `masterdata` | `update_customer` | `masterdata.customer.update` | Checked function | Routes (`PATCH /customers/{id}`) |
| `masterdata` | `delete_customer` | `masterdata.customer.delete` | Checked function | Routes (`DELETE /customers/{id}`) |
| `masterdata` | `create_product` | `masterdata.product.create` | Checked function | Routes (`POST /products`) |
| `masterdata` | `list_products` | `masterdata.product.read` | Checked function | Routes (`GET /products`) |
| `masterdata` | `get_product` | `masterdata.product.read` | Checked wrapper | Routes (`GET /products/{id}`) |
| `masterdata` | `load_product_unchecked` | — | Unchecked core | `update_product`, `delete_product` |
| `masterdata` | `update_product` | `masterdata.product.update` | Checked function | Routes (`PATCH /products/{id}`) |
| `masterdata` | `delete_product` | `masterdata.product.delete` | Checked function | Routes (`DELETE /products/{id}`) |
| `masterdata` | `create_account` | `masterdata.account.create` | Checked function | Routes (`POST /accounts`) |
| `masterdata` | `list_accounts` | `masterdata.account.read` | Checked function | Routes (`GET /accounts`) |
| `masterdata` | `get_account` | `masterdata.account.read` | Checked wrapper | Routes (`GET /accounts/{id}`) |
| `masterdata` | `load_account_unchecked` | — | Unchecked core | `update_account`, `delete_account` |
| `masterdata` | `update_account` | `masterdata.account.update` | Checked function | Routes (`PATCH /accounts/{id}`) |
| `masterdata` | `delete_account` | `masterdata.account.delete` | Checked function | Routes (`DELETE /accounts/{id}`) |
| `masterdata` | `create_uom` | `masterdata.uom.create` | Checked function | Routes (`POST /uom`) |
| `masterdata` | `list_uoms` | — | Unchecked function | Backs `Public()` route `GET /uom` |
| `masterdata` | `create_uom_conversion` | `masterdata.uom_conversion.create` | Checked function | Routes (`POST /uom-conversions`) |
| `masterdata` | `list_uom_conversions` | `masterdata.uom_conversion.read` | Checked function | Routes (`GET /uom-conversions`) |
| `masterdata` | `create_currency` | `masterdata.currency.create` | Checked function | Routes (`POST /currencies`) |
| `masterdata` | `list_currencies` | — | Unchecked function | Backs `Public()` route `GET /currencies` |
| `masterdata` | `create_exchange_rate` | `masterdata.exchange_rate.create` | Checked function | Routes (`POST /exchange-rates`) |
| `masterdata` | `list_exchange_rates` | `masterdata.exchange_rate.read` | Checked function | Routes (`GET /exchange-rates`) |
| `masterdata` | `_commit_or_conflict` | — | Internal helper | Translates DB `IntegrityError` to 409 `ConflictError` |
| `sales` | `create_order` | `sales.order.create` | Checked function | Routes (`POST /sales-orders`) |
| `sales` | `list_orders` | `sales.order.read` | Checked function | Routes (`GET /sales-orders`) |
| `sales` | `get_order` | `sales.order.read` | Checked wrapper | Routes (`GET /sales-orders/{id}`) |
| `sales` | `load_order_unchecked` | — | Unchecked core | `create_order`, `update_order`, `confirm_order` router reload, `ship_order` router reload, `cancel_order` |
| `sales` | `update_order` | `sales.order.update` | Checked function | Routes (`PATCH /sales-orders/{id}`) |
| `sales` | `confirm_order` | `sales.order.confirm` | Checked function | Routes (`POST /sales-orders/{id}/confirm`) |
| `sales` | `cancel_order` | `sales.order.cancel` | Checked function | Routes (`POST /sales-orders/{id}/cancel`) |
| `sales` | `ship_order` | `sales.order.ship` | Checked function | Routes (`POST /sales-orders/{id}/ship`) |
| `sales` | `_commit_or_conflict` | — | Internal helper | Translates DB `IntegrityError` to 409 `ConflictError` |
| `sales` | `_fetch_customer`, `_fetch_products`, `_build_lines`, `_allocate_order_no` | — | Internal helpers | Private draft validation & numbering utilities |
| `ledger` | `create_period` | `ledger.period.create` | Checked function | Routes (`POST /periods`) |
| `ledger` | `list_periods` | `ledger.period.read` | Checked function | Routes (`GET /periods`) |
| `ledger` | `get_period` | `ledger.period.read` | Checked wrapper | None (internal / test helper) |
| `ledger` | `load_period_unchecked` | — | Unchecked core | `close_period` |
| `ledger` | `close_period` | `ledger.period.close` | Checked function | Routes (`POST /periods/{id}/close`) |
| `ledger` | `create_journal_entry` | `ledger.journal_entry.create` | Checked wrapper | Routes (`POST /journal-entries`) |
| `ledger` | `list_journal_entries` | `ledger.journal_entry.read` | Checked function | Routes (`GET /journal-entries`) |
| `ledger` | `get_journal_entry` | `ledger.journal_entry.read` | Checked wrapper | Routes (`GET /journal-entries/{id}`) |
| `ledger` | `load_journal_entry_unchecked` | — | Unchecked core | `reverse_journal_entry` |
| `ledger` | `reverse_journal_entry` | `ledger.journal_entry.reverse` | Checked function | Routes (`POST /journal-entries/{id}/reverse`) |
| `ledger` | `get_trial_balance` | `ledger.report.read` | Checked function | Routes (`GET /reports/trial-balance`) |
| `ledger` | `post_journal_entry` | — | Unchecked core | `create_journal_entry`, `reverse_journal_entry`, `ledger.posting` event subscribers |
| `ledger` | `_commit_or_conflict` | — | Internal helper | Translates DB `IntegrityError` to 409 `ConflictError` |
| `ledger` | `_validate_lines`, `_ensure_accounts_belong_to_company`, `_reject_control_account_lines`, `_resolve_and_lock_period`, `_allocate_entry_no`, `_build_lines`, `_post_reversal` | — | Internal helpers | Private posting & validation helpers |
| `inventory` | `create_adjustment` | `inventory.adjustment.create` | Checked function | Routes (`POST /inventory/adjustments`) |
| `inventory` | `list_stock_summary` | `inventory.summary.read` | Checked function | Routes (`GET /inventory/summary`), `seed_demo.py` (with `DEMO_SEED_ACTOR`) |
| `inventory` | `list_stock_moves` | `inventory.move.read` | Checked function | Routes (`GET /inventory/moves`) |
| `inventory` | `get_or_lock_summary` | — | Unchecked core mutex | `create_adjustment`, `handle_goods_shipped` |
| `inventory` | `apply_stock_move` | — | Unchecked core helper | `create_adjustment`, `handle_goods_shipped` |
| `inventory` | `handle_goods_shipped` | — | Unchecked subscriber | Event bus dispatch (`sales.goods_shipped`) |
| `inventory` | `_is_source_conflict`, `_commit_or_conflict`, `_ensure_product_belongs_to_company` | — | Internal helpers | Private validation & translation helpers |
| `receivables` | `create_invoice` | `receivables.invoice.create` | Checked function | Routes (`POST /receivables/invoices`) |
| `receivables` | `list_invoices` | `receivables.invoice.read` | Checked function | Routes (`GET /receivables/invoices`) |
| `receivables` | `get_invoice` | `receivables.invoice.read` | Checked wrapper | Routes (`GET /receivables/invoices/{id}`) |
| `receivables` | `load_invoice_unchecked` | — | Unchecked core | `void_invoice`, `issue_invoice` router post-commit reload |
| `receivables` | `void_invoice` | `receivables.invoice.void` | Checked function | Routes (`POST /receivables/invoices/{id}/void`) |
| `receivables` | `create_payment` | `receivables.payment.create` | Checked function | Routes (`POST /receivables/payments`) |
| `receivables` | `list_payments` | `receivables.payment.read` | Checked function | Routes (`GET /receivables/payments`) |
| `receivables` | `get_payment` | `receivables.payment.read` | Checked wrapper | Routes (`GET /receivables/payments/{id}`) |
| `receivables` | `load_payment_unchecked` | — | Unchecked core | `void_payment`, `allocate_payment`, router post-commit reloads |
| `receivables` | `get_payment_by_external_ref` | `receivables.payment.read` | Checked wrapper | `seed_demo.py` (with `DEMO_SEED_ACTOR`) |
| `receivables` | `load_payment_by_external_ref_unchecked` | — | Unchecked core | `receivables.router` 409 conflict recovery |
| `receivables` | `void_payment` | `receivables.payment.void` | Checked function | Routes (`POST /receivables/payments/{id}/void`) |
| `receivables` | `allocate_payment` | `receivables.payment.allocate` | Checked wrapper | Routes (`POST /receivables/payments/{id}/allocations`) |
| `receivables` | `allocate_payment_core` | — | Unchecked core | `create_payment` (inline allocation shortcut) |
| `receivables` | `get_ar_aging` | `receivables.report.read` | Checked function | Routes (`GET /receivables/reports/ar-aging`) |
| `receivables` | `_fetch_order`, `_fetch_customer`, `_fetch_customers`, `_allocate_doc_no`, `_fingerprint_allocations` | — | Internal helpers | Private validation & allocation utilities (router owns commit boundary) |
| `platform.permissions` | `hash_password` | — | Unchecked utility | `login`, `add_user_with_grant` |
| `platform.permissions` | `verify_password` | — | Unchecked utility | `login` (constant-time password verification) |
| `platform.permissions` | `_hash_token` | — | Unchecked helper | `login`, `resolve_actor` (SHA-256 token indexing) |
| `platform.permissions` | `_resolve_permission_codes` | — | Unchecked helper | `resolve_actor` (fresh per-request permission resolution) |
| `platform.permissions` | `login` | — | Unchecked function | Backs `Public()` route `POST /auth/login` |
| `platform.permissions` | `resolve_actor` | — | Unchecked function | `AuthenticationMiddleware` token-to-actor resolution |
| `platform.permissions` | `switch_company` | `session.manage` | Checked self-scoped function | Routes (`POST /auth/switch-company`), reads `require_current_session_id()` |
| `platform.permissions` | `logout` | `session.manage` | Checked self-scoped function | Routes (`POST /auth/logout`), reads `require_current_session_id()` |
| `platform.permissions` | `add_user_with_grant` | — | Unchecked helper | `bootstrap_admin.py` (Rule 10b non-committing user+grant creation) |
| `platform.permissions` | `count_users` | — | Unchecked helper | `bootstrap_admin.py: count_users` (Rule 10b initial user check) |
| `platform.permissions` | `create_user` | **DELETED** | Obsolete | Deleted from codebase in Slice D (replaced by atomic `add_user_with_grant`) |
| `platform.permissions` | `grant_role` | **DELETED** | Obsolete | Deleted from codebase in Slice D (replaced by atomic `add_user_with_grant`) |

### 7.2 Resolution on Query Helpers and Service-to-Service Calls

**Correction (round 1 finding 1; extended by round 2 finding 2 and round 2 finding 1
— confirmed by exhaustively grepping every `await get_*(session...)`/`await list_*
(session...)` call site across all five `service.py` files AND all five
`router.py` files)**: Finding 2's placement rule ("outermost function gets the check")
is necessary but not sufficient in **two** ways, both real:

1. (Round 1 finding 1) Some checked service functions internally call a *different*
   checked function (e.g. `create_order` returning via `get_order`).
2. (Round 2 finding 1) Several **router handlers** — not just service functions —
   call a checked read function a *second time*, after the primary write, purely to
   reload a fresh row for the HTTP response (confirmed necessary: e.g.
   `confirm_sales_order` discards `confirm_order`'s own return value and re-selects
   via `get_order` because `SalesOrder.updated_at`'s server-side `onupdate=func.now()`
   value isn't visible on the in-memory object until a real re-select — this is a
   genuine, deliberate, documented existing pattern, not a bug to remove).
3. (Round 2 finding 1) `bootstrap_admin.py`'s `_ensure_company()` calls
   `masterdata_service.list_companies(session)` **with no `ActorContext` bound at
   all** (Rule 10b exemption — confirmed, no actor binding exists anywhere in that
   script) — `list_companies` gaining `masterdata.company.read` breaks this
   immediately; §8's original "no change" claim for `bootstrap_admin.py` was wrong.

Left as originally drafted, (1)/(2) silently make a single permission insufficient to
perform its own action (e.g. `sales.order.confirm` alone would 403 on `POST
/sales-orders/{id}/confirm`, because the router's own post-commit reload needs
`sales.order.read` too), and (3) breaks the very first command anyone runs against a
fresh database. Confirmed, exhaustively, across both service-to-service AND
router-to-service call sites:

| Checked entry point(s) | Internally/subsequently calls | Would wrongly also require |
|---|---|---|
| `sales.create_order`, `update_order`, `cancel_order` (service→service) | `get_order` | `sales.order.read` |
| `sales/router.py: confirm_sales_order`, `ship_sales_order` (router→service, post-commit reload) | `service.get_order` | `sales.order.read` |
| `masterdata.update_customer`, `delete_customer` (service→service) | `get_customer` | `masterdata.customer.read` |
| `masterdata.update_product`, `delete_product` (service→service) | `get_product` | `masterdata.product.read` |
| `masterdata.update_account`, `delete_account` (service→service) | `get_account` | `masterdata.account.read` |
| `ledger.reverse_journal_entry` (via its `_post_reversal` helper, service→service) | `get_journal_entry` | `ledger.journal_entry.read` |
| `receivables.create_payment` (inline-allocation shortcut, service→service) | `allocate_payment` | `receivables.payment.allocate` |
| `receivables/router.py: issue_invoice`, `void_invoice` (router→service, post-commit reload) | `service.get_invoice` | `receivables.invoice.read` |
| `receivables/router.py: create_payment`, `void_payment`, `allocate_payment` (router→service, post-commit reload) | `service.get_payment` | `receivables.payment.read` |
| `app/cli/bootstrap_admin.py: _ensure_company` (CLI, Rule 10b, no actor bound at all) | `masterdata_service.list_companies` | *anything* — breaks unconditionally |

(`confirm_order`/`ship_order` **service functions** were checked too and do **not**
hit case (1) — both return their own already-locked `order` row directly, never
calling `get_order` internally; it's their *router handlers* that hit case (2)
separately. `inventory.create_adjustment` → `get_or_lock_summary` was already
correctly unchecked per Finding 2's logic (round 11 finding 2 — corrected mislabel;
`get_or_lock_summary` is an internal helper with no code of its own, Finding 2's
placement-rule territory — Finding 5 is specifically about reads backing `Public()`
routes, unrelated here), no conflict there.)

**Fix, generalized (naming corrected from round 1's private-underscore convention,
since case (2)/(3) need cross-module reuse from `router.py`/`app/cli/`, and a
leading-underscore name signals "module-private" — misleading if imported
elsewhere)**: every read function on this table splits into an explicitly-named
unchecked core plus a thin, checked public wrapper. The unchecked core is still
**not** part of the router-facing public API and is never itself given a route or a
permission code — its name says so:

```python
async def load_order_unchecked(session: AsyncSession, order_id: uuid.UUID) -> SalesOrder:
    """Fetch a `SalesOrder` row with NO permission check.

    For same-transaction reload after an already-authorized write (by a caller one
    layer up that already ran its own `require_permission()`), or a Rule
    10a/10b-exempt CLI script — never for a fresh, independently-authorized read.
    """
    ...  # the query body get_order has today, verbatim

async def get_order(session: AsyncSession, order_id: uuid.UUID) -> SalesOrder:
    require_permission("sales.order.read")
    return await load_order_unchecked(session, order_id)
```

Applied uniformly: `load_company_unchecked`/`load_period_unchecked`/`load_order_unchecked`/
`load_customer_unchecked`/`load_product_unchecked`/`load_account_unchecked`/
`load_journal_entry_unchecked`/`load_payment_unchecked`/`load_invoice_unchecked`/
`load_payment_by_external_ref_unchecked`/`list_companies_unchecked` (the last
one specifically for `bootstrap_admin.py`'s case (3) — `masterdata.list_companies`
gets the same checked-wrapper/unchecked-core split as the by-id `get_*` functions,
even though it's a list function, not a get-by-id, because it has the identical
"one route needs it checked, one Rule-10b CLI needs it unchecked" shape). Every
service-to-service internal call (case 1), every router-level post-commit reload
(case 2), and `bootstrap_admin.py`'s call (case 3) are updated to call the
`*_unchecked` variant directly; `receivables.allocate_payment` still splits into
`allocate_payment_core(...)` (unchecked) + a public checked `allocate_payment`
wrapper, same rationale, same naming convention.

**This list was produced by exhaustively grepping this session (service-to-service
AND router-to-service call sites, plus every `app/cli/*.py` script's direct
`masterdata_service`/other service-module calls), not assumed complete by
construction — implementation should re-run the same grep against the actual diff
before considering this slice done**, the same "verify, don't assume" discipline
this project already applies to import-linter contract shapes. Round 2 already
found one category (router-level reloads) and one call site (`bootstrap_admin.py`)
round 1's otherwise-exhaustive service-layer-only grep missed — a real signal that a
third pass focused specifically on `app/cli/*.py` and `router.py` files (not just
`service.py`) is warranted before implementation, not just at review time.

**Correction (round 1 finding 2)**: `platform.permissions.service.switch_company()`
and `logout()` also each gain their own `require_permission("session.manage")` call as
their first statement (importing from `app.core.authorization`, same as every business
module), **in addition to** the router-level `RequiresPermission("session.manage")`
dependency from §6. ADR-009 Decision 7's own text states the service-boundary check
"remains authoritative even for HTTP requests... redundant with the router layer
there, which is the point of defense-in-depth" — this is stated as the general
architecture for every `RequiresPermission`-classified route, not scoped to the five
business modules; the brief's Decision 5 "scoped per business module: masterdata,
sales, ledger, inventory, receivables" sentence is about which modules' *function-by-
function enumeration* is this slice's own undocumented work (`platform.permissions`'s
two routes were already named individually in Decision 3), not a carve-out exempting
`switch_company`/`logout` from the two-layer requirement itself. Originally omitted
from this plan's draft; confirmed as a real gap, not a re-litigation of Decision 5.

## 8. Non-HTTP script changes

- **`app/core/settings.py` (round 12 finding 1, round 13 finding 5)**:
  - `environment: str` field in `Settings` configured with
    `validation_alias=AliasChoices("APP_ENVIRONMENT", "ENVIRONMENT", "environment")`
    (using `pydantic.AliasChoices`) so that `APP_ENVIRONMENT=production` from `.env.example`
    and `docker-compose.yml` is faithfully parsed into `settings.environment` and does not
    fail-open to default `"development"`.
  - **Trust boundary cleanup (round 13 finding 5)**: Delete `tenant_header_name: str = "X-Company-Id"`
    and its obsolete header-trust comment block completely from `Settings` — `AuthenticationMiddleware`
    derives active company solely from verified sessions, so this setting is dead code.
- **`app/cli/seed_demo.py`**:
  - `DEMO_SEED_ACTOR = ActorContext(user_id=uuid4-ish fixed constant, permission_codes=frozenset({...23 codes from §Finding 4...}), is_system_actor=True)`.
  - `with actor_context(DEMO_SEED_ACTOR):` wraps `seed_demo()`'s **entire** body from the
    first line, outermost before entering `company_context`.
  - **Dual stock-query migration & mechanical cleanup (round 13 finding 1, round 14 finding 3)**:
    - Both call sites of `get_or_lock_summary()` (line 368 in `_reconcile_stock` AND line 561 in
      `_assert_invariants`) are migrated to `inventory_service.list_stock_summary(session, product_id=product_id)`.
    - Both call sites explicitly handle the empty sequence:
      `rows = await inventory_service.list_stock_summary(session, product_id=product_id)`
      `current_on_hand = rows[0].on_hand if rows else Decimal("0")`.
    - **Dead code & lock cleanup (round 14 finding 3)**: Remove the redundant `company_id = require_current_company_id()`
      lookup on line 367 and its import; remove obsolete lock-release comments and intermediate lock-release
      commits (`await session.commit()`) on lines 369-373 and 562-564.
  - **Shared programmatic & CLI production guard (round 13 finding 4, round 14 finding 4)**:
    - Guard lives directly inside the reusable `async def seed_demo(*, allow_production: bool = False) -> SeedSummary`
      function (not just `_main()`).
    - At execution time, `seed_demo()` queries `get_settings().environment == "production"`.
    - If in production: checks `allow_production or os.getenv("ALLOW_SEED_DEMO_IN_PRODUCTION", "").lower() in ("1", "true")`.
      If false, raises `RuntimeError("seed_demo: execution refused in production environment without explicit override")`.
    - `_main()` parses CLI `--allow-production` flag and forwards `allow_production=args.allow_production` to `seed_demo()`.
- **`app/cli/rebuild_ar_balances.py`, `rebuild_stock_summary.py`, `replay_outbox.py`**:
  no change (confirmed by direct reading — no `service.py` calls in the first two;
  `replay_outbox.py`'s event dispatch hits only unchecked cascade functions per
  Finding 2).
- **`app/cli/demo_o2c.py`**: per Finding 3 — drop `_ensure_company`/`COMPANY_CODE`/
  `_headers`; add `_login(client) -> tuple[str, str]` (token, active_company_id) via
  `POST /auth/login` using `DEMO_ADMIN_EMAIL`/`DEMO_ADMIN_PASSWORD` (falling back to
  `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD`); every subsequent `client.get/post`
  call's `headers` becomes `{"Authorization": f"Bearer {token}"}`.
  **Addition (round 1 finding 8)**: `login()` requires an explicit `company_id` once
  the admin has more than one entitlement (`AmbiguousCompanySelectionError`) — calling
  `POST /auth/login` with no `company_id` only works for a single-company admin.
  `demo_o2c.py` is explicitly documented (module docstring) as **single-company-admin
  only**: it calls login with no `company_id` and relies on the default bootstrap
  admin having exactly one entitlement (true for `bootstrap_admin.py`'s default flow,
  which grants exactly one company at creation time). It does **not** add a
  `DEMO_COMPANY_ID` selection mechanism — that would only matter for an admin with
  multiple entitlements, which is out of this script's stated purpose (a fresh-DB "wow
  demo," not a multi-tenant walkthrough); the docstring states this constraint
  explicitly so a future multi-company admin doesn't hit a confusing
  `AmbiguousCompanySelectionError` with no explanation.
- **`app/cli/bootstrap_admin.py`**: **correction (round 2 finding 1) — this is NOT
  "no change".** `_ensure_company()` calls `masterdata_service.list_companies(session)`
  directly (confirmed, `bootstrap_admin.py:83`), with no `ActorContext` bound anywhere
  in this script (Rule 10b exemption — genuinely no actor exists yet to bind). Once
  `list_companies` requires `masterdata.company.read` per §2/§7, this call breaks
  unconditionally — the exact "Rule 10b doesn't bypass an unconditional check" bug.
  Fix: `_ensure_company()` calls the new `masterdata_service.list_companies_unchecked
  (session)` (§7) instead of the public checked `list_companies` — consistent with
  every other write this script already does directly against the ORM/private helpers
  rather than through a checked service call (`_ensure_reference_data`'s
  `session.add(Currency(...))`/`session.add(Uom(...))` are raw ORM inserts, never
  routed through `masterdata_service.create_currency`/`create_uom` at all, for the
  same reason — this script deliberately never calls a checked service function).

## 9. README, Makefile, and Deployment Rollout

**Full startup rollout (round 13 finding 3, round 14 finding 2, round 16 findings 1 & 2, round 19 finding 1)**:
1. **Explicit bootstrap command and credentials (round 17 finding 1, round 19 finding 1)**:
   - Both email and password are strictly mandatory with no code defaults (ADR-009 Decision 6):
     `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_secure_password make bootstrap` or
     `docker compose exec -T -e BOOTSTRAP_ADMIN_EMAIL="admin@example.com" -e BOOTSTRAP_ADMIN_PASSWORD="your_secure_password" app python -m app.cli.bootstrap_admin`.
   - Update `.env.example` to document `BOOTSTRAP_ADMIN_EMAIL=` and `BOOTSTRAP_ADMIN_PASSWORD=` (with no default values).
2. **Execution Ordering**:
   - Mandatory sequence: (1) `make up` (spawns containers & runs migrations), (2) `make bootstrap` (provisions initial admin user), (3) `make demo` / `make seed`.
   - `bootstrap_admin` must run before `make seed` and before `make demo`.
3. **Quick Start in `README.md` and `README.zh-TW.md`**:
   - Update Quick Start sections (e.g. `README.md:47`, `README.zh-TW.md:43`) with the exact 3-step sequence:
     1. Start & Migrate: `make up` (starts Docker containers, runs `alembic upgrade head`, waits for health)
     2. Bootstrap Admin: `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_secure_password make bootstrap`
     3. Demo / Seed: `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_secure_password make demo` (or `make seed`)
4. **`Makefile` and Docker Compose execution (round 15 finding 1, round 16 findings 1 & 2, round 17 finding 1, round 18 finding 1, round 19 finding 1)**:
   - `docker-compose.yml` retains existing infrastructure fallback defaults (`POSTGRES_*`, `APP_PORT`, `DATABASE_URL`, `APP_ENVIRONMENT` with `:-`) to ensure `make up` remains functional out-of-the-box.
   - **Administrative credentials** (`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`) have STRICTLY ZERO code defaults and are NOT declared with `:-` fallbacks in `docker-compose.yml`. Instead, they are passed into the container process at execution time strictly via `docker compose exec -T -e ...`.
   - Process arguments (`--password` / `--email`) are deliberately omitted from the `Makefile` command to avoid exposing secrets in `/proc/<pid>/cmdline`; `bootstrap_admin.py` reads them directly from `os.environ`:
     ```makefile
     .PHONY: bootstrap
     bootstrap:
        @test -n "$$BOOTSTRAP_ADMIN_EMAIL" || (echo "ERROR: BOOTSTRAP_ADMIN_EMAIL must be set in the environment." && exit 1)
        @test -n "$$BOOTSTRAP_ADMIN_PASSWORD" || (echo "ERROR: BOOTSTRAP_ADMIN_PASSWORD must be set in the environment." && exit 1)
        docker compose exec -T -e BOOTSTRAP_ADMIN_EMAIL="$$BOOTSTRAP_ADMIN_EMAIL" -e BOOTSTRAP_ADMIN_PASSWORD="$$BOOTSTRAP_ADMIN_PASSWORD" app python -m app.cli.bootstrap_admin

     seed:
        docker compose exec -T app python -m app.cli.seed_demo

     demo:
        @test -n "$$BOOTSTRAP_ADMIN_EMAIL" || (echo "ERROR: BOOTSTRAP_ADMIN_EMAIL must be set in the environment." && exit 1)
        @test -n "$$BOOTSTRAP_ADMIN_PASSWORD" || (echo "ERROR: BOOTSTRAP_ADMIN_PASSWORD must be set in the environment." && exit 1)
        DEMO_BASE_URL=http://localhost:$(APP_PORT) DEMO_ADMIN_EMAIL="$$BOOTSTRAP_ADMIN_EMAIL" DEMO_ADMIN_PASSWORD="$$BOOTSTRAP_ADMIN_PASSWORD" uv run python -m app.cli.demo_o2c
     ```
5. **"Try it" walkthrough in both READMEs (round 12 finding 4)**:
   - Step 1 calls `POST /auth/login` with email/password, capturing `$TOKEN` and noting the
     authenticated session is active in company `MAIN`.
   - Subsequent `curl` commands drop `-H "X-Company-Id:$COMPANY_ID"` and gain
     `-H "Authorization: Bearer $TOKEN"`.
   - Walkthrough uses the already-seeded `TWD`/`EA` and operates in `MAIN`, avoiding conflicting
     attempts to re-POST existing reference data (409) or operating on an unentitled new company (403).
   - Re-verified by actually running the updated commands against a live local server (Decision 5's
     explicit requirement, this project's "run the real command" doctrine) before commit.
6. **Repo-wide trust boundary sweep (round 3 finding 5, round 13 finding 5)**:
   - Run a repo-wide sweep for `X-Company-Id`, `tenant_header_name`, `TenancyMiddleware`, and `tenancy_middleware`.
   - Update architecture/multi-tenancy sections in `README.md` and `README.zh-TW.md`.
   - Clean up obsolete comments and docstrings in `app/main.py`, `app/core/settings.py`, and `app/core/tenancy.py`.
the zh-TW translation is presumably expected to track the English original already.
`app/main.py`'s own module docstring and `app/core/tenancy.py`'s `TenantScopedMixin`-
adjacent prose (already partly addressed by §4's `AuthenticationMiddleware` rewrite
replacing `tenancy_middleware`'s docstring) get the same sweep treatment.

## 10. Tests (mapped to Decision 5's required list, §"Tests landing in this slice")

**§10a — existing-test migration strategy (round 1 finding 3 — this was missing
entirely from the original draft, and is likely the single largest mechanical diff in
this slice, wider than any of the new-test work below).** Once service-layer
`require_permission()` calls land per §7, every one of this project's ~266 existing
tests that calls a now-checked `service.py` function **directly** (bypassing HTTP —
confirmed to be the majority of the ledger/sales/receivables/masterdata/inventory
suites, which test business logic against `service.py` directly, not through
`tests/conftest.py`'s `client` fixture) starts failing closed with
`ActionDeniedError: No actor context is bound`, unless it explicitly binds one first.
This is not new-test-writing, it's a precondition every such existing test needs added
— explicitly Slice D's own scope per Decision 1 ("D's own scope must include fixing
every test... its cutover breaks, not deferring cleanup"), not a hidden Slice F
cost. Concrete plan:
- `tests/conftest.py` gains `TEST_ADMIN_ACTOR = ActorContext(user_id=<fixed constant>,
  permission_codes=<the full set of all 44+1 codes from §2/migration 0009+0010>,
  is_system_actor=True)` — a broad, all-permissions test actor, used **only** by
  tests whose purpose is exercising business logic, not authorization itself
  (ADR-009 Decision 4's "no `SYSTEM_ACTOR`" prohibition is about *production* code
  paths never having an implicit bypass; a named, narrowly-scoped-to-`tests/` actor
  is a different thing, the same way `DEMO_SEED_ACTOR`/`REFERENCE_DATA_ADMIN_ACTOR`
  are already purpose-specific non-production actors).
  **Correction (round 8 finding 2) — `TEST_ADMIN_ACTOR` is a plain module-level
  constant, not a fixture; there is no `bind_test_admin_actor` fixture.** An earlier
  draft of this section introduced a whole-test-wrapping `bind_test_admin_actor`
  pytest fixture (`with actor_context(TEST_ADMIN_ACTOR): yield`, entered once for
  the whole test function), while a later round (6/7) established a *different*,
  narrower rule for mixed HTTP/direct-service tests — the two were never reconciled
  and directly contradicted each other. **Resolved by retiring the fixture
  entirely**: every test needing `TEST_ADMIN_ACTOR` binds it explicitly, inline,
  with `with actor_context(TEST_ADMIN_ACTOR): ...` around exactly the statements
  that need it — one mechanism, used consistently everywhere in this plan, not two.
  For a test that is *purely* direct-service calls with no HTTP request anywhere in
  it, that `with` block may reasonably span the test's whole body (there is no
  leak-check risk to guard against when no HTTP request happens in that test) — but
  it is still the same explicit, local `with actor_context(...):` construct as
  every other case in this document, not a separate opt-in fixture with its own
  semantics. Existing test modules under `tests/{ledger,sales,receivables,
  masterdata,inventory}/` that call `service.py` directly add this `with` block to
  every test function that needs it (mechanical, per-file, but real work — not
  zero-diff).
  **Correction (round 2 finding 4) — this directory list was incomplete**: checked
  services are also called directly from `tests/e2e/test_seed_idempotent.py`,
  `tests/e2e/test_property_o2c_balances.py`, `tests/platform/permissions/
  test_service.py`, and `tests/platform/permissions/test_bootstrap_admin.py` — all
  four need the same treatment, not just the five business-module directories
  originally listed. Re-run a full-repo grep for direct `*_service.create_*`/
  `*_service.update_*`/etc. calls across `tests/**` before considering this slice's
  test migration complete, the same "don't assume the list is exhaustive"
  discipline as §7's own note.
  **Constraint (round 2 finding 4, restated under the single-mechanism rule above)
  — a `with actor_context(...):` scope must never wrap a leak-checked HTTP call**:
  if a test both binds `TEST_ADMIN_ACTOR` around some code *and* makes a real
  request through the `client` fixture while that binding is still open,
  `_assert_no_leaked_context()`'s `current_actor_or_none() is None` check after the
  request would see `TEST_ADMIN_ACTOR` (the still-open outer binding, restored by
  the middleware's `reset_current_actor(token)` call on exit), not `None` — a
  false-positive leak failure, not a real one. Resolution: every `with
  actor_context(TEST_ADMIN_ACTOR):` scope closes *before* any subsequent `client`
  call the same test makes; tests that make real HTTP requests (including
  `tests/platform/permissions/test_router.py`'s leak-check tests) get their actor
  exclusively through a real `/auth/login` + bearer token for that portion, never
  through an outer `actor_context` binding left open across the request.
- `tests/conftest.py`'s `client` fixture wraps `ASGITransport` in `asgi_lifespan.
  LifespanManager(app)` (package already a pinned dependency —
  `pyproject.toml`/`uv.lock` confirmed — just unused until now), so lifespan startup
  actually runs under the test client and §5's route-classification crash is genuinely
  exercised by the test suite, not silently skipped (confirmed: today's
  `ASGITransport(app=app)` alone does **not** trigger FastAPI/Starlette lifespan
  events at all).
- A new `tests/platform/permissions/_helpers.py` function
  `grant_test_role(db_session, user_id, company_id, role_code) -> None` —
  tests that need a real, HTTP-login-capable user entitled to an additional specific company
  (there is no HTTP endpoint that grants this, by design — only `bootstrap_admin.py`
  and this test helper insert `UserCompanyRole` rows directly) insert the row via
  `db_session` directly, matching how `tests/platform/permissions/_helpers.py`'s
  existing `create_user_with_role()` already works. `grant_company_entitlement` is not used.
- Tests that create a company via `masterdata_service.create_company` directly (many
  do, before any login/actor exists) now also need `TEST_ADMIN_ACTOR` bound around that
  call, per §7's new check on `create_company`.

**Correction (round 3 finding 2) — `TEST_ADMIN_ACTOR` is wrong for two specific
cases, both real**:
- `tests/platform/permissions/test_bootstrap_admin.py` tests `bootstrap_admin.py`
  itself — the entire point of those tests is proving Rule 10b's "runs with genuinely
  no actor bound" invariant. Binding `TEST_ADMIN_ACTOR` around them would silently mask
  a real regression (e.g. someone accidentally routing `bootstrap_admin.py` through a
  checked service call — the exact class of bug round 2 finding 1 found for real)
  because the broad `TEST_ADMIN_ACTOR` (round 10 finding 3 — corrected wording, not a
  "fixture," per round 8 finding 2's already-established plain-constant design) would
  satisfy whatever check was wrongly added, hiding it. These tests must run with
  **no** actor bound at all, exactly like the production script they test.
  **Sharpened (round 8 finding 2) — this file needs a split within a single test
  function, not a blanket no-actor rule for the whole file**: confirmed (re-reading
  the actual file), `tests/platform/permissions/test_bootstrap_admin.py:160` calls
  `masterdata_service.list_companies(db_session)` directly, **in the same test
  function** as its `bootstrap_admin(...)` calls, to assert on the company `bootstrap_
  admin()` created — and `list_companies` is now checked (`masterdata.company.read`).
  Every `bootstrap_admin(...)` call in this file stays with **no** actor bound
  (that's the invariant under test); the separate `list_companies(db_session)`
  assertion call, immediately after, gets its own narrow `with actor_context
  (TEST_ADMIN_ACTOR): companies = await masterdata_service.list_companies
  (db_session)` — same "narrow scope around exactly the checked call" mechanism as
  everywhere else in this plan, applied within one test function that genuinely
  needs both states at different lines.
- `tests/platform/permissions/test_router.py`'s own `create_company()` setup helper
  (in `_helpers.py`, used to set up a company before exercising `/auth/login`) calls
  the now-checked `masterdata_service.create_company()` — but round 2's constraint
  correctly forbids leaving any `actor_context` binding open across a leak-checked
  HTTP request `test_router.py`'s tests make. Resolution: the `_helpers.py`
  `create_company()` helper itself wraps **only its own internal service call** in a
  narrow, self-contained `with actor_context(TEST_ADMIN_ACTOR): ...` block that opens
  and closes entirely inside the helper function — never exposed to the calling test,
  never overlapping with any subsequent HTTP request the test makes. Any other
  `tests/**/_helpers.py` setup function that calls a checked service directly (there
  are several, e.g. `tests/ledger/_helpers.py`/`tests/receivables/_helpers.py`'s own
  company/period/account setup helpers) follows the same narrow-scope pattern — the
  same single mechanism this whole section now uses uniformly, not a second one.

**Correction (round 3 finding 1; corrected by round 4 findings 2–5; corrected again
by round 5 findings 1–4 after two prior attempts still didn't fully hold together) —
the largest gap in this whole plan.** §10a above only addressed tests that call
`service.py` **directly**. It said nothing about the much larger set of existing
tests that call the real HTTP `client` fixture using `company_headers(company_id)`
or a hand-rolled `{"X-Company-Id": ...}` dict.

**Scope (round 4 finding 2, extended by round 5 finding 3, extended again by round 7
finding 1 — every prior scope estimate was still an undercount)**: direct references
to `company_headers`/`X-Company-Id` span 25 files (7 using only a literal header
dict, never the helper: `tests/inventory/test_adjustments.py`, `tests/receivables/
{test_aging,test_invoices,test_payments,test_reconciliation_and_isolation}.py`,
`tests/sales/{test_credit_limit_plugin,test_shipping}.py`). Files that only consume
`_helpers.py` functions *transitively* also break: `tests/core/test_events.py`,
`tests/ledger/test_posting.py`, the concurrency test files. **Round 7 finding 1 —
a third, structurally different category, invisible to both prior sweeps**: some
protected requests today carry **no header at all** (Phase 1 has no auth, so an
anonymous call already worked, and there was never a `company_headers`/`X-Company-Id`
reference to grep for in the first place): `tests/masterdata/
test_company_functional_currency.py` makes two anonymous `POST /api/v1/companies`
calls **directly in its test bodies — it has no local `_create_company()` helper of
its own** (corrected, round 10 finding 2 — an earlier draft of this paragraph
miscounted it as a fifth file in the next sentence's helper list; verified by
re-reading the file: no `def _create_company`/`def create_company` anywhere in it);
`tests/masterdata/test_reference_data_api.py` makes anonymous
`POST /api/v1/uom-conversions`/`POST /api/v1/exchange-rates` calls (only its two
`GET /uom`/`GET /currencies` calls should stay anonymous — those two routes alone
are `Public()`). **`tests/masterdata/` separately has exactly four files with their
own *local* `_create_company()` helper function** (not five — `test_company_
functional_currency.py` does not have one, per the correction above):
`test_conflict_handling.py`, `test_cross_company_isolation.py`,
`test_customers_api.py`, `test_products_and_accounts_api.py` — never covered by the
shared `create_company`/`CompanyAuth` design below, which so far only named
`tests/{ledger,sales,receivables,inventory}/_helpers.py`. All five
`tests/masterdata/` files (the four with a local helper, plus `test_company_
functional_currency.py`'s direct inline calls) need migrating to the shared design;
the fifth just has no helper function to replace — its two direct `POST /companies`
call sites are authenticated with `anchor_admin.headers` (or a freshly-created
`CompanyAuth`'s headers, matching whichever the test's own scenario calls for) in
place.

**Corrected sweep methodology (round 8 finding 1 — the prior version of this
methodology itself had a gap)**: an import-graph sweep for `_helpers.py`/
`company_headers` consumers (as established) is necessary but not sufficient, since
it cannot find calls that never had a header/import to trace in the first place.
The route-aware full-repo sweep must cover **every** `AsyncClient` call form, not
just the four convenience methods — **`client.request(method, path, ...)` is a real,
separate gap**, confirmed: `tests/masterdata/test_cross_company_isolation.py`'s
`test_all_tenant_endpoints_reject_missing_company_context` and `tests/ledger/
test_cross_company_isolation.py`'s `test_ledger_endpoints_reject_missing_company_
context` both use `client.request(method, path)` in a loop over several
`(method, path)` pairs, entirely missed by a sweep that only looks for `.post`/
`.patch`/`.delete`/`.get`. **Sweep target, corrected**: every `client.<verb>(...)`
call **and** every `client.request(<verb>, ...)` call, across all of `tests/**`,
cross-referenced against §2's permission-code table — any call hitting a route
classified `RequiresPermission(...)` (i.e., every route except the Tier-1 static
paths, `GET /uom`, `GET /currencies`, `GET /health`, `POST /auth/login`) that does
not already carry `headers=<something>.headers`/a real `Authorization` header needs
migrating, regardless of which client method it uses or whether it happens to
reference `company_headers`, `X-Company-Id`, or any shared helper at all. This
plan's own four successive scope-widening rounds (25 → 31 → "route-aware sweep, no
fixed number" → "+ `.request()` and any other client alias") is itself the evidence
that no single sweep pattern can be trusted as exhaustive here — the real scope is
whatever the sweep finds at implementation time, not any count or method list stated
in this document. **These two specific tests are explicitly migrated**, not left to
the general sweep alone: both are the "missing/bogus header → 403" sweep tests
already flagged (§10, `AuthenticationMiddleware` test module discussion) as
re-expressing to "missing/bogus bearer token → 401" and potentially folding into
that new test — this is the same fix, now correctly located via the `.request()`-
aware sweep instead of missed by it.

**One complete, composable helper API (round 5 findings 1/2, corrected again by
round 6 findings 1/2/4 — fixture discoverability, an omitted composite helper, and
a lint failure)**. `CompanyAuth`/`_login_fresh_admin`/`create_company` live in a
plain (non-fixture) module `tests/_auth_helpers.py`, imported by every one of
`tests/{ledger,sales,receivables,inventory}/_helpers.py`, replacing their separate
ad hoc `create_company` definitions/re-exports with one shared implementation.
**Fix (round 6 finding 1) — `anchor_admin` itself must live in `tests/conftest.py`,
not `tests/_auth_helpers.py`**: pytest only auto-discovers fixtures defined in (or
explicitly re-exported/`pytest_plugins`-registered from) a `conftest.py` — merely
importing the module that defines a `@pytest_asyncio.fixture`-decorated function
from elsewhere does **not** register it, unlike plain helper functions (which import
normally). `tests/_auth_helpers.py` therefore exports only plain functions/the
dataclass; `anchor_admin` is defined directly in `tests/conftest.py` (alongside the
existing `client`/`db_session` fixtures it depends on), calling into
`tests/_auth_helpers.py`'s plain `_login_fresh_admin`:

```python
# tests/_auth_helpers.py — plain functions/dataclass only, no fixtures

@dataclass(frozen=True)
class CompanyAuth:
    """A company plus a bearer token for a fresh admin entitled to it — the
    HTTP-test equivalent of `company_headers(company_id)`, carrying a real
    identity instead of a trusted header."""
    company_id: uuid.UUID
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _login_fresh_admin(
    client: AsyncClient, db_session: AsyncSession, *, company_id: uuid.UUID
) -> str:
    """Create a fresh user (random email, so parallel/rerun tests never collide),
    grant it `admin` for `company_id` (single call — `create_user_with_role()`
    already performs the grant; nothing else grants anything here, round 4
    finding 4's fix), log in for real, return the bearer token."""
    email = f"test-{uuid.uuid4()}@example.test"
    # Fix (round 6 finding 4): `create_user_with_role()`'s return value is unused
    # here — assigning it to `user_id` for no reason trips ruff's F841 and fails
    # this project's `ruff check` validation gate. Call without assignment.
    await create_user_with_role(
        db_session, email=email, password="not-a-real-password",
        company_id=company_id, role_code="admin",
    )
    resp = await client.post(
        "/auth/login",
        json={"email": email, "password": "not-a-real-password", "company_id": str(company_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def create_company(
    client: AsyncClient, db_session: AsyncSession, code: str, *, anchor: CompanyAuth
) -> CompanyAuth:
    """Create a NEW company for real over `POST /companies` (using `anchor`'s
    token — `masterdata.company.create` isn't company-scoped, `Company` isn't
    `TenantScopedMixin`, so the anchor's own entitlement is irrelevant to whether
    they can create a *different* company — preserving real HTTP coverage of this
    route, round 4 finding 3's explicit requirement), then logs a FRESH admin in
    for the new company (creating it does not itself grant the creator any
    entitlement — no such auto-grant exists), returning that new company's own
    `CompanyAuth`, not the anchor's."""
    resp = await client.post(
        "/api/v1/companies",
        json={"code": code, "name": f"{code} Inc.", "functional_currency_code": "TWD"},
        headers=anchor.headers,
    )
    assert resp.status_code == 201, resp.text
    company_id = uuid.UUID(resp.json()["id"])
    token = await _login_fresh_admin(client, db_session, company_id=company_id)
    return CompanyAuth(company_id=company_id, token=token)
```

```python
# tests/conftest.py — the fixture itself, where pytest can actually find it

@pytest_asyncio.fixture
async def anchor_admin(client: AsyncClient, db_session: AsyncSession) -> CompanyAuth:
    """Bootstraps one company via raw ORM (sidesteps the chicken-and-egg problem —
    no company exists yet for anyone to be entitled to, mirroring how
    `bootstrap_admin.py` solves the identical problem in production), then logs a
    fresh admin in for it — takes BOTH `client` (for the real login call) and
    `db_session` (for the raw company insert + user/grant), closing over both from
    the start."""
    company = Company(code=f"ANCHOR-{uuid.uuid4().hex[:8]}", name="Anchor Co.",
                       functional_currency_code="TWD")
    db_session.add(company)
    await db_session.flush()
    await db_session.commit()
    token = await _login_fresh_admin(client, db_session, company_id=company.id)
    return CompanyAuth(company_id=company.id, token=token)
```

**Threaded through every downstream helper (round 5 finding 2, extended by round 6
finding 2, extended again by round 7 finding 1 — a fifth directory was missing)**:
`create_account`/`create_period` (`tests/ledger/_helpers.py`), `create_customer`/
`create_product`/order-lifecycle helpers (`tests/sales/_helpers.py`),
`create_adjustment` (`tests/inventory/_helpers.py`), every simple receivables setup
helper, **and `tests/masterdata/`'s four files' own local `_create_company()`-style
helpers (round 10 finding 2 — corrected from an earlier miscount of five;
`test_company_functional_currency.py` has no such helper, only direct inline calls,
per the correction above)** (replaced with calls into the shared
`tests/_auth_helpers.py` `create_company`, same as the other four directories — not
kept as separate local implementations) all change their signature from accepting a
bare
`company_id: uuid.UUID` (then calling `headers=company_headers(company_id)`, or, for
`test_reference_data_api.py`'s anonymous protected writes, no header at all) to
accepting an `auth: CompanyAuth` (then calling `headers=auth.headers`) — a
mechanical, uniform rename applied to every helper function's parameter list and
every one of its call sites across all five directories, not four.
`test_reference_data_api.py`'s two `GET /uom`/`GET /currencies` calls are the one
exception that stays genuinely anonymous — those two routes alone are `Public()`.

**Fix (round 6 finding 2) — `tests/receivables/_helpers.py`'s `setup_shipped_order`/
`ShippedOrderContext` needed explicit handling, not just "every simple helper"**:
`setup_shipped_order(client, code, ...)` is a **composite** helper — it internally
calls `create_company` then `create_period`/`create_account`/`create_customer`/
`create_product`/`create_adjustment`/`create_draft_order`/`confirm_order`/
`ship_order` in sequence, and `ShippedOrderContext` (its return type) currently
carries a bare `company_id`, which dozens of call sites across `tests/receivables/`
extract directly. Fix: `setup_shipped_order` gains required `db_session: AsyncSession`
and `anchor: CompanyAuth` parameters (same shape as `create_company`'s own new
signature), calls the new `create_company(client, db_session, code, anchor=anchor)`
internally to get a `CompanyAuth`, threads that `auth` through every one of its own
internal calls instead of a bare `company_id`, and `ShippedOrderContext` gains an
`auth: CompanyAuth` field (replacing the bare `company_id: uuid.UUID` field — the
dataclass's other fields are unchanged). The "dozens of callers" that access
`ctx.company_id` today become `ctx.auth.company_id` (still available, just one
attribute deeper) or `ctx.auth.headers` (replacing whatever `company_headers
(ctx.company_id)` call they were making) — a real, mechanical per-call-site rename,
not a design gap, but one requiring its own explicit pass through
`tests/receivables/**` specifically because this one helper is a multi-step
composite, not a simple single-request wrapper like the others.

- **Multi-company/cross-company-isolation tests**: re-expressed as two separately
  created-and-logged-in `CompanyAuth`s — a strictly more realistic
  cross-tenant-isolation test than the old "same anonymous caller, two different
  headers" model, since it now also proves entitlement isolation, not just
  header-driven data filtering.
- **Fix (round 4 finding 5, unchanged, still correct) — three tests cannot be
  mechanically converted at all**: `tests/masterdata/test_conflict_handling.py`'s
  `test_create_{customer,product,account}_with_nonexistent_company_id_is_409` POST
  against a syntactically valid but nonexistent `company_id` — impossible to express
  via `create_company`-style entitled setup (the entitlement grant itself would fail
  its own FK first) and structurally HTTP-unreachable post-cutover regardless. Moved
  to service-level tests with `TEST_ADMIN_ACTOR` + a manually-bound bogus
  `company_context(uuid.uuid4())`, documented as such.
- **Fix (round 5 finding 4, corrected again by round 6 finding 3 after naming only
  three files understated the real scope) — the "HTTP tests vs. direct-service tests
  never combine" separation this plan asserted since round 1 is false.** Confirmed
  real in at least `tests/ledger/test_posting.py`, `test_period_close_concurrency.py`,
  `tests/sales/{test_confirm_concurrency,test_credit_limit_plugin,test_shipping}.py`,
  `tests/receivables/{test_payments,test_invoices}.py` — all mix authenticated HTTP
  setup (creating a company/order/etc. via `client`) with **direct** checked-service
  calls in the *same* test function, some inside `asyncio.gather`-spawned concurrent
  tasks. **This list is not claimed exhaustive either** (round 6 finding 3's own
  point: naming specific files kept undercounting across two rounds) — the actual
  rule to apply, everywhere, at implementation time (**corrected by round 7 finding
  2 — round 6's version of this rule was itself overbroad**): round 6's wording,
  "any test function that calls a `service.py` function directly needs an actor
  binding," is wrong as stated — it would wrongly demand binding an actor around
  calls to functions this plan itself defines as deliberately **unchecked**
  (`platform.permissions.service.login`/`resolve_actor` — **but not `switch_company`/
  `logout`, corrected round 10 finding 1: §7 explicitly adds
  `require_permission("session.manage")` to both, so an earlier draft's "every other
  `platform.permissions.service` function" overclaimed and directly contradicted
  §7's own text**), `list_uoms`/`list_currencies`, every `load_X_unchecked` core
  from §7, the event-cascade functions `post_journal_entry`/`apply_stock_move`/
  `handle_goods_shipped`/the generic posting handler), and it would actively break
  `test_bootstrap_admin.py` (§10a, round 5 finding 2's fix — those tests must run
  with **no** actor bound, proving Rule 10b's invariant) and any deliberate
  negative-authorization test (one that calls a checked function with **no** actor,
  or an actor missing the needed code, specifically to assert it raises
  `ActionDeniedError`/gets a 403 — binding `TEST_ADMIN_ACTOR` there would defeat the
  test's entire point, the same class of mistake round 5 finding 2 already caught
  once for `test_bootstrap_admin.py` specifically). **Corrected rule**: bind
  `TEST_ADMIN_ACTOR` narrowly around a direct service call only when (a) that
  specific function is one §7 actually adds `require_permission()` to (the
  finalized checked-function set from §2/§7's table — not "every `service.py`
  function," which includes plenty that stay deliberately unchecked), **and** (b)
  the test is not itself a negative-authorization test whose point is exercising the
  no-actor/insufficient-permission path. The implementation-time sweep is scoped to
  "every direct call to a function in the checked-function set, in a test that isn't
  itself testing that function's own authorization boundary" — not a blanket rule
  over every `service.py` call. **Concrete instance (round 10 finding 1)**:
  `tests/platform/permissions/test_service.py` (already named in §10a's round-2
  finding-4 directory list) calls `service.switch_company(...)`/`service.logout
  (...)` directly, e.g. `test_switch_company_re_checks_entitlement`/
  `test_logout_deletes_session_row` — both now-checked per §7's fix above, so these
  calls need the same narrow `with actor_context(TEST_ADMIN_ACTOR):` treatment as
  any other checked function, unless the specific test is itself asserting the
  denial path (a `session.manage`-lacking actor, or none bound), in which case it
  keeps that narrower/absent binding on purpose. Applied where it does hold: bind
  `TEST_ADMIN_ACTOR`
  **narrowly around just the direct-service call(s)** (`with actor_context
  (TEST_ADMIN_ACTOR): await some_service.some_write(...)`), never as a whole-test
  fixture — this was always the actual intent of "narrow scope" in round 3/4's
  language, but this plan's own §10a summary oversimplified it into a clean category
  split that doesn't match how these files are actually structured.
  **Concurrency-specific rule**: `asyncio.create_task()`/`asyncio.gather()` copies
  the *parent* task's `contextvars.Context` at the moment of spawning — so binding
  `actor_context(...)` in the test function **before** spawning is visible inside
  every spawned task (correct when every concurrent task should share one actor);
  binding it **inside** each spawned task's own body instead gives each task an
  independent, isolated binding (correct when the test's whole point is simulating
  multiple independent concurrent callers) — never assume a `with actor_context(...)`
  entered after tasks are already running affects tasks already spawned, since a
  `.set()` inside one task never propagates to sibling tasks or the parent.
- **Fix (round 5 finding 5) — the startup-crash test's fixture ordering was wrong.**
  By the time the shared `client` fixture's `LifespanManager(app)` context is
  entered, `_classify_routes_or_crash(app)` has already run once (at that
  `LifespanManager.__aenter__`) — mutating `app.routes` inside the test body
  afterward is too late, and mutating the shared module-level `app` singleton at all
  is itself risky across tests (leaks into every other test sharing the same `app`
  object for the rest of the session). **Fix**: this one test does **not** use the
  `client`/`LifespanManager` fixture at all. It calls `_classify_routes_or_crash`
  directly and synchronously, twice: once against the real, already-fully-mounted
  `app` (asserting no raise — proves every real production route is classified),
  and once against a small throwaway `FastAPI()` instance constructed inline in the
  test with one deliberately unclassified route added (asserting it raises) — no
  ASGI/lifespan machinery involved either way, sidestepping the ordering problem
  entirely rather than working around it.
- Real scope: **at least 31 files** (round 5 finding 3), likely more once every
  transitive `_helpers.py` consumer is enumerated by import graph at implementation
  time — the single largest piece of mechanical work in this slice, by a wide
  margin over every other change this plan describes.

- `tests/test_smoke.py`: new `AuthenticationMiddleware` test module (or extend
  in-place) covering valid/expired/missing/malformed token, disabled-user → 403 not
  401, Tier-1 allowlist bypass proven for exactly those four paths and nothing else.
  **Startup-crash coverage (corrected round 9 finding 2 — this bullet previously
  described a stale, superseded approach)**: per §10's standalone design (round 5
  finding 5), this is **not** "add a throwaway route to the shared app inside the
  test, then remove it" — that mutates the module-level `app` singleton mid-test and
  is exactly the ordering/leakage problem round 5 finding 5 already fixed. It is the
  two direct, synchronous calls to `_classify_routes_or_crash` specified in §10:
  once against the real `app` (no raise expected), once against a throwaway inline
  `FastAPI()` instance carrying one deliberately unclassified route (raise
  expected) — no shared-app mutation, no `client`/`LifespanManager` involved. The
  `tests/masterdata/test_cross_company_isolation.py` sweep-pattern comparison stays
  useful only for the separate "missing/bogus token → 401" sweep test (§10a, round 8
  finding 1's fix), not for this startup-crash test.
- **New (round 1 finding 6 — was missing)**: an explicit HTTP-level test that a
  genuinely authenticated user lacking a route's required permission reaches the new
  `@app.exception_handler(ActionDeniedError)` (§Finding 1) and gets a real 403 response
  body — not just a unit test of `require_permission()` raising in isolation
  (`tests/core/test_authorization.py`'s existing-style tests prove the exception is
  raised; this one proves FastAPI's registered handler actually catches it and maps it,
  which is the part Finding 1 found missing today).
- `tests/platform/permissions/` test migration (round 18 finding 2, round 19 finding 2):
  With `create_user` and `grant_role` deleted from `platform.permissions.service`, `tests/platform/permissions/_helpers.py`
  provides three and only three canonical test helpers:
  1. `create_user_with_role(session, email, password, company_id, role_code)`:
     uses `permissions_service.add_user_with_grant(...)` followed by `await session.commit()`.
  2. `create_test_user(session, email, password) -> uuid.UUID`:
     creates ORM `User` directly, strictly requiring `password_hash=permissions_service.hash_password(password)`,
     followed by `await session.commit()` and `await session.refresh(user)`. Creates an unentitled test user (e.g. `test_service.py:95`).
  3. `grant_test_role(session, user_id, company_id, role_code) -> None`:
     creates ORM `UserCompanyRole` directly followed by `await session.commit()`, used for granting additional company roles to existing users.
  `grant_company_entitlement` is completely obsolete and does not exist.
- `tests/platform/permissions/test_service.py` (round 17 finding 4, round 18 finding 3, round 19 finding 3):
  1. Updated direct service calls for `switch_company(db_session, company_id)` and `logout(db_session)`: after `result = await login(...)`,
     resolves actor via `actor = await service.resolve_actor(db_session, result.raw_token)`, and wraps
     direct service calls in `with actor_context(actor.actor_context), session_context(result.session_id):`
     so that the caller's bound actor ID and session ID match identically.
  2. Added explicit adversarial cross-user session test: logs in User A (Session A) and User B (Session B);
     binds Actor A's context together with Session B's ID in `session_context`; verifies that calling
     `service.switch_company(db_session, company_id)` or `service.logout(db_session)` raises `InvalidSessionError`,
     preventing cross-user session mutation at the authoritative service layer.
- `tests/platform/permissions/test_router.py`: updated for the deleted manual
  resolution (§6) — same `_assert_no_leaked_context()` pattern, now asserting no leak
  after a request through real middleware, not the old manual `with` block.
  **Extended (round 3 finding 3)**: the existing `_assert_no_leaked_context()` only
  checks `current_actor_or_none()`/`current_session_id_or_none()` — `Authentication
  Middleware` also binds `company_context(...)` (§4), a third `ContextVar` the old
  helper (written before that binding existed) never checked. Extended to also assert
  `current_company_id_or_none() is None` after both a successful request and a
  request that raises, matching the other two checks' existing success/exception-path
  coverage exactly. Also covers
  the new service-layer `require_permission("session.manage")` calls inside
  `switch_company()`/`logout()` (§7 finding 2) actually firing (not just the router
  dependency).
- New: wrong-company entitlement → 403, cross-company entitlement isolation
  (extends `test_cross_company_isolation.py`'s existing scenarios with real login
  instead of a raw header).
- `tests/core/test_authorization.py`: add the case Decision 5 flags explicitly — a
  protected service function called with no `ActorContext` bound **and** with
  insufficient permission, both failing (ADR-009 round-4 gap).
- `tests/e2e/test_demo_o2c_smoke.py`: in-process ASGI smoke test updated for the new login-first flow (§Finding 3).
- `tests/e2e/test_demo_o2c_live.py` (round 12 finding 5, round 13 finding 2, round 14 finding 1): dedicated subprocess-based
  live server integration test:
  1. Bootstrap prerequisite: executes `bootstrap_admin` with known test credentials (`admin@example.com` / `adminpassword`)
     and commits to the database, ensuring admin user, single entitlement, and `MAIN` company exist before server boot.
  2. Ephemeral port allocation: discovers a free port dynamically via `socket.socket(socket.AF_INET, socket.SOCK_STREAM)`
     bound to `("127.0.0.1", 0)` and queries `getsockname()[1]`.
  3. Spawns `uvicorn app.main:app --host 127.0.0.1 --port <port>` in a dedicated OS subprocess
     (`subprocess.Popen`), avoiding thread-event-loop conflicts with asyncpg.
  4. Polls `http://127.0.0.1:<port>/health` via `httpx.get` until `200 OK` (readiness gate, up to 5s timeout).
  5. Runs `app.cli.demo_o2c` in a separate child process (`subprocess.run`) passing
     `DEMO_BASE_URL=http://127.0.0.1:<port>`, `BOOTSTRAP_ADMIN_EMAIL="admin@example.com"`, and
     `BOOTSTRAP_ADMIN_PASSWORD="adminpassword"` in `os.environ`, preventing import-time freezing of `BASE_URL`.
  6. Asserts process exit code 0, verifies CLI summary output, and checks DB assertions.
  7. Teardown: robust `try/finally` with `proc.terminate()`; if still alive after `proc.wait(timeout=5)`,
     enforces `proc.kill()` fallback.
- `tests/cli/test_seed_demo.py` (round 12 finding 1, round 13 finding 4, round 14 finding 4): complete suite of production
  guard integration tests (covering both programmatic `seed_demo()` direct calls and CLI subprocess invocations):
  1. `APP_ENVIRONMENT=production` with no overrides -> raises `RuntimeError` / exits non-zero.
  2. `APP_ENVIRONMENT=production` with `--allow-production` CLI flag -> succeeds.
  3. `APP_ENVIRONMENT=production` with `ALLOW_SEED_DEMO_IN_PRODUCTION=1` (and `"true"`) -> succeeds.
  4. `APP_ENVIRONMENT=production` with invalid `ALLOW_SEED_DEMO_IN_PRODUCTION=0` / `"false"` -> raises / exits non-zero.
  5. All tests use a pytest fixture with `get_settings.cache_clear()` in guaranteed `try/finally` teardown.
- `tests/receivables/test_router.py` (round 14 finding 5): regression test proving an actor with only
  `receivables.payment.create` (and not `receivables.payment.read`) submitting a duplicate payment receives the
  intended 409 conflict with existing payment details (via `load_payment_by_external_ref_unchecked`),
  without failing with 403 on `.read`.
- `tests/masterdata/test_reference_data_api.py`'s `GET /uom`/`GET /currencies` tests
  and `tests/test_smoke.py`'s `/health` test stay unauthenticated (ADR-009's own
  round-2 correction, unchanged).

## 11. Validation

Standard per-slice checklist (brief §4): `ruff check`, `ruff format --check`, `mypy
app`, `lint-imports`, full `pytest`, plus **actually running** the updated README
walkthrough against a live local server before commit. Given Finding 3/Finding 1 are
real gaps beyond Decision 5's text, this plan itself should go through Codex plan
review before any of the above is written — that review is what this document is for.

---

## 12. Explicit questions for Codex / scope-owner sign-off

1. Finding 3 (demo_o2c's company-creation removal, Option A) — agree, or is there a
   better resolution this plan missed? **(Round 1 agreed with Option A; added a
   single-company-admin constraint, §8.)**
2. Finding 2's placement rule (event-cascade functions get no independent
   `require_permission()`) — agree this is the correct reading of "two real layers,"
   not a loophole? **(Round 1 confirmed the rule itself was sound; round 1 finding 1
   found it was incompletely applied — six checked functions had an unchecked internal
   call to a *different* checked function. Fixed in §7 via a checked-wrapper/unchecked-
   core split. Re-confirm the fixed version.)**
3. §2's catalog — any naming/grouping objections before it's baked into an
   append-only migration? **(Round 1 raised no objection to the catalog itself.)**
4. Finding 6 (fail-closed crash on unexpected non-`APIRoute` types) — worth the extra
   branch, or overengineered for an app with no `Mount`s today? **(Round 1 raised no
   objection.)**
5. §5's `lifespan` wiring — **resolved, round 1 finding 7**: no reordering is needed;
   `lifespan` is passed to `FastAPI(...)` as a forward reference and only executes
   after the whole module (including every `include_router` call) has already run.
   Corrected in §5.
6. **New from round 1, updated round 9 (finding 1) to match §10a's corrected design**:
   §10a's `TEST_ADMIN_ACTOR` (a broad, all-permissions plain constant, bound via
   explicit local `with actor_context(TEST_ADMIN_ACTOR): ...` blocks at each call
   site that needs it — not a fixture, per round 8 finding 2's fix) for tests
   exercising business logic, not authorization — does this sit comfortably alongside
   ADR-009 Decision 4's "no `SYSTEM_ACTOR`" prohibition, given that prohibition was
   stated for production code paths? Confirm the distinction drawn in §10a is real,
   not a rationalization.

---

## 13. Round 1 Consensus Revisions

Codex (`gpt-5.6-sol`, read-only sandbox against this actual repo) returned **REJECTED**
with 9 findings. All 9 were independently verified against the actual code before
being accepted as real (grepping every affected `service.py`, reading the exact
functions named, checking the pinned `asgi-lifespan` dependency, re-reading
`seed_demo.py`'s real call order) — **no false positives this round; all 9 confirmed
and fixed in place above**:

1. Service-boundary placement rule incompletely applied (six checked functions had an
   unchecked internal call to a different checked function) — fixed via a
   checked-wrapper/unchecked-core split, §7.
2. `switch_company()`/`logout()` missing their own service-boundary
   `require_permission("session.manage")` call — added, §7.
3. Existing-test migration strategy was entirely missing (LifespanManager, a broad
   test-fixture actor, an entitlement-granting test helper) — added as new §10a.
4. Startup classifier accepted "at least one" marker instead of "exactly one" — fixed,
   §5.
5. `current_session_id_or_none()` (nullable) passed where `switch_company()`/`logout()`
   need a real `uuid.UUID` — fixed via a new `require_current_session_id()`, §6.
6. Finding 1's exception-handler fix lacked an HTTP-level regression test — added, §10.
7. The `lifespan` restructuring description was wrong (claimed router-mounting needed
   to move before `FastAPI(...)`; it doesn't) — corrected, §5.
8. `demo_o2c.py`'s login-without-`company_id` breaks for a multi-entitlement admin —
   documented as an explicit single-company-admin constraint, §8.
9. `DEMO_SEED_ACTOR` needed to wrap `seed_demo()`'s entire body, not just the part
   already inside `company_context` (`_ensure_reference_data`/`_ensure_company` run
   before that block) — fixed, §8.

## 14. Round 2 Consensus Revisions

Round 2 (fresh Codex instance, same repo, given round 1's findings + the fixed plan)
returned **REJECTED** again with 4 findings — round 1's fix to Finding 1 turned out to
be real but incomplete (a genuine second layer of the same bug class, not a new
unrelated issue), plus a real oversight in §6's wording and a real gap in §10a's
directory list. All 4 verified against actual code before fixing (grepping every
`router.py` for a second checked-service call after the primary one; re-reading
`bootstrap_admin.py:83`):

1. `bootstrap_admin.py`'s `_ensure_company()` calls `masterdata_service.
   list_companies(session)` with no actor bound at all (Rule 10b) — §8's "no change"
   claim was wrong; `list_companies` needed the same checked/unchecked split as the
   six round-1 functions. Fixed, §7/§8.
2. Round 1's fix to Finding 1 only covered service-to-service calls — several
   **router handlers** also call a checked read function a second time (a documented,
   deliberate post-commit reload pattern, confirmed by reading `confirm_sales_order`'s
   own comment about `onupdate=func.now()`), which has the identical
   over-restriction bug. Extended the same checked-wrapper/unchecked-core fix to
   every router-level reload call site (`sales.confirm/ship`, `receivables.create_
   invoice/void_invoice/create_payment/void_payment/allocate_payment`), and renamed
   the private `_load_X` convention to a public `load_X_unchecked` naming (needed
   for cross-module reuse from `router.py`/`app/cli/`, which a leading underscore
   would misleadingly discourage). Fixed, §7.
3. §6's "become plain functions... calling service functions directly" wording didn't
   explicitly say the existing per-exception `HTTPException` translation around
   `switch_company()`/`logout()` stays — ambiguous enough to read as removing it,
   which would surface `CompanyNotEntitledError`/`InvalidSessionError` as unhandled
   500s. Clarified: only the actor-resolution boilerplate is deleted, the existing
   `try/except` blocks stay. Fixed, §6.
4. §10a's existing-test directory list was incomplete (missed
   `tests/e2e/test_seed_idempotent.py`, `tests/e2e/test_property_o2c_balances.py`,
   `tests/platform/permissions/{test_service,test_bootstrap_admin}.py`), and applying
   the admin fixture to a test that also makes a leak-checked HTTP request would
   produce a false-positive leak failure. Fixed: directory list extended; an explicit
   constraint added that the fixture and a leak-checked `client` call must never
   appear in the same test. Fixed, §10a.

## 15. Round 3 Consensus Revisions

Round 3 confirmed round 2's findings 1–3 were correctly resolved, and returned
**REJECTED** with 5 new findings — narrower in kind than rounds 1–2 (test-suite and
documentation completeness, plus one remaining code-boundary inconsistency, rather
than new authorization-bypass mechanisms), a real signal of convergence. All 5
verified (grepping actual file counts for `company_headers()`/`client` fixture usage
across `tests/**`, re-reading this plan's own §2/§7 text for the contradiction):

1. §10a still only covered tests calling `service.py` directly — it said nothing
   about the much larger set of tests using the real HTTP `client` fixture with
   `company_headers()`/`X-Company-Id` (confirmed: 19 files, out of 36 using `client`
   at all). Every one of those breaks post-cutover. This is the largest single gap
   found across all three rounds. Fixed: new `login_headers()` async helper design,
   full re-expression pattern for multi-company/missing-header test scenarios,
   explicitly flagged as the largest mechanical piece of this slice. §10a.
2. Round 2 finding 4's fixture-conflict resolution was itself incomplete:
   `test_bootstrap_admin.py` must run with genuinely no actor bound (using
   `TEST_ADMIN_ACTOR` there would mask a real regression in the very invariant it
   tests); `test_router.py`'s own `create_company()` setup helper needed a narrowly
   self-contained `with actor_context(...):` scope, not the forbidden whole-test
   fixture. Fixed, §10a.
3. `_assert_no_leaked_context()` only checked actor/session, never the third
   `ContextVar` (`company_context`) `AuthenticationMiddleware` also binds. Fixed:
   extended to check all three. §10.
4. `get_payment_by_external_ref` was described inconsistently between §2 (implying a
   shared `.read` code) and §7 (no check at all) — a real contradiction, since its
   actual caller is authorized by `.create`, not `.read`. Fixed: resolved
   unambiguously as unchecked, no code of its own; §2's earlier sentence retracted.
5. README scope was too narrow — only the walkthrough, not the architecture/
   multi-tenancy prose (still describing the old header-trust model) or
   `README.zh-TW.md`. Fixed: expanded to a full repo-wide stale-reference sweep
   across both README files and adjacent docstrings. §9.

Resubmitted to Codex for round 4.

## 16. Round 4 Consensus Revisions

Round 4 confirmed round 3's three-context leak check, README sweep, and
bootstrap-admin no-actor carve-out were all correctly resolved, and returned
**REJECTED** with 5 findings — one leftover text inconsistency, and four that all
trace to the same root cause: §10a's HTTP-test-migration design (introduced in round
3) was written from a plausible-sounding assumption about how existing test helpers
work, not verified against their actual code. All 5 verified this round by actually
reading `tests/ledger/_helpers.py`, `tests/receivables/_helpers.py`, `tests/platform/
permissions/_helpers.py`, and re-grepping the exact file/call-site counts:

1. §7 still had stale text describing `get_payment_by_external_ref` as "folded into
   `.read`... not a direct CLI entry point," contradicting §2's already-fixed text.
   Fixed: renamed `load_payment_by_external_ref_unchecked`, text aligned. §7.
2. The 19-file count for `company_headers()` was real but incomplete — 25 files
   reference `company_headers`/`X-Company-Id` total, 7 using only a literal header
   dict, never the helper. Fixed: scope corrected to all 25. §10a.
3. The design's core claim — that `create_company()` test helpers already set
   companies up via `db_session`/raw ORM — was simply false; they perform a real,
   anonymous `POST /api/v1/companies`. Fixed: redesigned around a raw-ORM-bootstrapped
   "anchor admin" fixture whose token authenticates the *real* `POST /companies`
   call (preserving that endpoint's HTTP coverage, not bypassing it), then a
   fresh per-test user is granted entitlement to the new company and logged in for
   real. §10a.
4. The first draft's `login_headers()` called `create_user_with_role()` (which
   already grants the role) and then `grant_test_role()` again for the
   same triple — a `uq_user_company_role` violation. Fixed: one grant call, not two;
   `grant_test_role()` reserved for genuinely additional grants. §10a.
5. Three "nonexistent company_id → 409" tests can't be mechanically converted at all
   — granting entitlement to a nonexistent company fails its own FK before the
   test's real assertion runs, and post-cutover no real HTTP caller can present a
   bogus `company_id` in the first place. Fixed: these three move to service-level
   tests with a manually-bound bogus `company_context`, preserving the underlying
   invariant as a non-HTTP-reachable one, documented as such. §10a.

Resubmitted to Codex for round 5.

## 17. Round 5 Consensus Revisions

Round 5 confirmed round 4 findings 1 and 5 (the stale `get_payment_by_external_ref`
text, and the three nonexistent-company-id tests) were correctly resolved. It
returned **REJECTED** with 5 more findings, **all still inside §10a** — reported to
the scope owner before fixing (given the plan had reached its fifth round with the
remaining substance entirely concentrated in one section); scope owner directed
continuing to full convergence. All 5 fixed below:

1. The round-4 redesign's helper signatures don't actually compose —
   `anchor_admin(db_session)` needs `client` for its login call but wasn't given one;
   `create_company(client, code, *, admin_token)` needs `db_session` to create/grant/
   log in the fresh per-company user but wasn't given one either; and the text still
   describes two different grant sequences in two places. Needs one fully specified,
   internally consistent fixture/helper API closing over both `client` and
   `db_session`.
2. Only `create_company()` was redesigned. `create_account`/`create_period` (ledger),
   `create_product`/order-lifecycle helpers (sales), and `tests/inventory/
   _helpers.py` (never mentioned at all) still build a plain `company_headers(...)`
   dict — every one of them needs the same bearer-token threading, not just company
   creation.
3. The 25-file scope only counted direct references. Files that only consume the
   `_helpers.py` functions transitively (`tests/core/test_events.py`,
   `tests/ledger/test_posting.py`, concurrency tests, etc.) also break once those
   helpers' signatures change. Real scope is at least 31 files and likely more once
   every transitive consumer is enumerated.
4. The "HTTP tests vs. direct-service tests never combine" separation this plan
   asserted (§10a, rounds 1–4) is false — `test_posting.py`,
   `test_period_close_concurrency.py`, `test_confirm_concurrency.py` genuinely mix
   authenticated HTTP setup with direct checked-service calls in the same test
   function, including inside spawned concurrent tasks. These need narrowly-scoped
   actor bindings around just the direct-service portions (including inside each
   spawned task individually, since `ContextVar` bindings don't cross task
   boundaries the way they cross `await` points in the same task), not the
   whole-test-fixture/HTTP-only category split this plan has assumed since round 1.
5. The startup-crash test (§10, `AuthenticationMiddleware` test module) is ordered
   wrong as described: by the time the `client` fixture's `LifespanManager` context is
   entered, `_classify_routes_or_crash` has already run — adding an unclassified
   route to `app.routes` inside the test body afterward is too late to trigger it.
   Needs a fixture/test structure that mutates routes *before* lifespan startup (a
   separate app instance + explicit `LifespanManager` invocation inside the test,
   not reuse of the shared `client` fixture for this one case).

**Fixed after a scope-owner check-in** (round 10 finding 4 — this paragraph
originally said "Not yet fixed" at the point this session paused to report status
and get direction; superseded once the scope owner directed continuing, and the
fixes below were applied in the same session). Given all five rounds' remaining
substance was concentrated entirely in §10a's test-migration mechanics — with §2
through §8 (the permission catalog, migration 0010, `AuthenticationMiddleware`, the
startup check, router/service-layer changes, and the CLI script changes) unchanged
by rounds 4 and 5.

All 5 fixed (§10a): (1) one complete `CompanyAuth`/`anchor_admin`/`create_company`
API closing over both `client` and `db_session` consistently; (2) threaded through
every downstream helper in all four `_helpers.py` files (ledger/sales/receivables/
inventory), not just company creation; (3) scope corrected to "at least 31 files,
import-graph sweep required" rather than a fixed count; (4) the false HTTP/
direct-service test separation replaced with narrowly-scoped `actor_context(...)`
bindings around just the direct-service calls (including an explicit
task-context-copy rule for the concurrency tests); (5) the startup-crash test
redesigned to call `_classify_routes_or_crash` directly against both the real `app`
and a throwaway unclassified-route app, bypassing the `client`/`LifespanManager`
ordering problem entirely rather than working around it.

Resubmitted to Codex for round 6.

## 18. Round 6 Consensus Revisions

Round 6 confirmed the startup-crash test redesign and the concurrency
context-copying rule (round 5 findings 5 and part of 4) were sound. It returned
**REJECTED** with 4 findings — round 5 findings 2 and 4 remained incomplete, plus a
fixture-registration mechanics issue and a lint failure. All 4 verified and fixed:

1. `anchor_admin` defined in `tests/_auth_helpers.py` would never be discovered by
   pytest — merely importing a module doesn't register a fixture defined in it, only
   `conftest.py` (or explicit re-export/`pytest_plugins`) does. Fixed: `anchor_admin`
   moved into `tests/conftest.py` itself; `_auth_helpers.py` keeps only plain
   functions/the `CompanyAuth` dataclass.
2. `tests/receivables/_helpers.py`'s `setup_shipped_order`/`ShippedOrderContext` — a
   composite helper calling `create_company` then seven more helpers in sequence,
   with dozens of callers extracting `ctx.company_id` — was never given an explicit
   signature/field update. Fixed: gains `db_session`/`anchor` parameters, threads
   `CompanyAuth` through its whole internal sequence, `ShippedOrderContext` gains an
   `auth: CompanyAuth` field replacing the bare `company_id`.
3. The mixed HTTP/direct-service list (round 5 finding 4) still only named 3 files
   after "fixing"; the repo has more (`test_credit_limit_plugin.py`,
   `test_shipping.py`, `test_payments.py`, `test_invoices.py` at minimum). Fixed:
   replaced the file-list approach entirely with a general rule ("any test calling
   `service.py` directly needs a narrow `actor_context` binding around that call,
   full-repo sweep required, no fixed list") rather than trying to enumerate a list
   that kept growing under review.
4. `_login_fresh_admin` assigned `create_user_with_role()`'s return to an unused
   `user_id`, tripping ruff's `F841` and failing this project's own `ruff check`
   validation gate. Fixed: call without assignment.

Resubmitted to Codex for round 7.

## 19. Round 7 Consensus Revisions

Round 7 confirmed round 6 findings 1, 2, and 4, and the file-list→general-rule shift
were all correct. It returned **REJECTED** with 2 findings — the narrowest round
yet, both still inside §10a:

1. The scope sweep still missed a category invisible to both prior methods
   (header-text grep and `_helpers.py` import-graph): protected requests carrying
   **no header at all** today (`tests/masterdata/test_company_functional_currency.py`'s
   anonymous `POST /companies` calls, `test_reference_data_api.py`'s anonymous
   `POST /uom-conversions`/`POST /exchange-rates`), plus five `tests/masterdata/`
   files with their own local `_create_company()` helpers never folded into the
   shared design. Fixed: extended the shared `create_company`/`CompanyAuth` design
   to all five `tests/masterdata/` files; replaced the scope methodology itself with
   a route-aware full-repo sweep (every `client.*` call site cross-referenced
   against §2's permission table) rather than another text/import grep.
2. Round 6's replacement rule ("any direct `service.py` call needs an actor
   binding") was itself overbroad — it would wrongly bind an actor around calls to
   functions this plan defines as deliberately unchecked, and would break the
   Rule-10b bootstrap tests and any negative-authorization test that specifically
   needs no/insufficient actor bound. Fixed: rule scoped to "direct calls to a
   function in the finalized checked-function set, in a test that isn't itself
   testing that function's own authorization boundary."

Resubmitted to Codex for round 8.

## 20. Round 8 Consensus Revisions

Round 8 returned **REJECTED** with 2 findings — both concrete, both fixed:

1. The route-aware sweep methodology (round 7's fix) only listed `.post`/`.patch`/
   `.delete`/`.get`, missing `client.request(method, path)` — confirmed real,
   `tests/masterdata/test_cross_company_isolation.py` and `tests/ledger/
   test_cross_company_isolation.py` each have one test using exactly that form,
   currently expecting 403, which becomes 401 post-cutover. Fixed: sweep target
   extended to every `client.<verb>()` **and** `client.request(...)` call; both
   specific tests explicitly named as migrating to the already-planned
   missing-token/401 re-expression.
2. §10a's own text still contained an internal contradiction: an early
   whole-test-wrapping `bind_test_admin_actor` fixture concept (rounds 1–2) was
   never reconciled with the later narrow-scope-only rule (rounds 6–7) — concretely
   exposed by `test_bootstrap_admin.py`, which needs both "no actor" (around
   `bootstrap_admin()` itself) and "narrow `TEST_ADMIN_ACTOR`" (around its own
   `list_companies()` assertion call) *within the same test function*. Fixed: the
   whole-test fixture concept is retired entirely; `TEST_ADMIN_ACTOR` is a plain
   constant, bound via an explicit local `with actor_context(...):` at every call
   site needing it, one single mechanism used consistently everywhere in this
   document, including inside `test_bootstrap_admin.py`'s own split need.

Resubmitted to Codex for round 9.

## 21. Round 9 Consensus Revisions

Round 9 explicitly confirmed round 8's `.request()` sweep expansion and
`test_bootstrap_admin.py`'s split actor/no-actor handling were "correct and
complete." It returned **REJECTED** with 2 findings, both pure textual-consistency
cleanups — leftover text from before the design was fixed, not new substance:

1. §12 question 6 still called `TEST_ADMIN_ACTOR` a "fixture actor," contradicting
   §10a's own corrected plain-constant/explicit-local-binding design (round 8
   finding 2). Fixed: reworded to match.
2. §10's `tests/test_smoke.py` bullet still described the pre-round-5-finding-5
   approach (add a throwaway route to the shared app inside the test, remove after)
   for the startup-crash test, contradicting §10's own corrected standalone
   `_classify_routes_or_crash` design. Fixed: reworded to describe only the two
   direct, synchronous calls (real `app`, no raise; throwaway inline `FastAPI()`,
   raise expected) — no shared-app mutation.

Resubmitted to Codex for round 10.

## 22. Round 10 Consensus Revisions

Round 10 explicitly reconfirmed "§§2–8 remain sound" and both round 9 fixes were
correct. It returned **REJECTED** with 4 findings, all narrow textual-accuracy
issues — no new design problems:

1. §10a's list of deliberately-unchecked `platform.permissions.service` functions
   said "login/resolve_actor and every other" function in that module, contradicting
   §7's own text (which adds `require_permission("session.manage")` to
   `switch_company`/`logout`). Fixed: corrected the list; added that
   `tests/platform/permissions/test_service.py`'s direct `switch_company`/`logout`
   calls need the same narrow-binding treatment as any other checked function
   (unless the specific test is asserting the denial path).
2. §10a claimed five `tests/masterdata/` files have a local `_create_company()`
   helper; only four do — `test_company_functional_currency.py` makes direct inline
   `POST /companies` calls with no helper function. Fixed: corrected in both places
   this was stated; clarified all five files still need migrating, just via
   different mechanisms (four via their helper, one via its inline call sites
   directly).
3. One leftover "broad fixture actor" phrase (§10a, `test_bootstrap_admin.py`
   discussion) survived round 8's fixture-retirement fix. Fixed: reworded.
4. Two accounting errors: §0 said "five `app/cli/*.py` scripts" (there are six:
   `bootstrap_admin`/`demo_o2c`/`rebuild_ar_balances`/`rebuild_stock_summary`/
   `replay_outbox`/`seed_demo`); §17 still contained a "Not yet fixed" sentence
   immediately after stating all five of its findings were fixed (leftover from
   before the scope-owner check-in). Fixed both.

Resubmitted to Codex for round 11.

## 23. Round 11 Consensus Revisions

Round 11 explicitly reconfirmed all four round-10 fixes and that "§§2–8 remain
substantively sound." It returned **REJECTED** with 2 findings (internal cross-references,
fixed in place).

## 24. Round 12 Consensus Revisions

Round 12 returned **REJECTED** with 5 substantive findings — all 5 verified against actual
code and fixed in place across §§1, 2, 3, 7, 8, 9, and 10:

1. **`Settings.environment` alias for `APP_ENVIRONMENT`**:
   - `Settings` in `app/core/settings.py` configured with
     `validation_alias=AliasChoices("APP_ENVIRONMENT", "ENVIRONMENT", "environment")`
     (using `pydantic.AliasChoices`) so that `APP_ENVIRONMENT=production` in `.env.example`
     and `docker-compose.yml` is parsed into `settings.environment` properly and does not
     fail-open to default `"development"`.
   - `seed_demo.py` checks `settings.environment == "production"` and fails fast unless
     `--allow-production` is explicitly supplied.
   - Tested via `tests/cli/test_seed_demo.py` asserting refusal when `APP_ENVIRONMENT=production`.

2. **Service boundary placement rule vs. direct CLI queries**:
   - `seed_demo.py` is updated to call the public query `inventory_service.list_stock_summary(session, product_id=product_id)`
     (which enforces `require_permission("inventory.summary.read")`) to read `on_hand`,
     instead of calling the internal mutex helper `get_or_lock_summary`.
   - `receivables_service.get_payment_by_external_ref` enforces `require_permission("receivables.payment.read")`,
     with `create_payment`'s 409 recovery using `load_payment_by_external_ref_unchecked`
     to avoid double-checking `.read` during `.create`.
   - `DEMO_SEED_ACTOR` permission set extended to 23 codes to include `inventory.summary.read` and
     `receivables.payment.read`.

3. **Migration 0010 `permissions.description` non-null column data**:
   - Enumerate deterministic `description` strings for all 44 permissions in §2 table and §3.
   - Migration 0010 bulk-inserts `{"code": "...", "description": "..."}` for all 44 rows
     and grants all 44 to `admin` role.

4. **README walkthrough post-bootstrap conflict and tenancy resolution**:
   - Updated §9 walkthrough: recognizes that `bootstrap_admin` already seeds `TWD`, `EA`, and company `MAIN`.
   - The walkthrough logs in as admin, captures `$TOKEN` (and `active_company_id` for `MAIN`),
     and executes subsequent steps within `MAIN` using `-H "Authorization: Bearer $TOKEN"`,
     avoiding duplicate reference data 409 conflicts and unauthorized company 403s.

5. **Decision 5 live-server `demo_o2c` testing strategy**:
   - `tests/e2e/test_demo_o2c_smoke.py` retained as in-process ASGI smoke test.
   - Added `tests/e2e/test_demo_o2c_live.py` running `demo_o2c.py` end-to-end against a live
     Uvicorn server on an ephemeral port, verifying real socket HTTP authentication and
     transaction lifecycle per Decision 5.

Resubmitted to Codex for round 13.

## 25. Round 13 Consensus Revisions

Round 13 returned **REJECTED** with 5 findings — all 5 verified against actual code
and fixed in place across §§8, 9, and 10:

1. **Dual `get_or_lock_summary()` migration + empty sequence safety in `seed_demo.py`**:
   - Migrated both line 368 (`_reconcile_stock`) AND line 561 (`_assert_invariants`) to
     `inventory_service.list_stock_summary(session, product_id=product_id)`.
   - Added explicit empty sequence handling at both sites:
     `current_on_hand = rows[0].on_hand if rows else Decimal("0")`.

2. **Subprocess-based live-server test execution strategy for `demo_o2c`**:
   - `tests/e2e/test_demo_o2c_live.py` implements an explicit, robust subprocess test:
     1. Allocates ephemeral port via socket bind `("127.0.0.1", 0)`.
     2. Spawns `uvicorn` in a dedicated OS subprocess (`subprocess.Popen`).
     3. Readiness polling against `/health` (up to 5s).
     4. Runs `demo_o2c.py` in a separate child process (`subprocess.run`) with
        `DEMO_BASE_URL=http://127.0.0.1:<port>` in `env` (preventing module-level freeze).
     5. Asserts exit code 0 and output.
     6. Teardown terminates Uvicorn process cleanly.

3. **Complete Quick Start, Makefile, and Docker Compose bootstrap rollout**:
   - Updated Quick Start in `README.md` and `README.zh-TW.md` to insert `bootstrap_admin`
     before `make demo`.
   - Updated `Makefile` and `docker-compose.yml` comments to specify `bootstrap_admin` as prerequisite.

4. **Production seed override contract & test suite**:
   - Clarified override contract: both `--allow-production` CLI flag and `ALLOW_SEED_DEMO_IN_PRODUCTION=1`
     (or `"true"`) are accepted; invalid values like `"0"` or `"false"` are rejected.
   - `tests/cli/test_seed_demo.py` covers refusal, both valid overrides, invalid overrides,
     and calls `get_settings.cache_clear()` to avoid settings cache pollution.

5. **Trust boundary cleanup (`tenant_header_name` retirement)**:
   - Deleted `tenant_header_name` setting and obsolete trusted-header comment block from `Settings`.
   - Sweep all docstrings and code comments to remove obsolete `X-Company-Id` header-trust explanations.

Resubmitted to Codex for round 14.

## 26. Round 14 Consensus Revisions

Round 14 returned **REJECTED** with 5 findings — all 5 verified against actual code
and fixed in place across §§7, 8, 9, and 10:

1. **Subprocess live-server test pre-boot bootstrap admin & teardown kill fallback**:
   - `tests/e2e/test_demo_o2c_live.py`: executes `bootstrap_admin` with known test credentials
     and commits to DB before spawning Uvicorn, preventing 401 on fresh DB / truncated tables.
   - Teardown uses `try/finally` with `proc.terminate()` and `proc.kill()` fallback if not
     terminated within 5s.

2. **Explicit bootstrap credentials & full rollout across README/Makefile/Compose**:
   - Canonical bootstrap command uses explicit environment variables:
     `BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_password uv run python -m app.cli.bootstrap_admin`
     (or via `docker compose exec -T -e ...`).
   - `.env.example` includes example admin credential placeholders.
   - Mandatory ordering enforced: `bootstrap_admin` must run before `make seed` and `make demo`.
   - Updated Quick Start in `README.md`, `README.zh-TW.md`, `Makefile`, and `docker-compose.yml`.

3. **Dual stock query dead-code & row-lock cleanup in `seed_demo.py`**:
   - Removed unused `company_id = require_current_company_id()` call and its import from line 367.
   - Removed obsolete row-lock comments and lock-release commits (`await session.commit()`)
     from lines 369-373 and 562-564.

4. **Shared programmatic and CLI production guard boundary**:
   - Guard placed directly inside the core reusable `seed_demo(*, allow_production: bool = False)`
     function, inspecting `get_settings().environment == "production"` at execution time.
   - Rejection occurs whether invoked via CLI or programmatically unless override is present.
   - `_main()` forwards `--allow-production` CLI flag.
   - `tests/cli/test_seed_demo.py` covers both programmatic calls and CLI subprocesses across
     all 4 override states with `get_settings.cache_clear()` in guaranteed `try/finally` fixture teardown.

5. **`load_payment_by_external_ref_unchecked` naming & 409 regression test**:
   - Formally named `load_payment_by_external_ref_unchecked` (no leading underscore, conforming
     to cross-module public helper standard) for `receivables.router` 409 conflict recovery.
   - Added regression test in `tests/receivables/test_router.py` asserting an actor with only
     `receivables.payment.create` receives the expected 409 response on conflict without failing
     on a `.read` check.

Resubmitted to Codex for round 15.

## 27. Round 15 Consensus Revisions

Round 15 returned **REJECTED** with 3 findings (Round 14 findings 1, 3, 4 confirmed PASS;
findings 2 and 5 partially remained; new gap in function enumeration). All 3 verified against
actual code and fixed in place across §§1, 2, 7, 8, and 9:

1. **Concrete Makefile / Docker Compose bootstrap definition & credential injection**:
   - Resolved all ambiguity in §1 Finding 3 and §9: `bootstrap_admin.py` is the canonical
     first-install tool that exits 0 on success and exits 1 on `BootstrapRefusedError`.
   - `Makefile` concrete targets added with defaults `BOOTSTRAP_ADMIN_EMAIL ?= admin@example.com`
     and `BOOTSTRAP_ADMIN_PASSWORD ?= adminpassword`.
   - `docker-compose.yml` explicitly injects `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`
     into the `app` service environment, guaranteeing that host commands and `docker compose exec app ...`
     are immediately executable.
   - Strict 3-step Quick Start ordering defined: (1) `alembic upgrade head`, (2) `make bootstrap`, (3) `make demo`.

2. **Complete `load_payment_by_external_ref_unchecked` naming consistency**:
   - Replaced all remaining occurrences of `_get_payment_by_external_ref_core` in §1 Finding 2,
     §2 note, and throughout the text with `load_payment_by_external_ref_unchecked`.
   - Conforms strictly to the un-prefixed cross-module helper naming standard (`load_X_unchecked`).

3. **Exhaustive Function-by-Function Service Authorization Matrix**:
   - Added §7.1: complete, module-by-module matrix covering every single service function in all 6 modules
     (`masterdata`, `sales`, `ledger`, `inventory`, `receivables`, `platform.permissions`),
     its checked permission code, whether it is a checked wrapper or unchecked core, and its permitted callers.
   - Clarified that `inventory_service.get_or_lock_summary`, `apply_stock_move`, `post_journal_entry` internal core,
     `list_uoms`, and `list_currencies` are unchecked cores/functions with explicit permitted callers.

Resubmitted to Codex for round 16.

## 28. Round 16 Consensus Revisions

Round 16 returned **REJECTED** with 4 findings (Round 15 finding 2 confirmed PASS; findings 1 and 3 refined with actionable specifics):

1. **No hardcoded/fallback password defaults per ADR-009 Decision 6**:
   - Removed insecure `adminpassword` default values from `Makefile`, `docker-compose.yml`, and plan documentation.
   - `Makefile` and `docker compose exec` mandate providing `BOOTSTRAP_ADMIN_PASSWORD` in the environment, failing fast with an error message if empty.
   - `.env.example` documents `BOOTSTRAP_ADMIN_EMAIL=admin@example.com` and `BOOTSTRAP_ADMIN_PASSWORD=` without hardcoded secrets.

2. **Docker-consistent Quick Start & Makefile workflow**:
   - Quick Start sequence formalized: (1) `make up` (starts container stack, runs migrations, waits for health), (2) `BOOTSTRAP_ADMIN_PASSWORD=... make bootstrap` (executes bootstrap inside `app` container), (3) `BOOTSTRAP_ADMIN_PASSWORD=... make demo` (hits live API socket on host) or `make seed` (executes seed inside container).
   - Preserves `docker compose exec -T app` container-internal execution for DB tools (`bootstrap_admin`, `seed_demo`) while executing live HTTP client (`demo_o2c`) on the host.

3. **Truly Exhaustive Service Function Matrix (§7.1)**:
   - Enumerated 100% of service functions, private helpers, transaction utilities, and platform security functions across all 6 modules.
   - Added all `platform.permissions` functions (`hash_password`, `verify_password`, `_hash_token`, `_resolve_permission_codes`, `login`, `resolve_actor`, `switch_company`, `logout`, `create_user`, `grant_role`, `add_user_with_grant`, `count_users`) with detailed rationale and allowed callers.
   - Added all private transaction, constraint translation, and draft helpers across `masterdata`, `sales`, `ledger`, `inventory`, and `receivables`.

4. **Corrected `receivables` service function name (`get_ar_aging`)**:
   - Mapped `receivables.report.read` specifically to the actual service function `get_ar_aging` (line 660 in `app/modules/receivables/service.py`), eliminating the mislabeled `get_ar_aging_report`.

Resubmitted to Codex for round 17.

## 29. Round 17 Consensus Revisions

Round 17 returned **REJECTED** with 4 findings (Docker-consistent execution shape and `get_ar_aging` confirmed PASS; remaining findings addressed with strict code verification):

1. **Both email and password strictly mandatory with zero code defaults (ADR-009 Decision 6)**:
   - Removed `BOOTSTRAP_ADMIN_EMAIL ?= admin@example.com` from `Makefile` and `:-admin@example.com` from `docker-compose.yml`.
   - Both `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` must be provided in the environment; `make bootstrap` and `make demo` check both and fail fast if either is missing.
   - `.env.example` documents empty placeholders `BOOTSTRAP_ADMIN_EMAIL=` and `BOOTSTRAP_ADMIN_PASSWORD=`.

2. **Router path, HTTP method, and transaction matrix accuracy (§7.1, §7.2)**:
   - Fixed all HTTP methods in matrix: `PATCH /customers/{id}`, `PATCH /products/{id}`, `PATCH /accounts/{id}`, `PATCH /sales-orders/{id}`.
   - Fixed ledger period routes to match actual router: `POST /periods`, `GET /periods`, `POST /periods/{id}/close`.
   - Removed nonexistent `_commit_or_conflict` from `receivables.service` (clarified that `receivables.router` owns the commit and error translation boundary).
   - Explicitly listed `load_company_unchecked` and `load_period_unchecked` in §7.2's declared split list.

3. **Deletion of obsolete `create_user` and `grant_role` from `platform.permissions.service`**:
   - `create_user` and `grant_role` (which performed individual, unguarded commits) are formally deleted from the production codebase in Slice D after migrating tests to use `add_user_with_grant`.
   - `add_user_with_grant` and `count_users` remain the sole bootstrap helpers under Rule 10b.

4. **Self-scoping `session.manage` at the service layer**:
   - `switch_company(session, company_id: uuid.UUID)` and `logout(session: AsyncSession)` at the service layer read `session_id = require_current_session_id()` directly.
   - The service verifies that the session belongs to `actor.user_id` (via `current_actor_or_none()`), structurally preventing any user with `session.manage` from passing an arbitrary session ID to mutate or revoke another user's session.
   - Routers `POST /auth/switch-company` and `POST /auth/logout` no longer pass session IDs from request state.

Resubmitted to Codex for round 18.

## 30. Round 18 Consensus Revisions

Round 18 returned **REJECTED** with 3 findings (Round 17 finding 2 confirmed PASS; findings 1, 3, and 4 refined with exact code semantics):

1. **Pure exec -e credential transport & Compose fallback clarification**:
   - Clarified that `docker-compose.yml` retains necessary infrastructure fallback defaults (`POSTGRES_*`, `APP_PORT`, `DATABASE_URL`, `APP_ENVIRONMENT` with `:-`) to support `make up`, while administrative credentials (`BOOTSTRAP_ADMIN_*`) carry zero defaults and are injected purely at invocation time via `docker compose exec -T -e ...`.
   - Removed all `--password` and `--email` CLI argument usage in Makefile to prevent secret exposure in container process argument lists.

2. **Canonical 3-tier test helper migration (`tests/platform/permissions/_helpers.py`)**:
   - Explicitly defined the 3 canonical helpers:
     1. `create_user_with_role(session, email, password, company_id, role_code)`: calls `permissions_service.add_user_with_grant` and commits.
     2. `create_test_user(session, email, password) -> uuid.UUID`: creates raw ORM `User` with mandatory `password_hash=permissions_service.hash_password(password)` and commits.
     3. `grant_test_role(session, user_id, company_id, role_code) -> None`: creates raw ORM `UserCompanyRole` and commits.
   - Completely deleted all references to `grant_company_entitlement`.

3. **Type-safe fail-closed `_assert_current_actor_owns_session` for `session.manage`**:
   - Added `_assert_current_actor_owns_session(session_row)` helper in `platform.permissions.service` that checks `actor = current_actor_or_none()`, fails closed with `InvalidSessionError` if `actor is None`, performs strict type narrowing, and validates `session_row.user_id == actor.user_id`.
   - Direct service tests resolve actor and bind matching `actor_context` and `session_context`.
   - Added cross-user adversarial test ensuring mismatched session binding raises `InvalidSessionError`.

Resubmitted to Codex for round 19.

## 31. Round 19 Consensus Revisions (Guided by Codex Luna)

Round 19 returned **REJECTED** with 3 findings. Consulting Codex Luna (`gpt-5.6-luna`, High reasoning), all 3 findings were resolved with rigorous code and plan alignment:

1. **Docker / Compose Credential Isolation & Infrastructure Fallback Clarification**:
   - Scope clarification: `docker-compose.yml` retains standard infrastructure variable fallbacks (`POSTGRES_*`, `PORT`, etc. using `:-`) to keep `make up` out-of-the-box functional. Administrative bootstrap credentials carry strictly zero defaults and are omitted from static compose environment blocks.
   - Transport security: `Makefile` uses pure `docker compose exec -T -e BOOTSTRAP_ADMIN_EMAIL="..." -e BOOTSTRAP_ADMIN_PASSWORD="..." app python -m app.cli.bootstrap_admin` without passing `--email` or `--password` in process commandline arguments, preventing secret leakage via `/proc/<pid>/cmdline`. `bootstrap_admin.py` reads directly from `os.environ`.

2. **Unified 3-Tier Test Helper Naming & Explicit ORM Password Hash**:
   - Fixed naming conflict: `grant_company_entitlement` is completely eliminated in favor of canonical `grant_test_role(session, user_id, company_id, role_code) -> None`.
   - The 3 test helpers in `tests/platform/permissions/_helpers.py` are strictly:
     1. `create_user_with_role(session, email, password, company_id, role_code)`: wraps `add_user_with_grant` + `await session.commit()`.
     2. `create_test_user(session, email, password) -> uuid.UUID`: raw ORM insert with `password_hash=permissions_service.hash_password(password)` + commit & refresh.
     3. `grant_test_role(session, user_id, company_id, role_code) -> None`: raw ORM `UserCompanyRole` insert + commit for secondary entitlements.

3. **Type-Safe `current_actor_or_none()` None-Guard & Fail-Closed Ownership Verification**:
   - Introduced `_assert_current_actor_owns_session(session_row: SessionModel) -> None` in `platform.permissions.service`.
   - Explicitly evaluates `actor = current_actor_or_none()`. If `actor is None`, fails closed by raising `InvalidSessionError("No actor context is bound for the current session.")`.
   - Performs strict type narrowing on `actor`, then verifies `if session_row.user_id != actor.user_id: raise InvalidSessionError("The current actor does not own the requested session.")`.
   - Satisfies strict mypy rules across `switch_company` and `logout`.
   - Direct service tests resolve actor token and bind matching `with actor_context(actor.actor_context), session_context(result.session_id):`.
   - Adversarial test proves Actor A bound to Session B is rejected with `InvalidSessionError`.

Resubmitted to Codex for round 20.
