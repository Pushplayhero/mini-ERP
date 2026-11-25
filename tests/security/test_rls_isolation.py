"""PostgreSQL Row-Level Security (RLS) defense-in-depth tests (ADR-018)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.conftest import company_headers
from tests.platform.permissions._helpers import create_company
from tests.sales._helpers import create_customer

from app.core.tenancy import company_context


@pytest.mark.asyncio
async def test_session_executes_under_non_privileged_mini_erp_app_role(
    db_session: AsyncSession,
) -> None:
    """AsyncSession transactions switch locally to `mini_erp_app` (NOBYPASSRLS)."""
    role_name = (await db_session.execute(text("SELECT current_user"))).scalar_one()
    assert role_name == "mini_erp_app"

    role_flags = (
        await db_session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles " "WHERE rolname = 'mini_erp_app'")
        )
    ).one()
    assert role_flags.rolsuper is False
    assert role_flags.rolbypassrls is False

    # Runtime role must not be able to read or mutate alembic_version.
    with pytest.raises(DBAPIError):
        await db_session.execute(text("SELECT * FROM alembic_version"))
    await db_session.rollback()


@pytest.mark.asyncio
async def test_rls_blocks_raw_sql_reads_and_writes_without_company_context(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Raw SQL bypassing ORM `with_loader_criteria` still obeys PostgreSQL RLS."""
    from tests._auth_helpers import setup_company_with_admin

    auth_a = await setup_company_with_admin(client, code="RLSA")
    auth_b = await setup_company_with_admin(client, code="RLSB")
    cust_a_id = await create_customer(client, auth_a.company_id, "C-RLSA")
    await create_customer(client, auth_b.company_id, "C-RLSB")

    # 1. No company_context bound -> raw SQL SELECT sees 0 rows across tenant tables.
    cust_rows = (await db_session.execute(text("SELECT id FROM customers"))).all()
    assert cust_rows == []
    seq_rows = (await db_session.execute(text("SELECT company_id FROM ledger_sequences"))).all()
    assert seq_rows == []

    # 2. No company_context bound -> raw SQL INSERT fails RLS WITH CHECK.
    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO customers (id, company_id, code, name, currency_code, is_active) "
                "VALUES (:id, :cid, 'HACK', 'Hack Co', 'TWD', true)"
            ),
            {"id": str(uuid.uuid4()), "cid": str(auth_a.company_id)},
        )
    await db_session.rollback()

    # 3. Bound to Company A -> sees only Company A rows, cannot read/update/delete Company B.
    with company_context(auth_a.company_id):
        visible_ids = {
            row[0] for row in (await db_session.execute(text("SELECT id FROM customers"))).all()
        }
        assert visible_ids == {cust_a_id}

        # Cross-tenant UPDATE affects 0 rows
        upd_res = await db_session.execute(
            text("UPDATE customers SET name = 'PWNED' WHERE company_id = :cb"),
            {"cb": str(auth_b.company_id)},
        )
        assert isinstance(upd_res, CursorResult)
        assert upd_res.rowcount == 0

        # Cross-tenant INSERT with forged company_id raises RLS violation
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO customers (id, company_id, code, name, currency_code, is_active) "
                    "VALUES (:id, :cb, 'FORGED', 'Forged Co', 'TWD', true)"
                ),
                {"id": str(uuid.uuid4()), "cb": str(auth_b.company_id)},
            )
    await db_session.rollback()

    # Company B's customer remains intact
    resp_b = await client.get("/api/v1/customers", headers=company_headers(auth_b.company_id))
    assert resp_b.status_code == 200
    assert [c["code"] for c in resp_b.json()] == ["C-RLSB"]


@pytest.mark.asyncio
async def test_rls_dynamic_context_switch_and_savepoint_rollback_sync(
    db_session: AsyncSession,
) -> None:
    """Same-transaction tenant switches and nested SAVEPOINT rollbacks sync RLS GUC cleanly."""
    co_a = await create_company(db_session, "RLSSWA")
    co_b = await create_company(db_session, "RLSSWB")

    with company_context(co_a):
        await db_session.execute(
            text(
                "INSERT INTO ledger_sequences (company_id, year, next_no) "
                "VALUES (:cid, 2026, 10)"
            ),
            {"cid": str(co_a)},
        )
        guc_a = (await db_session.execute(text("SELECT app_current_company_id()"))).scalar_one()
        assert guc_a == co_a

        # Nested savepoint switches to co_b, inserts, then rolls back
        try:
            async with db_session.begin_nested():
                with company_context(co_b):
                    await db_session.execute(
                        text(
                            "INSERT INTO ledger_sequences (company_id, year, next_no) "
                            "VALUES (:cid, 2026, 20)"
                        ),
                        {"cid": str(co_b)},
                    )
                    guc_b = (
                        await db_session.execute(text("SELECT app_current_company_id()"))
                    ).scalar_one()
                    assert guc_b == co_b
                    raise RuntimeError("trigger savepoint rollback")
        except RuntimeError:
            pass

        # Outer context co_a must be re-synchronized automatically after SAVEPOINT rollback
        guc_after_rb = (
            await db_session.execute(text("SELECT app_current_company_id()"))
        ).scalar_one()
        assert guc_after_rb == co_a
        rows_a = (
            await db_session.execute(text("SELECT year, next_no FROM ledger_sequences"))
        ).all()
        assert [tuple(r) for r in rows_a] == [(2026, 10)]

        # Same transaction switches cleanly to co_b (no co_a rows visible)
        with company_context(co_b):
            rows_b = (
                await db_session.execute(text("SELECT year, next_no FROM ledger_sequences"))
            ).all()
            assert rows_b == []
    await db_session.rollback()


@pytest.mark.asyncio
async def test_pool_checkin_clears_rls_state_for_subsequent_core_connections(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Pooled DBAPI connections do not retain `_rls_managed` across Core engine checkouts."""
    co_a = await create_company(db_session, "RLSPL")
    with company_context(co_a):
        role_in_orm = (await db_session.execute(text("SELECT current_user"))).scalar_one()
        assert role_in_orm == "mini_erp_app"
    await db_session.commit()

    # Subsequent Core engine connection must start clean without leftover `_rls_*` pool metadata
    async with db_engine.begin() as core_conn:
        assert "_rls_managed" not in core_conn.info
        assert "_rls_company_id" not in core_conn.info
        assert "_rls_syncing" not in core_conn.info
        core_guc = (await core_conn.execute(text("SELECT app_current_company_id()"))).scalar_one()
        assert core_guc is None

    # Even when ContextVar is set, non-ORM Core connections are not hijacked by Session hooks
    with company_context(co_a):
        async with db_engine.begin() as core_conn_ctx:
            assert "_rls_managed" not in core_conn_ctx.info
            core_guc_ctx = (
                await core_conn_ctx.execute(text("SELECT app_current_company_id()"))
            ).scalar_one()
            assert core_guc_ctx is None
