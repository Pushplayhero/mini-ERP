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
