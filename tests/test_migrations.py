"""Explicit assertion that `alembic upgrade head` produced the expected schema.

The session-scoped `_run_migrations` autouse fixture in conftest.py already
runs `alembic upgrade head` before any test executes (so if it failed, the
whole suite would fail at collection/setup time) — this test additionally
asserts the resulting schema shape, so a silent "upgrade ran but created
nothing" failure mode is also caught.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "companies",
    "customers",
    "products",
    "accounts",
    "uom",
    "uom_conversions",
    "currencies",
    "exchange_rates",
    "outbox",
    # ADR-007 / migration 0005 (diff-review fix: these were missing from
    # this assertion even though the migration that creates them is the one
    # CURRENT_ISSUES.md flagged as under-tested).
    "stock_moves",
    "stock_summary",
    # ADR-008 / migration 0008.
    "invoices",
    "payments",
    "payment_allocation_commands",
    "payment_allocations",
    "receivables_sequences",
    "alembic_version",
}


@pytest.mark.asyncio
async def test_upgrade_head_creates_expected_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )

    missing = EXPECTED_TABLES - table_names
    assert not missing, f"alembic upgrade head did not create expected tables: {missing}"


@pytest.mark.asyncio
async def test_money_and_quantity_columns_are_numeric_20_6(db_engine: AsyncEngine) -> None:
    """master-plan §10.1: amount/quantity columns must be NUMERIC(20, 6)."""

    def _inspect(sync_conn):  # type: ignore[no-untyped-def]
        inspector = inspect(sync_conn)
        cols = {c["name"]: c for c in inspector.get_columns("customers")}
        return cols["credit_limit"]["type"]

    async with db_engine.connect() as conn:
        numeric_type = await conn.run_sync(_inspect)

    assert numeric_type.precision == 20
    assert numeric_type.scale == 6


@pytest.mark.asyncio
async def test_migration_0005_schema_matches_adr_007(db_engine: AsyncEngine) -> None:
    """Diff-review test-coverage fix: assert the specific objects ADR-007 /
    migration 0005 commits to, not just "the tables exist" — the unique
    partial index (Decision 4), both CHECK constraints (Decision 2's
    `on_hand >= 0` backstop and the diff-review fix's `standard_cost >= 0`
    backstop), the new `products.standard_cost` column, and the `SHIPPED`
    enum value (order lifecycle update).
    """

    def _inspect(sync_conn):  # type: ignore[no-untyped-def]
        inspector = inspect(sync_conn)
        product_cols = {c["name"] for c in inspector.get_columns("products")}
        product_checks = {c["name"] for c in inspector.get_check_constraints("products")}
        stock_moves_checks = {c["name"] for c in inspector.get_check_constraints("stock_moves")}
        stock_summary_checks = {c["name"] for c in inspector.get_check_constraints("stock_summary")}
        stock_moves_indexes = {ix["name"]: ix for ix in inspector.get_indexes("stock_moves")}
        return (
            product_cols,
            product_checks,
            stock_moves_checks,
            stock_summary_checks,
            stock_moves_indexes,
        )

    async with db_engine.connect() as conn:
        (
            product_cols,
            product_checks,
            stock_moves_checks,
            stock_summary_checks,
            stock_moves_indexes,
        ) = await conn.run_sync(_inspect)

        enum_values = (
            (
                await conn.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum "
                        "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
                        "WHERE pg_type.typname = 'sales_order_status'"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert "standard_cost" in product_cols
    assert "ck_products_standard_cost_nonneg" in product_checks
    assert "ck_stock_moves_qty_delta_nonzero" in stock_moves_checks
    assert "ck_stock_summary_on_hand_nonneg" in stock_summary_checks
    assert "uq_stock_moves_source" in stock_moves_indexes
    assert stock_moves_indexes["uq_stock_moves_source"]["unique"] is True
    assert "SHIPPED" in enum_values


@pytest.mark.asyncio
async def test_migration_0005_downgrade_upgrade_cycle_is_safe(db_engine: AsyncEngine) -> None:
    """Diff-review test-coverage fix (R3, flagged as missing in
    CURRENT_ISSUES.md item 2): prove `alembic upgrade head` ->
    `downgrade -1` -> `upgrade head` again — the exact operational sequence
    R3's guarded `ALTER TYPE ... ADD VALUE 'SHIPPED'` exists to survive.
    `downgrade()` never removes the enum label (Postgres can't, without a
    disruptive type rebuild — see the migration's own `downgrade()`
    docstring), so re-running the guarded `ADD VALUE` against a type that
    already carries the label must not raise `DuplicateObjectError`.
    """
    from alembic import command
    from alembic.config import Config

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    # `_clean_tenant_tables` (conftest, autouse) has already truncated every
    # tenant-scoped table for this test, so the tables migration 0005 drops
    # on downgrade are empty — no FK/data-loss concerns from the round-trip.
    #
    # Dispose the pooled AsyncEngine before and after touching schema
    # directly: asyncpg caches per-connection type/statement info, and a
    # pooled connection that survived across the DDL round-trip could hold
    # a stale OID for the recreated `stock_move_type` enum.
    #
    # `command.downgrade`/`command.upgrade` are synchronous and internally
    # call `asyncio.run(...)` (see `alembic/env.py`'s `run_migrations_online`)
    # — that raises if invoked from inside an already-running loop, which
    # this test function is (pytest-asyncio). Run them in a worker thread,
    # which has no running loop of its own, exactly like `_run_migrations`
    # in conftest.py gets away with calling them directly only because it
    # is a *sync* session-scoped fixture that runs before any test's loop
    # exists.
    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, "-1")
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()

    def _inspect(sync_conn):  # type: ignore[no-untyped-def]
        return set(inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        table_names = await conn.run_sync(_inspect)
        enum_values = (
            (
                await conn.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum "
                        "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
                        "WHERE pg_type.typname = 'sales_order_status'"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert {"stock_moves", "stock_summary"} <= table_names
    assert "SHIPPED" in enum_values


@pytest.mark.asyncio
async def test_migration_0008_schema_matches_adr_008(db_engine: AsyncEngine) -> None:
    """Assert the specific objects ADR-008 / migration 0008 commits to:

    `sales_orders.shipped_at` + its state-conditional CHECK, `accounts
    .is_control`, `customers.payment_terms_days` + its bounded CHECK, and
    the `uq_invoices_order_live` partial unique index (Decision 1).
    """

    def _inspect(sync_conn):  # type: ignore[no-untyped-def]
        inspector = inspect(sync_conn)
        sales_order_cols = {c["name"] for c in inspector.get_columns("sales_orders")}
        sales_order_checks = {c["name"] for c in inspector.get_check_constraints("sales_orders")}
        account_cols = {c["name"] for c in inspector.get_columns("accounts")}
        customer_cols = {c["name"] for c in inspector.get_columns("customers")}
        customer_checks = {c["name"] for c in inspector.get_check_constraints("customers")}
        invoice_indexes = {ix["name"]: ix for ix in inspector.get_indexes("invoices")}
        return (
            sales_order_cols,
            sales_order_checks,
            account_cols,
            customer_cols,
            customer_checks,
            invoice_indexes,
        )

    async with db_engine.connect() as conn:
        (
            sales_order_cols,
            sales_order_checks,
            account_cols,
            customer_cols,
            customer_checks,
            invoice_indexes,
        ) = await conn.run_sync(_inspect)

    assert "shipped_at" in sales_order_cols
    assert "ck_sales_orders_shipped_has_shipped_at" in sales_order_checks
    assert "is_control" in account_cols
    assert "payment_terms_days" in customer_cols
    assert "ck_customers_payment_terms_days_range" in customer_checks
    assert "uq_invoices_order_live" in invoice_indexes
    assert invoice_indexes["uq_invoices_order_live"]["unique"] is True


@pytest.mark.asyncio
async def test_migration_0008_shipped_at_backfill_on_populated_database(
    db_engine: AsyncEngine,
) -> None:
    """R17's populated-upgrade test: a `SHIPPED` order that predates

    migration 0008 (no `shipped_at` column at all) must have `shipped_at`
    correctly backfilled from its own `sales.goods_shipped` outbox payload
    when the migration runs — not just work against a from-empty database
    (which every other test in this suite already exercises via the
    session-scoped `_run_migrations` fixture).

    Downgrades to before 0008 (dropping `shipped_at` and the receivables
    tables entirely), inserts a company/customer/product/SHIPPED-order/
    outbox-row by raw SQL (the schema at that point has no ORM mapping for
    the pre-0008 shape), then upgrades back to head and asserts the
    backfill produced the exact `shipped_at` the outbox payload carried.
    """
    from alembic import command
    from alembic.config import Config

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, "-1")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()

    company_id = "11111111-1111-1111-1111-111111111111"
    customer_id = "22222222-2222-2222-2222-222222222222"
    product_id = "33333333-3333-3333-3333-333333333333"
    order_id = "44444444-4444-4444-4444-444444444444"
    outbox_id = "55555555-5555-5555-5555-555555555555"
    shipped_at_iso = "2026-08-01T03:04:05+00:00"

    async with engine.begin() as conn:
        uom_row = (await conn.execute(text("SELECT id FROM uom WHERE code = 'EA' LIMIT 1"))).first()
        assert uom_row is not None, "expected the autouse _seed_reference_data fixture to have run"
        uom_id = str(uom_row[0])

        await conn.execute(
            text(
                "INSERT INTO companies (id, code, name, functional_currency_code, is_active) "
                "VALUES (:id, 'BKFCO', 'Backfill Co', 'TWD', true)"
            ),
            {"id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO customers (id, code, name, currency_code, is_active, company_id) "
                "VALUES (:id, 'BKFCUST', 'Backfill Customer', 'TWD', true, :company_id)"
            ),
            {"id": customer_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, sku, name, uom_id, is_active, company_id) "
                "VALUES (:id, 'BKF-SKU', 'Backfill Widget', :uom_id, true, :company_id)"
            ),
            {"id": product_id, "uom_id": uom_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO sales_orders "
                "(id, company_id, order_no, customer_id, status, currency_code, "
                "snapshot_customer_code, snapshot_customer_name) "
                "VALUES (:id, :company_id, 'SO-BACKFILL-1', :customer_id, 'SHIPPED', 'TWD', "
                "'BKFCUST', 'Backfill Customer')"
            ),
            {"id": order_id, "company_id": company_id, "customer_id": customer_id},
        )
        await conn.execute(
            text(
                "INSERT INTO outbox (id, event_type, payload) "
                "VALUES (:id, 'sales.goods_shipped', "
                "CAST(:payload AS JSONB))"
            ),
            {
                "id": outbox_id,
                "payload": (
                    f'{{"company_id": "{company_id}", "source_id": "{order_id}", '
                    f'"order_no": "SO-BACKFILL-1", "lines": [], "total_cost": "0", '
                    f'"shipped_at": "{shipped_at_iso}"}}'
                ),
            },
        )

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT shipped_at FROM sales_orders WHERE id = :id"), {"id": order_id}
        )
        row = result.first()

    assert row is not None
    assert (
        row[0] is not None
    ), "migration 0008 must backfill shipped_at for a pre-existing SHIPPED order"
    assert row[0].isoformat() == "2026-08-01T03:04:05+00:00"


@pytest.mark.asyncio
async def test_migration_0008_shipped_at_backfill_rejects_zero_matching_outbox_events(
    db_engine: AsyncEngine,
) -> None:
    """Codex diff review (2026-08-15, second pass, finding 4) regression guard.

    The first fix-verification pass confirmed the preflight's SQL is
    correct for both the zero-match and multi-match cases (`LEFT JOIN ...
    HAVING count(ob.id) <> 1`), but pointed out test coverage only actually
    exercised the multi-match half — leaving a `LEFT JOIN` -> `INNER JOIN`
    regression free to pass every test in the suite (an `INNER JOIN` would
    just silently drop the zero-match order out of the preflight result
    entirely, since there is no source row to join against). This test
    forces a SHIPPED order with NO matching `sales.goods_shipped` outbox
    row — plus one *irrelevant* outbox row (right event_type, wrong
    `source_id`) to prove the `ON` clause's `source_id` match is doing real
    filtering, not just "any goods_shipped event in the table counts" —
    and asserts the migration's *preflight* raises (matching its own
    message), not just the redundant post-backfill backstop.
    """
    from alembic import command
    from alembic.config import Config

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, "-1")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()

    company_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    customer_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    product_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    order_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    other_order_id = "12121212-1212-1212-1212-121212121212"
    decoy_outbox_id = "13131313-1313-1313-1313-131313131313"
    real_outbox_id = "14141414-1414-1414-1414-141414141414"

    async with engine.begin() as conn:
        uom_row = (await conn.execute(text("SELECT id FROM uom WHERE code = 'EA' LIMIT 1"))).first()
        assert uom_row is not None, "expected the autouse _seed_reference_data fixture to have run"
        uom_id = str(uom_row[0])

        await conn.execute(
            text(
                "INSERT INTO companies (id, code, name, functional_currency_code, is_active) "
                "VALUES (:id, 'ZEROCO', 'Zero-Match Backfill Co', 'TWD', true)"
            ),
            {"id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO customers (id, code, name, currency_code, is_active, company_id) "
                "VALUES (:id, 'ZEROCUST', 'Zero-Match Customer', 'TWD', true, :company_id)"
            ),
            {"id": customer_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, sku, name, uom_id, is_active, company_id) "
                "VALUES (:id, 'ZERO-SKU', 'Zero-Match Widget', :uom_id, true, :company_id)"
            ),
            {"id": product_id, "uom_id": uom_id, "company_id": company_id},
        )
        # The SHIPPED order under test — deliberately given NO matching
        # outbox row.
        await conn.execute(
            text(
                "INSERT INTO sales_orders "
                "(id, company_id, order_no, customer_id, status, currency_code, "
                "snapshot_customer_code, snapshot_customer_name) "
                "VALUES (:id, :company_id, 'SO-ZEROBACKFILL-1', :customer_id, 'SHIPPED', 'TWD', "
                "'ZEROCUST', 'Zero-Match Customer')"
            ),
            {"id": order_id, "company_id": company_id, "customer_id": customer_id},
        )
        # Decoy: a real `sales.goods_shipped` event, but for a DIFFERENT
        # order — proves the preflight's `ON ... source_id = so.id::text`
        # actually filters, rather than any goods_shipped row satisfying
        # the join.
        await conn.execute(
            text(
                "INSERT INTO outbox (id, event_type, payload) "
                "VALUES (:id, 'sales.goods_shipped', CAST(:payload AS JSONB))"
            ),
            {
                "id": decoy_outbox_id,
                "payload": (
                    f'{{"company_id": "{company_id}", "source_id": "{other_order_id}", '
                    f'"order_no": "SO-UNRELATED", "lines": [], "total_cost": "0", '
                    f'"shipped_at": "2026-08-01T00:00:00+00:00"}}'
                ),
            },
        )

    await core_db.dispose_engine()
    try:
        with pytest.raises(Exception, match="do not have exactly one matching"):
            await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    # Restore: insert the real matching outbox row so a clean `upgrade
    # head` can proceed, leaving the shared `db_engine` at head for every
    # other test in the suite.
    engine = core_db.get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox (id, event_type, payload) "
                "VALUES (:id, 'sales.goods_shipped', CAST(:payload AS JSONB))"
            ),
            {
                "id": real_outbox_id,
                "payload": (
                    f'{{"company_id": "{company_id}", "source_id": "{order_id}", '
                    f'"order_no": "SO-ZEROBACKFILL-1", "lines": [], "total_cost": "0", '
                    f'"shipped_at": "2026-08-01T00:00:00+00:00"}}'
                ),
            },
        )

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()


@pytest.mark.asyncio
async def test_migration_0008_shipped_at_backfill_rejects_duplicate_outbox_events(
    db_engine: AsyncEngine,
) -> None:
    """Codex diff review (2026-08-15, finding 4) regression guard.

    `UPDATE ... FROM` join semantics are unspecified when a target row
    matches more than one source row — if a shipped order ever had TWO
    matching `sales.goods_shipped` outbox payloads (a violation of the
    idempotent-posting invariant this backfill relies on), the pre-fix
    migration would silently pick one of them instead of raising: its only
    check was "is shipped_at still NULL after the UPDATE?", which a
    multi-match row would trivially pass. This test forces that scenario
    (two outbox rows for the same order, different `shipped_at` values) and
    asserts the migration's preflight check now raises *before* writing
    anything, rather than silently choosing a value.
    """
    from alembic import command
    from alembic.config import Config

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, "-1")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()

    company_id = "66666666-6666-6666-6666-666666666666"
    customer_id = "77777777-7777-7777-7777-777777777777"
    product_id = "88888888-8888-8888-8888-888888888888"
    order_id = "99999999-9999-9999-9999-999999999999"
    outbox_id_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    outbox_id_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    async with engine.begin() as conn:
        uom_row = (await conn.execute(text("SELECT id FROM uom WHERE code = 'EA' LIMIT 1"))).first()
        assert uom_row is not None, "expected the autouse _seed_reference_data fixture to have run"
        uom_id = str(uom_row[0])

        await conn.execute(
            text(
                "INSERT INTO companies (id, code, name, functional_currency_code, is_active) "
                "VALUES (:id, 'DUPCO', 'Duplicate Backfill Co', 'TWD', true)"
            ),
            {"id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO customers (id, code, name, currency_code, is_active, company_id) "
                "VALUES (:id, 'DUPCUST', 'Duplicate Customer', 'TWD', true, :company_id)"
            ),
            {"id": customer_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, sku, name, uom_id, is_active, company_id) "
                "VALUES (:id, 'DUP-SKU', 'Duplicate Widget', :uom_id, true, :company_id)"
            ),
            {"id": product_id, "uom_id": uom_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO sales_orders "
                "(id, company_id, order_no, customer_id, status, currency_code, "
                "snapshot_customer_code, snapshot_customer_name) "
                "VALUES (:id, :company_id, 'SO-DUPBACKFILL-1', :customer_id, 'SHIPPED', 'TWD', "
                "'DUPCUST', 'Duplicate Customer')"
            ),
            {"id": order_id, "company_id": company_id, "customer_id": customer_id},
        )
        for outbox_id, shipped_at_iso in (
            (outbox_id_1, "2026-08-01T03:04:05+00:00"),
            (outbox_id_2, "2026-08-02T03:04:05+00:00"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO outbox (id, event_type, payload) "
                    "VALUES (:id, 'sales.goods_shipped', CAST(:payload AS JSONB))"
                ),
                {
                    "id": outbox_id,
                    "payload": (
                        f'{{"company_id": "{company_id}", "source_id": "{order_id}", '
                        f'"order_no": "SO-DUPBACKFILL-1", "lines": [], "total_cost": "0", '
                        f'"shipped_at": "{shipped_at_iso}"}}'
                    ),
                },
            )

    await core_db.dispose_engine()
    try:
        with pytest.raises(Exception, match="do not have exactly one matching"):
            await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    # Restore: delete the duplicate outbox row so a clean `upgrade head` can
    # proceed, leaving the shared `db_engine` at head for every other test
    # in the suite — same contract the backfill test above relies on.
    engine = core_db.get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox WHERE id = :id"), {"id": outbox_id_2})

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()
