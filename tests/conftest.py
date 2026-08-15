"""Shared pytest fixtures — real PostgreSQL only, never SQLite/mocks.

Backend selection for the ephemeral test database:

1. **testcontainers** (`testcontainers.postgres.PostgresContainer`) — used
   whenever a Docker daemon is reachable. This is the primary, brief-mandated
   path and is what runs in CI (GitHub Actions has Docker available) and on
   any contributor's machine with Docker installed.
2. **pgserver** (embedded, no-root, no-Docker Postgres binary) — automatic
   fallback used only when Docker is unavailable, e.g. this project's own
   sandboxed development/verification environment. It still runs a real
   PostgreSQL 16 server, just without a container runtime; it is NOT SQLite
   and NOT a mock. See README "Testing" section for details.

Either way, the same `alembic upgrade head` runs against the ephemeral
database once per test session before any test executes (`_run_migrations`
below), so the migration itself is exercised by the test suite (see also
`tests/test_migrations.py` for an explicit, isolated schema assertion).

Event-loop note: pytest-asyncio (function-scoped loop, the default) gives
each test its own event loop. asyncpg connections/pools created in one
loop cannot be reused from another, so the SQLAlchemy `AsyncEngine` (both
the app's own lazily-created singleton in `app.core.db` and any engine used
directly by fixtures) is deliberately torn down and recreated *every test
function* — see `db_engine` below. The Postgres *server process* itself
(container or pgserver) stays up for the whole session; only the
client-side engine/pool is per-test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
    except Exception:
        return False
    return True


def _start_testcontainers_postgres() -> tuple[str, object]:
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="asyncpg")
    container.start()
    dsn = container.get_connection_url()
    return dsn, container


def _start_pgserver_postgres() -> tuple[str, object]:
    import pgserver

    pgdata = Path(tempfile.mkdtemp(prefix="mini_erp_pgtest_"))
    server = pgserver.get_server(pgdata, cleanup_mode="delete")
    server.psql("CREATE DATABASE mini_erp_test;")
    # Windows fix: pgserver's own `get_postmaster_info()` reports a real
    # TCP `hostname`/`port` (127.0.0.1, an ephemeral port) — the previous
    # version of this function built the DSN with `host=<pgdata directory
    # path>` instead, which only works where asyncpg can treat that value
    # as a Unix-domain-socket directory. Windows has no such thing, so
    # asyncpg fell through to plain DNS resolution on the literal path
    # string and failed with `getaddrinfo failed` — this is what HANDOFF.md
    # previously documented as "the pgserver fallback path does NOT work on
    # Windows". It does; the DSN construction was just wrong. `psql()`
    # above already proves TCP works (it uses the same postmaster).
    info = server.get_postmaster_info()
    dsn = f"postgresql+asyncpg://postgres:@{info.hostname}:{info.port}/mini_erp_test"
    return dsn, server


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    if _docker_available():
        dsn, backend = _start_testcontainers_postgres()
        backend_kind = "testcontainers"
    else:
        dsn, backend = _start_pgserver_postgres()
        backend_kind = "pgserver (Docker unavailable — embedded PostgreSQL fallback)"

    print(f"\n[tests/conftest] Postgres backend: {backend_kind}\n[tests/conftest] DSN: {dsn}")

    # Wire the app + alembic to this ephemeral database. Must happen before
    # any code calls `get_settings()`/`get_engine()` for the first time.
    os.environ["DATABASE_URL"] = dsn

    from app.core.settings import get_settings

    get_settings.cache_clear()

    yield dsn

    if hasattr(backend, "stop"):
        backend.stop()


@pytest.fixture(scope="session", autouse=True)
def _run_migrations(postgres_dsn: str) -> None:
    """Apply `alembic upgrade head` against the ephemeral test database.

    This both prepares the schema for the rest of the suite AND is itself
    the verification that migrations run cleanly (see also
    `tests/test_migrations.py` for an explicit, isolated assertion).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(autouse=True)
async def db_engine(_run_migrations: None) -> AsyncIterator[AsyncEngine]:
    """The app's own engine singleton, reset every test to match its event loop.

    Using `app.core.db.get_engine()` here (rather than a fixture-local
    `create_async_engine` call) means fixtures and the FastAPI app under
    test always share the exact same engine/pool for a given test.
    """
    from app.core import db as core_db

    await core_db.dispose_engine()
    engine = core_db.get_engine()
    yield engine
    await core_db.dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _seed_reference_data(db_engine: AsyncEngine) -> None:
    """Seed global reference data (currencies, UoM) needed by most tests.

    Runs every test (cheap, idempotent via `ON CONFLICT DO NOTHING`) rather
    than once per session, because the engine itself is recreated per test
    (see `db_engine`) — this keeps fixture dependencies simple and avoids
    pytest-asyncio scope-mismatch pitfalls entirely.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO currencies (code, name, decimal_places, is_active) "
                "VALUES ('TWD', 'New Taiwan Dollar', 0, true), "
                "('USD', 'US Dollar', 2, true) "
                "ON CONFLICT (code) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO uom (id, code, name, is_active) "
                "VALUES (:id1, 'EA', 'Each', true), (:id2, 'KG', 'Kilogram', true) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"id1": str(uuid.uuid4()), "id2": str(uuid.uuid4())},
        )


@pytest_asyncio.fixture(autouse=True)
async def _clean_tenant_tables(
    db_engine: AsyncEngine, _seed_reference_data: None
) -> AsyncIterator[None]:
    """Truncate tenant-scoped + company tables before every test.

    Reference data (currencies/uom) is seeded fresh each test (see above)
    and never truncated here; it has no test-isolation concerns since it's
    global, read-mostly data shared by design (master-plan §10.2 only
    requires isolation of tenant-scoped rows).
    """
    async with db_engine.begin() as conn:
        # journal_entries/journal_lines carry `BEFORE UPDATE OR DELETE`
        # triggers (ADR-005 Decision 3) that unconditionally raise — but
        # those only fire for row-level UPDATE/DELETE statements. TRUNCATE
        # is neither (it's its own DDL-like statement with its own trigger
        # class, ON TRUNCATE, which nothing here defines), so it still
        # works for per-test cleanup despite the tables being immutable
        # from the application's point of view.
        await conn.execute(
            text(
                "TRUNCATE TABLE journal_lines, journal_entries, ledger_sequences, "
                "accounting_periods, stock_moves, stock_summary, "
                "payment_allocations, payment_allocation_commands, payments, "
                "invoices, receivables_sequences, "
                "sales_order_lines, sales_orders, sales_sequences, "
                "customers, products, accounts, outbox, companies "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def company_headers(company_id: uuid.UUID) -> dict[str, str]:
    """Build the `X-Company-Id` header a real deployment's auth layer would set."""
    return {"X-Company-Id": str(company_id)}
