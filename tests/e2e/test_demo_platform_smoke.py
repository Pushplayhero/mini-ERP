"""End-to-end smoke, idempotency, and guard tests for `app.cli.demo_platform`."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.cli.bootstrap_admin import bootstrap_admin
from app.cli.demo_o2c import run_demo
from app.cli.demo_platform import PLATFORM_CUSTOMER_CODE, run_demo_platform


@pytest.mark.asyncio
async def test_demo_platform_runs_idempotently_and_preserves_o2c(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Act 1-4 pass, second run has zero drift, and `demo_o2c` still works."""
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="PLATDEMO",
        company_name="Platform Demo Co",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        first_run = await run_demo_platform(demo_client)
        assert first_run.settled_canonical_in_this_run is True
        assert first_run.credit_limit_blocked_status == 409
        assert first_run.plugin_disabled_workflow_status == 409
        assert first_run.customfield_reject_statuses == [422, 422, 422, 422]
        assert first_run.customfield_normalized_cap == "12.345678"
        assert first_run.customfield_deactivated_reject_status == 422
        assert first_run.workflow_missing_status == 409
        assert first_run.workflow_sod_status == 403
        assert first_run.workflow_step_rbac_status == 403
        assert first_run.workflow_final_status == "CONSUMED"
        assert first_run.trial_balance_debit == first_run.trial_balance_credit
        assert first_run.integration_dry_run_status == "DRY_RUN"
        assert first_run.integration_export_rows >= 1
        assert first_run.integration_ssrf_blocked_status == 422

        # Second run must pass identically with zero stock or ledger drift.
        second_run = await run_demo_platform(demo_client)
        assert second_run.settled_canonical_in_this_run is False
        assert second_run.canonical_order_id == first_run.canonical_order_id
        assert second_run.workflow_final_status == "CANCELLED"
        assert second_run.trial_balance_debit == first_run.trial_balance_debit
        assert second_run.trial_balance_credit == first_run.trial_balance_credit
        assert second_run.stock_on_hand == first_run.stock_on_hand
        assert second_run.integration_dry_run_status == "DRY_RUN"
        assert second_run.integration_export_rows == first_run.integration_export_rows
        assert second_run.integration_ssrf_blocked_status == 422

        # Phase 1 O2C baseline must succeed seamlessly after demo_platform cleanup.
        await run_demo(demo_client)


@pytest.mark.asyncio
async def test_demo_platform_fails_closed_in_production_and_partial_env(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production and partial approver env configs must fail closed before any write."""
    from app.core.settings import get_settings
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")
    monkeypatch.delenv("DEMO_APPROVER_1_EMAIL", raising=False)
    monkeypatch.delenv("DEMO_APPROVER_1_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_APPROVER_2_EMAIL", raising=False)
    monkeypatch.delenv("DEMO_APPROVER_2_PASSWORD", raising=False)

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="PRODGATE",
        company_name="Prod Gate Co",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        # 1. Partial approver env configuration fails closed immediately.
        monkeypatch.setenv("DEMO_APPROVER_1_EMAIL", "lead@example.com")
        with pytest.raises(RuntimeError, match="partial DEMO_APPROVER_"):
            await run_demo_platform(demo_client)
        monkeypatch.delenv("DEMO_APPROVER_1_EMAIL", raising=False)

        # 2. Production without explicit approver env vars fails closed before any DB mutation.
        monkeypatch.setenv("APP_ENVIRONMENT", "production")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="APP_ENVIRONMENT=production"):
                await run_demo_platform(demo_client)
        finally:
            monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
            get_settings.cache_clear()

        # Verify zero side-effects were written prior to the fail-closed check.
        login_resp = await demo_client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "adminpassword123"},
        )
        login_resp.raise_for_status()
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
        cust_resp = await demo_client.get("/api/v1/customers", headers=headers)
        cust_resp.raise_for_status()
        assert all(c["code"] != PLATFORM_CUSTOMER_CODE for c in cust_resp.json())
