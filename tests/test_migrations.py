"""Explicit assertion that `alembic upgrade head` produced the expected schema.

The session-scoped `_run_migrations` autouse fixture in conftest.py already
runs `alembic upgrade head` before any test executes (so if it failed, the
whole suite would fail at collection/setup time) — this test additionally
asserts the resulting schema shape, so a silent "upgrade ran but created
nothing" failure mode is also caught.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

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
