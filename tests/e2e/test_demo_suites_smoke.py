"""End-to-end smoke, idempotency, self-healing, and guard tests for `app.cli.demo_suites`."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.cli.bootstrap_admin import bootstrap_admin
from app.cli.demo_o2c import run_demo
from app.cli.demo_suites import (
    FG_LIST_PRICE,
    FG_SELL_QTY,
    SUITES_BOM_FG_CODE,
    SUITES_CUSTOMER_CODE,
    SUITES_SUPPLIER_CODE,
    _ensure_masterdata,
    _get_or_create,
    _run_act_1_p2p,
    _run_act_2_costing,
    _run_act_3_mrp,
    run_demo_suites,
)


@pytest.mark.asyncio
async def test_demo_suites_runs_idempotently_self_heals_and_preserves_o2c(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify walkthrough, zero-drift rerun, self-healing reactivation, and `demo_o2c` compat."""
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="SUITESDEMO",
        company_name="Suites Demo Co",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        first_run = await run_demo_suites(demo_client)
        assert first_run.settled_canonical_in_this_run is True
        assert Decimal(first_run.raw_mwa_cost) == Decimal("25")
        assert first_run.bom_cycle_rejected_status == 422
        assert Decimal(first_run.bom_explosion_unit_cost) == Decimal("50")
        assert Decimal(first_run.mrp_fg_net_req) == Decimal("6")
        assert first_run.mrp_raw_action == "purchase"
        assert Decimal(first_run.wo_sub_wip_balance) == Decimal("0")
        assert Decimal(first_run.wo_fg_wip_balance) == Decimal("0")
        assert Decimal(first_run.fg_mwa_cost) == Decimal("50")
        assert Decimal(first_run.fg_on_hand) == Decimal("2")
        assert first_run.trial_balance_debit == first_run.trial_balance_credit

        # Verify exact GL balances after Run 1 on the clean bootstrapped company.
        login_resp = await demo_client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "adminpassword123"},
        )
        login_resp.raise_for_status()
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
        tb_resp = await demo_client.get("/api/v1/reports/trial-balance", headers=headers)
        tb_resp.raise_for_status()
        tb_by_code = {row["account_code"]: row for row in tb_resp.json()}
        assert Decimal(tb_by_code["1300"]["total_debit"]) - Decimal(
            tb_by_code["1300"]["total_credit"]
        ) == Decimal("150")
        assert Decimal(tb_by_code["1310"]["total_debit"]) - Decimal(
            tb_by_code["1310"]["total_credit"]
        ) == Decimal("0")
        assert Decimal(tb_by_code["2000"]["total_credit"]) - Decimal(
            tb_by_code["2000"]["total_debit"]
        ) == Decimal("0")
        assert Decimal(tb_by_code["1100"]["total_debit"]) - Decimal(
            tb_by_code["1100"]["total_credit"]
        ) == Decimal("0")
        assert Decimal(tb_by_code["5000"]["total_debit"]) - Decimal(
            tb_by_code["5000"]["total_credit"]
        ) == Decimal("100")
        assert Decimal(tb_by_code["4000"]["total_credit"]) - Decimal(
            tb_by_code["4000"]["total_debit"]
        ) == Decimal("520")

        # Second run must reuse the exact records and produce zero ledger/inventory drift.
        second_run = await run_demo_suites(demo_client)
        assert second_run.settled_canonical_in_this_run is False
        assert second_run.purchase_order_id == first_run.purchase_order_id
        assert second_run.bill_id == first_run.bill_id
        assert second_run.landed_cost_id == first_run.landed_cost_id
        assert second_run.wo_sub_id == first_run.wo_sub_id
        assert second_run.wo_fg_id == first_run.wo_fg_id
        assert second_run.sales_order_id == first_run.sales_order_id
        assert Decimal(second_run.mrp_fg_net_req) == Decimal("4")
        assert second_run.trial_balance_debit == first_run.trial_balance_debit
        assert second_run.trial_balance_credit == first_run.trial_balance_credit
        assert second_run.fg_on_hand == first_run.fg_on_hand
        assert second_run.fg_mwa_cost == first_run.fg_mwa_cost

        # Deactivate supplier/FG BOM and lower customer credit limit to verify self-healing.
        sups = (await demo_client.get("/api/v1/suppliers", headers=headers)).json()
        sup_id = next(s["id"] for s in sups if s["code"] == SUITES_SUPPLIER_CODE)
        assert (
            await demo_client.patch(
                f"/api/v1/suppliers/{sup_id}", json={"is_active": False}, headers=headers
            )
        ).status_code == 200

        custs = (await demo_client.get("/api/v1/customers", headers=headers)).json()
        cust_id = next(c["id"] for c in custs if c["code"] == SUITES_CUSTOMER_CODE)
        assert (
            await demo_client.patch(
                f"/api/v1/customers/{cust_id}",
                json={"credit_limit": "1.000000", "is_active": False},
                headers=headers,
            )
        ).status_code == 200

        boms = (await demo_client.get("/api/v1/mrp/boms", headers=headers)).json()
        bom_fg_id = next(b["id"] for b in boms if b["code"] == SUITES_BOM_FG_CODE)
        assert (
            await demo_client.patch(
                f"/api/v1/mrp/boms/{bom_fg_id}", json={"is_active": False}, headers=headers
            )
        ).status_code == 200

        healed_run = await run_demo_suites(demo_client)
        assert healed_run.settled_canonical_in_this_run is True
        assert healed_run.trial_balance_debit == first_run.trial_balance_debit
        assert healed_run.fg_on_hand == first_run.fg_on_hand

        # Clean post-heal rerun returns False with zero changes.
        fifth_run_state = await run_demo_suites(demo_client)
        assert fifth_run_state.settled_canonical_in_this_run is False

        # Phase 1 O2C baseline demo must continue succeeding cleanly in the same company.
        await run_demo(demo_client)


@pytest.mark.asyncio
async def test_demo_suites_resumes_interrupted_ar_and_recovers_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify interrupted SO->ship AR resume sets settled=True and _get_or_create handles 409."""
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="SUITESINTR",
        company_name="Suites Interrupt Co",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        login_resp = await demo_client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "adminpassword123"},
        )
        login_resp.raise_for_status()
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

        # Create a supplier first, then force a stale initial miss so POST hits HTTP 409.
        sup_resp = await demo_client.post(
            "/api/v1/suppliers",
            json={
                "code": SUITES_SUPPLIER_CODE,
                "name": "Suites Raw Material Vendor",
                "currency_code": "TWD",
                "payment_terms_days": 30,
            },
            headers=headers,
        )
        assert sup_resp.status_code == 201
        created_sup = sup_resp.json()

        attempts = 0

        def _miss_first_hit_second(row: dict[str, object]) -> bool:
            nonlocal attempts
            attempts += 1
            return attempts > 1 and row["code"] == SUITES_SUPPLIER_CODE

        recovered, created_now = await _get_or_create(
            demo_client,
            headers,
            list_path="/api/v1/suppliers",
            create_path="/api/v1/suppliers",
            payload={
                "code": SUITES_SUPPLIER_CODE,
                "name": "Suites Raw Material Vendor",
                "currency_code": "TWD",
                "payment_terms_days": 30,
            },
            match=_miss_first_hit_second,
            compatible=lambda row: row["currency_code"] == "TWD",
        )
        assert created_now is False
        assert recovered["id"] == created_sup["id"]

        # Simulate crash after Act 1-3 + SO ship (before AR invoice/payment), then verify resume.
        company_id = login_resp.json()["active_company_id"]
        api = "http://127.0.0.1:8000/api/v1"
        md = await _ensure_masterdata(demo_client, headers, api, company_id=company_id)
        po, _, _ = await _run_act_1_p2p(demo_client, headers, api, md)
        await _run_act_2_costing(demo_client, headers, api, md, po)
        await _run_act_3_mrp(demo_client, headers, api, md)

        fg_id = str(md.fg_product["id"])
        assert (
            await demo_client.post(
                f"{api}/costing/summaries/{fg_id}/sync-standard-cost", headers=headers
            )
        ).status_code == 200
        pre_so = (
            await demo_client.post(
                f"{api}/sales-orders",
                json={
                    "customer_id": md.customer["id"],
                    "lines": [
                        {
                            "product_id": fg_id,
                            "qty": str(FG_SELL_QTY),
                            "unit_price": FG_LIST_PRICE,
                        }
                    ],
                },
                headers=headers,
            )
        ).json()
        assert (
            await demo_client.post(f"{api}/sales-orders/{pre_so['id']}/confirm", headers=headers)
        ).status_code == 200
        assert (
            await demo_client.post(f"{api}/sales-orders/{pre_so['id']}/ship", headers=headers)
        ).status_code == 200

        res = await run_demo_suites(demo_client)
        assert res.sales_order_id == pre_so["id"]
        assert res.settled_canonical_in_this_run is True
        assert Decimal(res.fg_on_hand) == Decimal("2")

        res_again = await run_demo_suites(demo_client)
        assert res_again.settled_canonical_in_this_run is False
        assert res_again.trial_balance_debit == res.trial_balance_debit


@pytest.mark.asyncio
async def test_demo_suites_fails_closed_in_production(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`APP_ENVIRONMENT=production` must reject demo execution before writing any business rows."""
    from app.core.settings import get_settings
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")
    monkeypatch.delenv("ALLOW_DEMO_SUITES_IN_PRODUCTION", raising=False)

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="SUITESPROD",
        company_name="Suites Prod Gate Co",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        monkeypatch.setenv("APP_ENVIRONMENT", "production")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="APP_ENVIRONMENT=production"):
                await run_demo_suites(demo_client)
        finally:
            monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
            get_settings.cache_clear()

        login_resp = await demo_client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "adminpassword123"},
        )
        login_resp.raise_for_status()
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
        sup_resp = await demo_client.get("/api/v1/suppliers", headers=headers)
        sup_resp.raise_for_status()
        assert all(s["code"] != SUITES_SUPPLIER_CODE for s in sup_resp.json())
