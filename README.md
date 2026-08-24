# mini-erp

English | [繁體中文](README.zh-TW.md)

![coverage](https://raw.githubusercontent.com/Pushplayhero/mini-ERP/badges/coverage.svg)

> Not a Digiwin with more features — the Digiwin Taiwanese SMEs can
> self-host, extend by writing a plugin instead of buying consultant
> hours, and actually trust with their data. Open-source, API-first,
> plugin-extensible ERP kernel.

**This repository state = Phase 1 ("Kernel") complete.** Five business
modules — masterdata, ledger, sales, inventory + shipping, receivables —
implement a full order-to-cash (O2C) line on top of a real double-entry
posting engine, each backed by an [architecture decision
record](docs/adr/README.md); ADR-003 through ADR-008 each passed a real
`codex` CLI consensus review before implementation, and ADR-001/ADR-002
document decisions already made and shipped in Weeks 1-2 (see "Design
Decisions" below for the distinction). See `docs/open-erp-master-plan.md`
for the longer-term plan (Platform → Suites → Taiwan localization) and
`docs/mini-erp-architecture.md` for the original kernel blueprint this
phase implements.

## Non-Goals (Phase 1)

Out of scope until Phase 2+: a plugin **loader** (Phase 1 ships one
hard-wired demonstration plugin — `app/plugins/credit_limit.py` — not a
dynamic loading mechanism), workflow/approval engine, user-defined custom fields UI (the `custom_data JSONB` column
exists on every primary business entity — companies, customers, products,
accounts, orders, invoices, payments, journal entries, accounting periods
— as the underlying mechanism, though not on line/fact-level tables like
order lines, journal lines, or stock moves; there is also no admin UI or
central field-definition table yet), and any frontend
(this is an API-only kernel — `/docs` is the interactive client for now).
Taiwan-specific tax/e-invoice integration is Phase 4.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Pydantic v2 ·
PostgreSQL 16 (strict types, DB-level constraints) · Alembic · pytest +
pytest-asyncio + testcontainers-postgres. Tested on Linux and Windows.

## Why this exists

A lot of ERP tutorials demonstrate "how to insert into a `sales_orders`
table" and stop. Real ERP kernels are interesting because of the
invariants they must maintain *across* tables:

- **Immutability of posted financial state:** Once an accounting period
  closes or a journal entry posts, it is NEVER mutated or deleted.
  Corrections are posted as explicit reversals (ADR-005).
- **The O2C invariants hold under concurrency:** Two users trying to
  fulfill the last unit of stock or exceed a credit limit simultaneously
  cannot both succeed; one commits, one gets a structured `409 Conflict`
  (ADR-006, ADR-007).
- **Every financial movement is backed by balanced double-entry
  bookkeeping:** A shipment posts COGS + Inventory; an invoice posts AR +
  Revenue; a payment posts Cash + AR. No money moves without two balanced
  sides.
- **Fail-closed multi-tenancy:** A query without an active company context
  RAISES; it does not return all rows or silently filter to nothing.

This codebase is a clean, minimal reference implementation of those
rules. It is structured to be read top-to-bottom, with ADRs documenting
every non-obvious decision.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
cp .env.example .env

export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export BOOTSTRAP_ADMIN_PASSWORD='change-this-password'
export BOOTSTRAP_COMPANY_CODE=MAIN
export BOOTSTRAP_COMPANY_NAME='Main Company'

make up
make bootstrap
make seed
make demo
make check
```

`make bootstrap` is the first-install command that creates the initial
administrator user and company. `make seed` populates the demo dataset
(and is safe to rerun idempotently). `make demo` executes the live O2C walkthrough.

`make test` runs the full test suite against an ephemeral PostgreSQL
container (via `testcontainers-postgres` when Docker is running, with
automatic fallback to embedded `pgserver` for sandboxed/no-daemon
environments). The Docker-compose path (`make up` + `make bootstrap` + `make seed` + `make demo`)
is the reference workflow for end-to-end testing.

## Quick start

### Docker Compose

```bash
cp .env.example .env

export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export BOOTSTRAP_ADMIN_PASSWORD='change-this-password'
export BOOTSTRAP_COMPANY_CODE=MAIN
export BOOTSTRAP_COMPANY_NAME='Main Company'

make up
make bootstrap
make seed
make demo
```

`make bootstrap` creates the initial administrator user and tenant company
(first-install only). `make seed` populates the demo dataset idempotently.

`make demo`'s output looks like this (see "Try it" below for the same
flow as raw `curl`):

```
Order SO-2026-000001: shipped, total 300.000000
Invoice INV-2026-000001: open, total 300.000000
Payment received and fully allocated.

Trial balance:
  code   account                             debit           credit
  1000   Cash                           300.000000         0.000000
  1100   Accounts Receivable            300.000000       300.000000
  1300   Inventory                        0.000000        90.000000
  4000   Revenue                          0.000000       300.000000
  5000   COGS                            90.000000         0.000000
         TOTAL                          690.000000       690.000000
```

Other Makefile targets: `make down` (stop), `make clean` (stop + drop the
volume), `make test` (`pytest` inside your own environment), `make check`
(the exact `ruff check` → `ruff format --check` → `mypy` → `lint-imports`
→ `pytest` sequence CI runs).

### Running on your host (no Docker)

Start a PostgreSQL instance, then configure the application explicitly:

```bash
uv sync --group dev
cp .env.example .env

export DATABASE_URL='postgresql+asyncpg://mini_erp:mini_erp@localhost:5432/mini_erp'
export APP_ENVIRONMENT=development
export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export BOOTSTRAP_ADMIN_PASSWORD='change-this-password'
export BOOTSTRAP_COMPANY_CODE=MAIN
export BOOTSTRAP_COMPANY_NAME='Main Company'

uv run alembic upgrade head
```

In terminal 1, start the API:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In terminal 2, run the direct CLI commands:

```bash
uv run python -m app.cli.bootstrap_admin
uv run python -m app.cli.seed_demo
DEMO_BASE_URL=http://127.0.0.1:8000 uv run python -m app.cli.demo_o2c
```

`seed_demo` talks directly to PostgreSQL. `demo_o2c` is an HTTP client
and therefore requires the running uvicorn process.

## Architecture

```mermaid
graph TB
    Client["curl / API client"]

    subgraph App["mini-erp — one FastAPI process (modular monolith, ADR-001)"]
        MW["AuthenticationMiddleware<br/>verifies Bearer token & binds Session Context"]

        subgraph Modules["business modules — independent, no cross-module imports (CI-enforced)"]
            MD["masterdata<br/>companies · customers · products · UoM · accounts"]
            SA["sales<br/>orders: draft → confirmed → shipped"]
            IN["inventory<br/>append-only stock_moves + stock_summary"]
            RE["receivables<br/>invoices · payments · AR aging"]
        end

        PL["plugins<br/>credit_limit hook (ADR-006)"]
        EB["event bus<br/>app/core/events.py (ADR-004)"]
        LE["ledger<br/>posting engine + trial balance (ADR-003, ADR-005)"]
    end

    PG[("PostgreSQL 16<br/>append-only fact tables, ADR-002")]

    Client -->|"HTTP + Authorization: Bearer <token>"| MW
    MW --> MD & SA & IN & RE
    SA -->|"sales.order.validate_confirm hook"| PL
    SA -->|"publish: order_confirmed (no subscriber yet), goods_shipped"| EB
    RE -->|"publish: invoice_issued/voided, payment_received/voided"| EB
    EB -->|"subscriber to goods_shipped, runs FIRST: deduct stock"| IN
    EB -->|"subscriber to goods_shipped (runs SECOND) + all 4 receivables events: post a balanced journal entry"| LE
    MD & SA & IN & RE & LE --> PG
```

Every arrow into a *tenant-scoped* operation crosses the
active Session Context tenancy filter (see "Multi-tenancy" below) before
any query runs — currency/UoM reference data is the one exception
(global, not company-owned; see the `curl` walkthrough below). `sales`
publishes `order_confirmed` too, but it currently has no subscriber at
all (ADR-006 Decision 4) — registered so `publish()`/replay still
validate its schema and it still gets an outbox row, but nothing reacts
to "confirmed" on its own; the ledger/inventory reaction happens
downstream, at `ship`. `goods_shipped` is the interesting one: the event
bus dispatches it to **two** subscribers in the SAME transaction, and
**their order is normative, not incidental** (ADR-007 Decision 1) —
`inventory` (deducts stock) runs before `ledger.posting` (posts Dr COGS
/ Cr Inventory), "move the goods, then account for them"; either order
would still commit or roll back atomically together, but the ordering
determines which handler's exception is attributed as the failure
reason on a bad `ship`. Full O2C flow: order confirm → ship (posts
COGS/Inventory, deducts stock) → `receivables` invoice (posts
AR/Revenue) → payment (posts Cash/AR) → allocation (subledger-only,
posts nothing) — proved end to end by
`tests/e2e/test_o2c_end_to_end.py` and, for Hypothesis-generated (bounded,
not literally unbounded) legal operation sequences,
`tests/e2e/test_property_o2c_balances.py`.

## Try it: the O2C flow as `curl`

First, ensure the app is up and bootstrapped:

```bash
make up

export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export BOOTSTRAP_ADMIN_PASSWORD='change-this-password'
export BOOTSTRAP_COMPANY_CODE=MAIN
export BOOTSTRAP_COMPANY_NAME='Main Company'

make bootstrap
```

Then run the full O2C flow using bearer session authentication (redact/replace
the UUIDs with whatever your own run produces):

```bash
BASE=http://localhost:8000
API=$BASE/api/v1

# 1. Login to obtain a bearer session token (ADR-009 Authentication).
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"change-this-password"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Every authenticated request uses Authorization: Bearer <token> (tenancy is
# resolved ambiently from the active session).
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")

# Reference data (currencies, UoM) and the initial company are seeded at bootstrap.
# Discover the seeded UoM:
UOM_ID=$(curl -s $API/uom "${H[@]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')

curl -s -X POST $API/periods "${H[@]}" -d '{"year":2026,"month":8}' > /dev/null
for row in 1000:Cash:asset 1100:"Accounts Receivable":asset 1300:Inventory:asset \
           4000:Revenue:revenue 5000:COGS:expense; do
  IFS=: read -r code name type <<< "$row"
  curl -s -X POST $API/accounts "${H[@]}" -d "{\"code\":\"$code\",\"name\":\"$name\",\"type\":\"$type\"}" > /dev/null
done

CUSTOMER_ID=$(curl -s -X POST $API/customers "${H[@]}" \
  -d '{"code":"CUST-001","name":"Test Customer","currency_code":"TWD","credit_limit":"50000"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

PRODUCT_ID=$(curl -s -X POST $API/products "${H[@]}" \
  -d "{\"sku\":\"WIDGET-1\",\"name\":\"Widget\",\"uom_id\":\"$UOM_ID\",\"list_price\":\"100\",\"standard_cost\":\"30\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST $API/inventory/adjustments "${H[@]}" \
  -d "{\"product_id\":\"$PRODUCT_ID\",\"qty_delta\":\"10\",\"reason\":\"initial stock\"}" > /dev/null

# --- the actual O2C line ---

ORDER_ID=$(curl -s -X POST $API/sales-orders "${H[@]}" \
  -d "{\"customer_id\":\"$CUSTOMER_ID\",\"lines\":[{\"product_id\":\"$PRODUCT_ID\",\"qty\":\"2\",\"unit_price\":\"100\"}]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST $API/sales-orders/$ORDER_ID/confirm "${H[@]}" > /dev/null
curl -s -X POST $API/sales-orders/$ORDER_ID/ship "${H[@]}"
# {"...","status":"shipped","total":"200.000000",...}  — posts Dr 5000 COGS 60 / Cr 1300 Inventory 60

INVOICE_ID=$(curl -s -X POST $API/receivables/invoices "${H[@]}" -d "{\"order_id\":\"$ORDER_ID\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# posts Dr 1100 AR 200 / Cr 4000 Revenue 200

PAYMENT_ID=$(curl -s -X POST $API/receivables/payments "${H[@]}" \
  -d '{"customer_id":"'"$CUSTOMER_ID"'","external_ref":"PAY-001","amount":"200"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# posts Dr 1000 Cash 200 / Cr 1100 AR 200

curl -s -X POST $API/receivables/payments/$PAYMENT_ID/allocations "${H[@]}" \
  -d "{\"request_ref\":\"ALLOC-001\",\"allocations\":[{\"invoice_id\":\"$INVOICE_ID\",\"amount\":\"200\"}]}" \
  > /dev/null
# subledger-only bookkeeping (ADR-008 Decision 2) — posts nothing

curl -s $API/reports/trial-balance "${H[@]}"
```

```json
[
  {"account_id":"90a4baa0-...","account_code":"1000","account_name":"Cash","account_type":"ASSET","total_debit":"200.000000","total_credit":"0.000000"},
  {"account_id":"c5cf3413-...","account_code":"1100","account_name":"Accounts Receivable","account_type":"ASSET","total_debit":"200.000000","total_credit":"200.000000"},
  {"account_id":"cee6f514-...","account_code":"1300","account_name":"Inventory","account_type":"ASSET","total_debit":"0.000000","total_credit":"60.000000"},
  {"account_id":"27ae2975-...","account_code":"4000","account_name":"Revenue","account_type":"REVENUE","total_debit":"0.000000","total_credit":"200.000000"},
  {"account_id":"41e3480d-...","account_code":"5000","account_name":"COGS","account_type":"EXPENSE","total_debit":"60.000000","total_credit":"0.000000"}
]
```

Σdebit = Σcredit = 460 — the double-entry invariant every posting in this
project holds, proved for many Hypothesis-generated legal O2C sequences
(not just this one) by `tests/e2e/test_property_o2c_balances.py`.

## Testing

```bash
uv sync --group dev
uv run pytest -v
```

Tests run against **real PostgreSQL, never SQLite/mocks**. `tests/conftest.py`
picks the backend automatically: `testcontainers.postgres.PostgresContainer`
(a throwaway `postgres:16-alpine` container) if Docker is reachable —
this is what CI and most contributor machines use — otherwise an
embedded [`pgserver`](https://pypi.org/project/pgserver/) binary, still
real Postgres, just without a container runtime. Either path runs
`alembic upgrade head` against the ephemeral database first
(`tests/test_migrations.py` also asserts the resulting schema
explicitly), then truncates tenant-scoped tables between tests while
keeping seeded reference data (currencies/UoM) for the whole session.

Beyond ordinary CRUD/integration tests per module:

- **Cross-company isolation** (`tests/*/test_cross_company_isolation.py`):
  for every tenant-scoped endpoint, company A creates a resource and
  company B is proven unable to read/update/delete it (404) or see it in
  a list, plus a DB-layer test that the ORM filter itself fail-closes
  (raises) when no company context is bound.
- **Full O2C end-to-end** (`tests/e2e/test_o2c_end_to_end.py`): create →
  confirm → ship → invoice → pay → allocate against a real Postgres, with
  the exact ledger delta asserted at every single step (not just a final
  "it balances" check — a step posting the right amount to the wrong
  account, or vice versa, fails loudly).
- **Property-based invariants**
  (`tests/ledger/test_property_trial_balance.py`,
  `tests/e2e/test_property_o2c_balances.py`): Hypothesis-generated random
  (but always-legal, bounded-length) operation sequences, asserting the
  trial balance balances, the AR ↔ ledger 1100 control-account tie-out
  holds, and `on_hand >= 0` — not just for the one flow the E2E test
  walks, but for many legal interleavings of
  orders/shipments/invoices/payments.
- **Idempotent CLIs** (`tests/e2e/test_seed_idempotent.py`): `seed_demo`
  run twice against a real DB produces unchanged row counts, stock, and
  balances.

## Design Decisions

Every non-trivial decision below is written up as a full ADR under
[`docs/adr/`](docs/adr/README.md); ADR-003 through ADR-008 each passed a
real `codex` CLI architecture-consensus review *before* implementation
(not a self-review — see each ADR's "Consensus Revisions" section for
the finding-by-finding record). This section is a map, not a
substitute — follow the links for the actual reasoning.

| Topic | ADR |
|---|---|
| Modular monolith, not microservices | [ADR-001](docs/adr/ADR-001-modular-monolith.md) |
| Append-only fact tables + rebuildable summaries | [ADR-002](docs/adr/ADR-002-append-only-ledgers.md) |
| Posting engine (events → balanced journal entries) | [ADR-003](docs/adr/ADR-003-posting-engine.md) |
| Event bus | [ADR-004](docs/adr/ADR-004-event-bus.md) |
| Ledger/journal design | [ADR-005](docs/adr/ADR-005-ledger-journal-design.md) |
| Sales module, hook registry | [ADR-006](docs/adr/ADR-006-sales-and-hook-registry.md) |
| Inventory, shipping | [ADR-007](docs/adr/ADR-007-inventory-and-shipping.md) |
| Receivables — invoicing, payments, AR aging | [ADR-008](docs/adr/ADR-008-receivables.md) |

A few cross-cutting decisions that don't have their own numbered ADR but
matter to anyone reading the code:

**Multi-tenancy.** `app/core/tenancy.py` holds the active company id in a
`contextvars.ContextVar`, bound per-request by `AuthenticationMiddleware`
(`app/main.py`) from the authenticated bearer session (ADR-009). `app/core/db.py`
registers a SQLAlchemy `do_orm_execute` hook that injects
`with_loader_criteria` filtering to the active company for every ORM
**`SELECT`** touching a tenant-scoped table (it returns immediately for
any non-`SELECT` statement — writes are a separate mechanism, below), and
**raises if no context is bound** rather than silently returning zero or
all rows — fail-closed, not fail-open. This is why cross-company
`GET`/`PATCH`/`DELETE` by id resolve to `404`: every service function
that mutates a row by id fetches it first via exactly this
hook-filtered `SELECT` (e.g. `receivables.service.get_invoice`), so the
row is invisible before any write is even attempted — "belongs to
another company" and "doesn't exist" are indistinguishable by design.
Writes themselves are protected by convention, not this hook: every
`INSERT` stamps `company_id` from `require_current_company_id()`, never
from client input — `*Create` schemas have no `company_id` field at all,
so nothing can be smuggled into a request body. Access is guarded by
`platform.permissions` bearer sessions and granular permission enforcement.

**Money.** Every monetary column is `NUMERIC(20, 6)`; `exchange_rates.rate`
is `NUMERIC(20, 10)` with a `rate_date` (Phase 1 is TWD-only in practice —
`masterdata.schemas.CustomerCreate`/`CompanyCreate` enforce it — but the
multi-currency schema is already in place for Phase 3). Every money field
across every module is round-half-even at the Pydantic schema layer, not
left to implicit DB coercion: journal line amounts (`ledger.schemas`) and
receivables payment/allocation amounts (`receivables.schemas`) since Week
6 or earlier; `Customer.credit_limit`, `Product.list_price`/
`standard_cost` (`masterdata.schemas`), and
`SalesOrderLineCreate.unit_price` (`sales.schemas`) since Week 8 Decision
0. Each module owns its own private round-half-even helper
(`_round_half_even_6dp` in masterdata/sales/receivables;
`_round_half_even_to_6dp` in ledger) — never imported cross-module, per
the import-linter module-independence contract — rather than sharing one
from `app/core/`. Zero is always a legal value for `credit_limit`/
price/cost fields (never rejected), unlike `receivables.schemas`'s
`_round_and_reject_zero`, which is specific to payment/allocation amounts
where zero genuinely isn't legal.

**Ledger.** Double-entry, dual-currency lines, a `DEFERRED CONSTRAINT
TRIGGER` that rejects an unbalanced entry at commit, immutability via
`BEFORE UPDATE OR DELETE` triggers (corrections are reversal-only), and
gapless entry numbering under row-level locking. The trial balance is
always computed on the fly from `journal_lines` — there is no
maintained balance column on `accounts` (see ADR-002 for why that's a
deliberate per-domain choice, not an inconsistency with
`stock_summary`/`invoices.settled_amount`, which *are* maintained).

**Event bus.** `app/core/events.py` is synchronous and in-process:
`publish()` validates the payload against a registered schema, writes one
row to `outbox` in the caller's own transaction, then calls every
subscribed handler inline — a handler exception aborts the whole
transaction. Not every registered event has a subscriber:
`sales.order_confirmed` is registered (so `publish()`/replay still
validate its schema and it still gets an outbox row) but deliberately has
none (ADR-006 Decision 4) — nothing reacts to "confirmed" on its own.
`app/modules/ledger/posting.py` subscribes to `sales.goods_shipped` and
all four `receivables.*` events, turning each into a balanced journal
entry via a declarative rules table.
`app/modules/inventory/service.py`'s `handle_goods_shipped` ALSO
subscribes to `sales.goods_shipped`, to deduct stock — `goods_shipped` is
the only currently-registered event with two subscribers (every
`receivables.*` event has just the one, `ledger.posting`), and for it,
**subscription order is normative, not incidental** (ADR-007 Decision 1):
inventory's deduction handler runs before ledger's posting handler,
"move the goods, then account for them". Both still run inside the same
transaction as
the publish, so either order commits or rolls back atomically together;
the ordering only affects which handler's exception is attributed as the
failure reason on a bad `ship`. `app.cli.replay_outbox` re-dispatches any
`outbox` row still `dispatched_at IS NULL` directly to its handler(s).

**Plugins.** In-process, no sandbox — same trust model as Odoo (see
`SECURITY.md`): a plugin is code an administrator chose to install, not
untrusted third-party input. `app/plugins/credit_limit.py` is Phase 1's
one demonstration plugin, vetoing `sales.confirm_order` via the
`sales.order.validate_confirm` hook when a customer's exposure (open
invoices + uninvoiced confirmed orders) would exceed their credit limit
(`credit_limit == 0` means "do not check"). A real plugin *loader*
(dynamic discovery/registration) is Phase 2 scope.

## Project layout

```
app/
├── core/            # settings, async DB session mgmt, tenancy context/filter,
│                     # event bus, hook registry, exceptions, advisory locking
├── modules/
│   ├── masterdata/   # companies, customers, products, UoM, accounts, currencies
│   ├── ledger/       # journal entries, accounting periods, trial balance, posting engine
│   ├── sales/        # orders: draft → confirmed → shipped, hook registry consumer
│   ├── inventory/    # stock_moves (append-only) + stock_summary, goods_shipped subscriber
│   └── receivables/  # invoices, payments, allocations, AR aging
├── plugins/          # credit_limit.py — the one hard-wired demonstration plugin
├── cli/              # seed_demo, demo_o2c, replay_outbox, rebuild_ar_balances, rebuild_stock_summary
└── main.py           # FastAPI app, tenancy middleware, exception handlers, router mounting
alembic/               # async migrations; single source of truth is app.core.settings
docs/
├── adr/               # architecture decision records, incl. WEEK7-phase1-hardening-brief.md
│                       # (see the Design Decisions table above)
├── open-erp-master-plan.md
└── mini-erp-architecture.md
tests/
├── conftest.py        # real-Postgres fixtures (testcontainers, or pgserver fallback)
├── e2e/                # full O2C flow, property-based invariants, idempotent-seed tests
└── <module>/           # CRUD + cross-company isolation integration tests, one dir per module
```

## Engineering quality gates (CI)

`ruff check` (lint) → `ruff format --check` → `mypy` → `import-linter`
(module independence contracts — business modules may not import each
other's `models`/`service`, core may not import business modules or
plugins) → `pytest` against a real Postgres service container. Same
sequence as `make check`. See `.github/workflows/ci.yml`.

## License

Apache-2.0 (see `LICENSE`) — chosen for enterprise adoption friendliness.
