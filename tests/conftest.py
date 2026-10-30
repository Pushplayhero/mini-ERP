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
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.authorization import ActorContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    for _ in range(2):
        try:
            subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=True)
            return True
        except Exception:
            continue
    return False


def _start_testcontainers_postgres() -> tuple[str, object]:
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="asyncpg")
    container.start()
    dsn = container.get_connection_url()
    return dsn, container


def _start_pgserver_postgres() -> tuple[str, object]:
    import asyncio

    import asyncpg
    import pgserver

    pgdata = Path(tempfile.mkdtemp(prefix="mini_erp_pgtest_"))
    server = pgserver.get_server(pgdata, cleanup_mode="delete")  # type: ignore[attr-defined]
    info = server.get_postmaster_info()
    connect_host = info.hostname if info.hostname is not None else str(info.socket_dir)

    async def _init_db() -> None:
        conn = await asyncpg.connect(
            user="postgres",
            host=connect_host,
            port=info.port,
            database="postgres",
        )
        try:
            await conn.execute("CREATE DATABASE mini_erp_test;")
        finally:
            await conn.close()

    asyncio.run(_init_db())
    if info.hostname is not None:
        dsn = f"postgresql+asyncpg://postgres:@{info.hostname}:{info.port}/mini_erp_test"
    else:
        dsn = f"postgresql+asyncpg://postgres:@:{info.port}/mini_erp_test?host={info.socket_dir}"
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
    from tests._auth_helpers import clear_company_token_cache

    clear_company_token_cache()
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
                "customers, products, accounts, outbox, companies, "
                # ADR-009 / WEEK9 Slice B: `users` is real per-test data (an
                # email a test creates), not a shared catalog like
                # `roles`/`permissions`/`role_permissions` (those stay
                # untruncated below, same treatment as `currencies`/`uom`
                # above — code-seeded, read-mostly, no test-isolation
                # concern). `sessions`/`user_company_roles` need no explicit
                # listing: both FK to `companies` (CASCADE), so truncating
                # `companies` already sweeps them.
                "users "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


ALL_ADMIN_PERMISSION_CODES: frozenset[str] = frozenset(
    {
        "session.manage",
        "masterdata.company.create",
        "masterdata.company.read",
        "masterdata.customer.create",
        "masterdata.customer.read",
        "masterdata.customer.update",
        "masterdata.customer.delete",
        "masterdata.product.create",
        "masterdata.product.read",
        "masterdata.product.update",
        "masterdata.product.delete",
        "masterdata.account.create",
        "masterdata.account.read",
        "masterdata.account.update",
        "masterdata.account.delete",
        "masterdata.uom.create",
        "masterdata.uom_conversion.create",
        "masterdata.uom_conversion.read",
        "masterdata.currency.create",
        "masterdata.exchange_rate.create",
        "masterdata.exchange_rate.read",
        "sales.order.create",
        "sales.order.read",
        "sales.order.update",
        "sales.order.confirm",
        "sales.order.cancel",
        "sales.order.ship",
        "ledger.period.create",
        "ledger.period.read",
        "ledger.period.close",
        "ledger.journal_entry.create",
        "ledger.journal_entry.read",
        "ledger.journal_entry.reverse",
        "ledger.report.read",
        "inventory.adjustment.create",
        "inventory.summary.read",
        "inventory.move.read",
        "receivables.invoice.create",
        "receivables.invoice.read",
        "receivables.invoice.void",
        "receivables.payment.create",
        "receivables.payment.read",
        "receivables.payment.void",
        "receivables.payment.allocate",
        "receivables.report.read",
    }
)

TEST_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    permission_codes=ALL_ADMIN_PERMISSION_CODES,
    is_system_actor=True,
)


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac


@pytest_asyncio.fixture
async def anchor_admin(db_session: object) -> None:
    """Rule 10b bootstrap an anchor admin user/company/role in test DB."""
    from app.cli.bootstrap_admin import bootstrap_admin

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="MAIN",
        company_name="Main Company",
    )


def company_headers(company_id: uuid.UUID) -> dict[str, str]:
    """Build Authorization bearer headers for tests (fail-closed per ADR-009)."""
    from tests._auth_helpers import get_company_token

    token = get_company_token(company_id)
    if token is None:
        raise RuntimeError(
            f"No test auth token found for company {company_id}. "
            "Every HTTP test must authenticate via a real session token (ADR-009 Cutover)."
        )
    return {"Authorization": f"Bearer {token}"}
