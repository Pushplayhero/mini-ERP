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
from typing import Any

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Explicit "state immediately before migration N ran" revision ids, used by
# the per-migration round-trip/backfill tests below instead of a relative
# `command.downgrade(cfg, "-1")`. A relative "-1" step only means "before
# migration N" while N is still `head` — it silently stops meaning that the
# moment any later migration is appended (exactly what happened when Slice
# A's migration 0009 landed: "-1" started meaning "undo 0009 only", leaving
# 0005/0008's own schema/constraints in place and breaking these tests' setup
# — Codex diff review caught one of the four call sites still pointed at the
# wrong migration's pre-state after the first fix pass). Explicit revision
# ids are robust to any number of future migrations landing after either.
_BEFORE_MIGRATION_0005 = "a2f5d8e91c47"  # 0005's own down_revision (0004's revision id)
_BEFORE_MIGRATION_0008 = "c7d2e6f1a840"  # 0008's own down_revision (0007's revision id)
_BEFORE_MIGRATION_0011 = "e7b8c9d0a1f2"  # 0011's own down_revision (0010's revision id)
_BEFORE_MIGRATION_0012 = "b4c5d6e7f8a1"  # 0012's own down_revision (0011's revision id)
_BEFORE_MIGRATION_0013 = "c5d6e7f8a1b2"  # 0013's own down_revision (0012's revision id)

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
    # ADR-009 / WEEK9 Slice A / migration 0009.
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "user_company_roles",
    "sessions",
    # ADR-010 / WEEK10 Slice B / migration 0011.
    "company_plugins",
    # ADR-011 / WEEK11 Slice A / migration 0012.
    "custom_field_definitions",
    # ADR-012 / WEEK12 Slice A / migration 0013.
    "workflow_definitions",
    "workflow_steps",
    "workflow_instances",
    "workflow_approval_logs",
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

    def _inspect(sync_conn: Connection) -> Any:
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

    def _inspect(sync_conn: Connection) -> Any:
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
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0005)
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()

    def _inspect(sync_conn: Connection) -> set[str]:
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

    def _inspect(sync_conn: Connection) -> Any:
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
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0008)
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
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0008)
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
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0008)
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


@pytest.mark.asyncio
async def test_migration_0010_and_0011_seed_permissions_and_grant_all_to_admin(
    db_engine: AsyncEngine,
) -> None:
    """ADR-009 / ADR-010 / ADR-011: seeds the full permission catalog (45 -> 49)
    and grants every permission to the 'admin' role.
    """
    async with db_engine.connect() as conn:
        perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
        # 1 (0009) + 44 (0010) + 2 (0011) + 2 (customfield in 0012) + 3 (workflow in 0013) = 52
        assert (
            perm_count == 52
        ), f"expected exactly 52 permissions in total catalog, got {perm_count}"

        admin_grants = await conn.scalar(
            text("SELECT COUNT(*) FROM role_permissions WHERE role_code = 'admin'")
        )
        assert (
            admin_grants == 52
        ), f"expected admin role to have all 52 permissions, got {admin_grants}"


@pytest.mark.asyncio
async def test_migration_0011_schema_matches_adr_010(db_engine: AsyncEngine) -> None:
    """Assert migration 0011 creates company_plugins table and constraints."""
    from sqlalchemy import inspect

    def _inspect(sync_conn: Connection) -> Any:
        inspector = inspect(sync_conn)
        cols = {c["name"]: c for c in inspector.get_columns("company_plugins")}
        fks = inspector.get_foreign_keys("company_plugins")
        uqs = inspector.get_unique_constraints("company_plugins")
        indexes = inspector.get_indexes("company_plugins")
        return cols, fks, uqs, indexes

    async with db_engine.connect() as conn:
        cols, fks, uqs, indexes = await conn.run_sync(_inspect)

        # Permissions check
        seeded_perms = (
            (
                await conn.execute(
                    text(
                        "SELECT code FROM permissions "
                        "WHERE code IN ('platform.plugin.read', 'platform.plugin.manage')"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert "id" in cols
    assert "company_id" in cols
    assert "plugin_name" in cols
    assert "is_enabled" in cols
    assert "config" in cols
    assert "created_at" in cols
    assert "updated_at" in cols
    assert "created_by" in cols
    assert "updated_by" in cols

    # FK constraint check
    company_fk = next((fk for fk in fks if fk["referred_table"] == "companies"), None)
    assert company_fk is not None
    assert company_fk["options"].get("ondelete") == "RESTRICT"

    # Unique constraint check
    assert any(uq["name"] == "uq_company_plugins_company_plugin" for uq in uqs)

    # Index check
    assert any(ix["name"] == "ix_company_plugins_company_id" for ix in indexes)

    assert set(seeded_perms) == {"platform.plugin.read", "platform.plugin.manage"}


@pytest.mark.asyncio
async def test_migration_0011_downgrade_upgrade_cycle_is_safe(db_engine: AsyncEngine) -> None:
    """Verify downgrade to _BEFORE_MIGRATION_0011 drops company_plugins and 2 permissions,
    and upgrade head restores them cleanly.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0011)

        engine = core_db.get_engine()
        async with engine.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "company_plugins" not in table_names

            perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
            assert perm_count == 45

        await core_db.dispose_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()
    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        assert "company_plugins" in table_names

        perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
        assert perm_count == 52


@pytest.mark.asyncio
async def test_migration_0012_schema_matches_adr_011(db_engine: AsyncEngine) -> None:
    """Assert migration 0012 creates custom_field_definitions table and constraints."""
    from sqlalchemy import inspect

    def _inspect(sync_conn: Connection) -> Any:
        inspector = inspect(sync_conn)
        cols = {c["name"]: c for c in inspector.get_columns("custom_field_definitions")}
        fks = inspector.get_foreign_keys("custom_field_definitions")
        uqs = inspector.get_unique_constraints("custom_field_definitions")
        indexes = inspector.get_indexes("custom_field_definitions")
        check_constraints = inspector.get_check_constraints("custom_field_definitions")
        return cols, fks, uqs, indexes, check_constraints

    async with db_engine.connect() as conn:
        cols, fks, uqs, indexes, check_constraints = await conn.run_sync(_inspect)

        seeded_perms = (
            (
                await conn.execute(
                    text(
                        "SELECT code FROM permissions "
                        "WHERE code IN ('platform.customfield.read', 'platform.customfield.manage')"
                    )
                )
            )
            .scalars()
            .all()
        )

    expected_cols = {
        "id",
        "company_id",
        "entity_type",
        "field_key",
        "field_type",
        "label",
        "is_required",
        "is_active",
        "default_value",
        "validation_rules",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert expected_cols.issubset(cols.keys())

    # FK constraint check: company_id -> companies(id) ON DELETE RESTRICT
    company_fk = next((fk for fk in fks if fk["referred_table"] == "companies"), None)
    assert company_fk is not None
    assert company_fk["options"].get("ondelete") == "RESTRICT"

    # Unique constraint check: (company_id, entity_type, field_key)
    assert any(uq["name"] == "uq_custom_field_company_entity_key" for uq in uqs)

    # Index check: (company_id, entity_type)
    assert any(ix["name"] == "ix_custom_field_definitions_company_entity" for ix in indexes)

    # Check constraints (defense-in-depth at PostgreSQL DDL layer)
    ck_names = {ck["name"] for ck in check_constraints}
    expected_cks = {
        "ck_custom_field_key_format",
        "ck_custom_field_entity_type_valid",
        "ck_custom_field_type_valid",
        "ck_custom_field_validation_rules_is_object",
        "ck_custom_field_label_nonempty",
    }
    assert expected_cks.issubset(ck_names)

    # Permissions check
    assert set(seeded_perms) == {"platform.customfield.read", "platform.customfield.manage"}


@pytest.mark.asyncio
async def test_migration_0012_downgrade_upgrade_cycle_is_safe(db_engine: AsyncEngine) -> None:
    """Verify downgrade to _BEFORE_MIGRATION_0012 drops custom_field_definitions and 2 permissions,
    and upgrade head restores them cleanly.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0012)

        engine = core_db.get_engine()
        async with engine.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "custom_field_definitions" not in table_names

            perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
            assert perm_count == 47

        await core_db.dispose_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()
    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        assert "custom_field_definitions" in table_names

        perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
        assert perm_count == 52


@pytest.mark.asyncio
async def test_migration_0013_schema_matches_adr_012(db_engine: AsyncEngine) -> None:  # noqa: PLR0915
    """Assert migration 0013 creates 4 workflow tables with constraints and indexes."""
    from sqlalchemy import inspect

    def _inspect(sync_conn: Connection) -> Any:
        inspector = inspect(sync_conn)

        def table_info(tbl: str) -> dict[str, Any]:
            return {
                "cols": {c["name"]: c for c in inspector.get_columns(tbl)},
                "fks": inspector.get_foreign_keys(tbl),
                "uqs": inspector.get_unique_constraints(tbl),
                "indexes": inspector.get_indexes(tbl),
                "checks": inspector.get_check_constraints(tbl),
            }

        return {
            "defs": table_info("workflow_definitions"),
            "steps": table_info("workflow_steps"),
            "insts": table_info("workflow_instances"),
            "logs": table_info("workflow_approval_logs"),
        }

    async with db_engine.connect() as conn:
        schema = await conn.run_sync(_inspect)
        seeded_perms = (
            (
                await conn.execute(
                    text(
                        "SELECT code FROM permissions "
                        "WHERE code IN ("
                        "'platform.workflow.read', "
                        "'platform.workflow.manage', "
                        "'platform.workflow.approve'"
                        ")"
                    )
                )
            )
            .scalars()
            .all()
        )

    # 1. workflow_definitions
    defs = schema["defs"]
    expected_defs_cols = {
        "id",
        "company_id",
        "entity_type",
        "transition",
        "name",
        "description",
        "is_active",
        "allow_self_approval",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert expected_defs_cols.issubset(defs["cols"].keys())
    assert any(uq["name"] == "uq_workflow_def_company_id" for uq in defs["uqs"])
    assert any(uq["name"] == "uq_workflow_def_company_entity_transition" for uq in defs["uqs"])
    defs_checks = {ck["name"] for ck in defs["checks"]}
    assert "ck_workflow_def_entity_transition_valid" in defs_checks
    assert "ck_workflow_def_name_nonempty" in defs_checks

    # 2. workflow_steps
    steps = schema["steps"]
    expected_steps_cols = {
        "id",
        "company_id",
        "definition_id",
        "step_order",
        "step_name",
        "min_amount",
        "required_permission",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert expected_steps_cols.issubset(steps["cols"].keys())
    assert steps["cols"]["min_amount"]["type"].precision == 20
    assert steps["cols"]["min_amount"]["type"].scale == 6
    assert any(fk["name"] == "fk_workflow_step_company_def" for fk in steps["fks"])
    assert any(uq["name"] == "uq_workflow_step_def_order" for uq in steps["uqs"])
    steps_checks = {ck["name"] for ck in steps["checks"]}
    assert "ck_workflow_step_order_positive" in steps_checks
    assert "ck_workflow_step_min_amount_nonnegative" in steps_checks
    assert "ck_workflow_step_name_nonempty" in steps_checks

    # 3. workflow_instances
    insts = schema["insts"]
    expected_insts_cols = {
        "id",
        "company_id",
        "definition_id",
        "entity_type",
        "entity_id",
        "transition",
        "status",
        "target_amount",
        "current_step_order",
        "steps_snapshot",
        "requested_by",
        "consumed_at",
        "consumed_by",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert expected_insts_cols.issubset(insts["cols"].keys())
    assert insts["cols"]["target_amount"]["type"].precision == 20
    assert insts["cols"]["target_amount"]["type"].scale == 6
    assert any(fk["name"] == "fk_workflow_instance_company_def" for fk in insts["fks"])
    assert any(uq["name"] == "uq_workflow_instance_company_id" for uq in insts["uqs"])
    insts_checks = {ck["name"] for ck in insts["checks"]}
    assert "ck_workflow_instance_entity_transition_valid" in insts_checks
    assert "ck_workflow_instance_status_valid" in insts_checks
    assert "ck_workflow_instance_target_amount_nonnegative" in insts_checks
    assert "ck_workflow_instance_steps_snapshot_is_array" in insts_checks
    assert "ck_workflow_instance_pending_has_step" in insts_checks
    assert "ck_workflow_instance_consumed_timestamp" in insts_checks
    assert any(
        ix["name"] == "uq_workflow_instance_active" and ix["unique"] is True
        for ix in insts["indexes"]
    )
    assert any(ix["name"] == "ix_workflow_instances_lookup" for ix in insts["indexes"])

    # 4. workflow_approval_logs
    logs = schema["logs"]
    expected_logs_cols = {
        "id",
        "company_id",
        "instance_id",
        "step_order",
        "step_name",
        "decision",
        "actor_id",
        "comment",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert expected_logs_cols.issubset(logs["cols"].keys())
    assert any(fk["name"] == "fk_workflow_log_company_instance" for fk in logs["fks"])
    assert any(uq["name"] == "uq_workflow_log_instance_step" for uq in logs["uqs"])
    logs_checks = {ck["name"] for ck in logs["checks"]}
    assert "ck_workflow_log_step_order_positive" in logs_checks
    assert "ck_workflow_log_step_name_nonempty" in logs_checks
    assert "ck_workflow_log_decision_valid" in logs_checks

    # 5. Seeded permissions
    assert set(seeded_perms) == {
        "platform.workflow.read",
        "platform.workflow.manage",
        "platform.workflow.approve",
    }


@pytest.mark.asyncio
async def test_migration_0013_downgrade_upgrade_cycle_is_safe(db_engine: AsyncEngine) -> None:
    """Verify downgrade to _BEFORE_MIGRATION_0013 drops 4 tables (even when populated with
    steps referencing seeded workflow permissions) and 3 permissions, and upgrade head
    restores them cleanly.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app.core import db as core_db

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    workflow_tables = {
        "workflow_definitions",
        "workflow_steps",
        "workflow_instances",
        "workflow_approval_logs",
    }

    company_id = "91919191-9191-9191-9191-919191919191"
    def_id = "92929292-9292-9292-9292-929292929292"
    step_id = "93939393-9393-9393-9393-939393939393"
    inst_id = "94949494-9494-9494-9494-949494949494"
    log_id = "95959595-9595-9595-9595-959595959595"
    entity_id = "96969696-9696-9696-9696-969696969696"
    actor_id = "97979797-9797-9797-9797-979797979797"

    # Populate all 4 tables (including workflow_steps referencing 'platform.workflow.approve')
    # to prove downgrade() drops tables in safe FK dependency order before deleting permissions.
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO companies (id, code, name, functional_currency_code, is_active) "
                "VALUES (:id, 'WFCO', 'Workflow Test Co', 'TWD', true)"
            ),
            {"id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO workflow_definitions "
                "(id, company_id, entity_type, transition, name) "
                "VALUES (:id, :company_id, 'sales_order', 'confirm', 'SO Confirm Flow')"
            ),
            {"id": def_id, "company_id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO workflow_steps "
                "(id, company_id, definition_id, step_order, step_name, min_amount, "
                "required_permission) "
                "VALUES (:id, :company_id, :def_id, 1, 'Step 1', 100.000000, "
                "'platform.workflow.approve')"
            ),
            {"id": step_id, "company_id": company_id, "def_id": def_id},
        )
        await conn.execute(
            text(
                "INSERT INTO workflow_instances "
                "(id, company_id, definition_id, entity_type, entity_id, transition, status, "
                "target_amount, current_step_order, steps_snapshot, requested_by) "
                "VALUES (:id, :company_id, :def_id, 'sales_order', :entity_id, 'confirm', "
                "'PENDING', "
                "500.000000, 1, '[]'::jsonb, :actor_id)"
            ),
            {
                "id": inst_id,
                "company_id": company_id,
                "def_id": def_id,
                "entity_id": entity_id,
                "actor_id": actor_id,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO workflow_approval_logs "
                "(id, company_id, instance_id, step_order, step_name, decision, actor_id) "
                "VALUES (:id, :company_id, :inst_id, 1, 'Step 1', 'APPROVE', :actor_id)"
            ),
            {"id": log_id, "company_id": company_id, "inst_id": inst_id, "actor_id": actor_id},
        )

    await core_db.dispose_engine()
    try:
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE_MIGRATION_0013)

        engine = core_db.get_engine()
        async with engine.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert not (workflow_tables & table_names)

            perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
            assert perm_count == 49

        await core_db.dispose_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await core_db.dispose_engine()

    engine = core_db.get_engine()
    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        assert workflow_tables.issubset(table_names)

        perm_count = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
        assert perm_count == 52
