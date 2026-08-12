# mini-erp

> A minimal, correctness-obsessed open-source ERP kernel. Phase 1 ("Kernel")
> of a longer-term plan to build an open, API-first, plugin-extensible ERP
> for Taiwanese SMEs — see `docs/open-erp-master-plan.md` and
> `docs/mini-erp-architecture.md` for the full picture.

**This repository state = Phase 1 / Week 1.** Scope: repo skeleton, CI,
Docker Compose, Alembic, and a complete `masterdata` module (companies,
customers, products, UoM, accounts, currencies/exchange rates) with the
multi-company isolation mechanism the rest of Phase 1 builds on.

## Non-Goals (this week)

`sales`, `inventory`, `receivables`, `ledger` business logic; the posting
engine; the in-process event bus; the outbox dispatcher; plugin loader;
workflow/RBAC; frontend. These are empty module shells with a `README.md`
pointing at when they land — see `app/modules/*/README.md`.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Pydantic v2 ·
PostgreSQL 16 · Alembic · [uv](https://docs.astral.sh/uv/) for packaging.

**Why uv over Poetry**: a single tool for interpreter management, venvs,
dependency resolution/locking, and running scripts (`uv run ...`), with a
resolver that's fast enough to not think about; it's also what `pyproject.toml`
`[dependency-groups]` (PEP 735) is designed around. No functional need for
Poetry's plugin ecosystem here.

## Quick start

### Option A — Docker Compose (recommended)

```bash
cp .env.example .env
docker compose up --build
# app runs migrations on startup, then serves on http://localhost:8000
curl http://localhost:8000/health
open http://localhost:8000/docs   # interactive OpenAPI UI
```

### Option B — running on your host

```bash
uv sync --group dev
cp .env.example .env   # edit DATABASE_URL to point at your own Postgres
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

### Try it: create a company, then a customer

Every tenant-scoped write/read requires an `X-Company-Id` header — see
"Design Decisions" below for why there's no `company_id` in request bodies.

```bash
COMPANY_ID=$(curl -s -X POST localhost:8000/api/v1/companies \
  -H 'Content-Type: application/json' \
  -d '{"code":"ACME","name":"Acme Corp","functional_currency_code":"TWD"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST localhost:8000/api/v1/currencies \
  -H 'Content-Type: application/json' \
  -d '{"code":"TWD","name":"New Taiwan Dollar","decimal_places":0}'

curl -s -X POST localhost:8000/api/v1/customers \
  -H "X-Company-Id: $COMPANY_ID" -H 'Content-Type: application/json' \
  -d '{"code":"CUST-001","name":"Test Customer","currency_code":"TWD"}'
```

## Testing

```bash
uv sync --group dev
uv run pytest -v
```

Tests run against **real PostgreSQL, never SQLite**. `tests/conftest.py`
picks the backend automatically:

1. If a Docker daemon is reachable, `testcontainers.postgres.PostgresContainer`
   starts a throwaway `postgres:16-alpine` container per test session (this
   is what runs on any machine with Docker, and in CI).
2. Otherwise it falls back to [`pgserver`](https://pypi.org/project/pgserver/),
   an embedded, no-root PostgreSQL 16 binary — used only in this project's
   own sandboxed development environment, which has no Docker daemon. Still
   real Postgres, just without a container runtime.

Either path runs `alembic upgrade head` against the ephemeral database
before the suite executes (`tests/test_migrations.py` also asserts the
resulting schema explicitly), then truncates tenant-scoped tables between
tests (`tests/conftest.py::_clean_tenant_tables`) while keeping seeded
reference data (currencies/UoM) for the whole session.

**Cross-company isolation** (master-plan §10.2's mandatory suite) lives in
`tests/masterdata/test_cross_company_isolation.py`: for each tenant-scoped
endpoint, company A creates a resource and company B is proven unable to
read/update/delete it (404) or see it in a list — plus a DB-layer test that
the ORM filter itself raises (fail-closed) when no company context is bound.

## Design Decisions

**Multi-company isolation (master-plan §10.2).** `app/core/tenancy.py`
holds the active company id in a `contextvars.ContextVar`, bound per-request
by `TenancyMiddleware` (`app/main.py`) from a trusted `X-Company-Id` header.
`app/core/db.py` registers a SQLAlchemy `do_orm_execute` event that injects
`with_loader_criteria` filtering to the active company for any query
touching a `TenantScopedMixin` subclass — and **raises `TenancyContextError`
if no context is bound**, rather than silently returning zero or all rows.
This is why cross-company `GET`/`PATCH`/`DELETE` by id resolve to `404`: the
row is filtered out at the ORM layer before the router even sees it, so
"belongs to another company" and "doesn't exist" are indistinguishable by
design. Postgres Row-Level Security is deferred to Phase 2 as defense in
depth, not relied on here.

Week 1 has no auth/RBAC (that's Phase 2 — `platform.permissions`), so the
`X-Company-Id` header is a deliberate, documented stand-in for a verified
JWT/session claim a real auth layer will set later. It's read from a
*header*, never from a request body field, which is what actually matters:
`CustomerCreate`/`ProductCreate`/`AccountCreate` schemas have no
`company_id` field at all, so a client cannot smuggle one in even today —
see `tests/masterdata/test_customers_api.py::test_client_supplied_company_id_in_body_is_ignored`.

**Global vs. tenant-scoped master data.** `Customer`, `Product`, `Account`
inherit `TenantScopedMixin` (per master-plan §10.2, isolated per company).
`Uom`, `Currency`, `ExchangeRate` do not — a kilogram and ISO 4217 `TWD` are
not company-specific concepts, so they're shared reference data seeded once
and readable by everyone. `Company` is the tenant root and is therefore
never itself tenant-scoped.

**Amount/quantity precision (master-plan §10.1).** Every monetary or
quantity column is `NUMERIC(20, 6)`; `exchange_rates.rate` is
`NUMERIC(20, 10)` (rates need more precision than the amounts they scale).
`currencies.decimal_places` drives *display* rounding — a UI concern, not a
storage one — while `exchange_rates.rate_date` lets future transaction
tables snapshot the rate at post time instead of re-querying history.

**Outbox table, write-only (master-plan §10.4).** `outbox` is created this
week with exactly the columns master-plan §10.4 specifies
(`id, event_type, payload, occurred_at, dispatched_at, attempts`). Nothing
writes to it yet and there's no dispatcher — masterdata has no transactional
postings of its own to publish (see `app/modules/masterdata/events.py`).
The in-process event bus that will populate it lands in Week 3 alongside the
posting engine, per `mini-erp-architecture.md` §7.

**Modular monolith boundaries.** `app/core` must never import
`app.modules.*`; business modules must never import each other's
`models`/`service`. Both rules are CI-enforced via `import-linter`
(`pyproject.toml`'s `[tool.importlinter]` section) — a violation fails the
build, not just a lint warning.

## Project layout

```
app/
├── core/               # settings, async DB session mgmt, tenancy context/filter, exceptions
├── modules/
│   ├── masterdata/     # companies, customers, products, UoM, accounts, currencies — DONE (Week 1)
│   ├── sales/          # empty shell — Week 4
│   ├── inventory/      # empty shell — Week 5
│   ├── receivables/    # empty shell — Week 6
│   └── ledger/         # empty shell — Week 2/3
└── main.py             # FastAPI app, tenancy middleware, exception handlers, router mounting
alembic/                # async migrations; single source of truth is app.core.settings
tests/
├── conftest.py          # real-Postgres fixtures (testcontainers, or pgserver fallback)
├── test_smoke.py
├── test_migrations.py   # asserts `alembic upgrade head` produces the expected schema
└── masterdata/          # CRUD + cross-company isolation integration tests
```

## Engineering quality gates (CI)

`ruff check` (lint) → `ruff format --check` → `mypy --strict` →
`import-linter` (module boundaries) → `pytest` against a real Postgres
service container. See `.github/workflows/ci.yml`.

## License

Apache-2.0 (see `LICENSE`) — chosen per master-plan §5 for enterprise
adoption friendliness.
