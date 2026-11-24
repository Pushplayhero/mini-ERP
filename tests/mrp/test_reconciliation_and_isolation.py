"""Cross-company isolation, RBAC, 1310 control protection, concurrency & rebuild_mrp_balances."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.cli import rebuild_mrp_balances
from tests.conftest import company_headers
from tests.inventory._helpers import on_hand
from tests.ledger._helpers import balanced_lines
from tests.mrp._helpers import (
    bom_line,
    calculate_net_requirements,
    confirm_work_order,
    create_bom,
    create_product,
    create_work_order,
    explode_bom,
    issue_materials,
    seed_stock_via_po,
    setup_mrp_company,
)
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)


@pytest.mark.asyncio
async def test_mrp_cross_company_isolation_rbac_and_1310_control_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Isolate tenants cleanly, reject unprivileged roles 403, and guard 1310 WIP account."""
    ctx_a = await setup_mrp_company(client, "MRPA")
    ctx_b = await setup_mrp_company(client, "MRPB")

    raw_a = await create_product(client, ctx_a.company_id, "RAWA", standard_cost="10")
    fg_a = await create_product(client, ctx_a.company_id, "FGA", standard_cost="20")
    bom_a = (
        await create_bom(
            client, ctx_a.company_id, code="BOM-A", product_id=fg_a, lines=[bom_line(raw_a, "1")]
        )
    ).json()
    wo_a = (
        await create_work_order(client, ctx_a.company_id, product_id=fg_a, planned_qty="2")
    ).json()

    # Tenant B cannot read, mutate, explode, or reference Tenant A's BOMs/WOs/products
    assert (
        await client.get(
            f"/api/v1/mrp/boms/{bom_a['id']}", headers=company_headers(ctx_b.company_id)
        )
    ).status_code == 404
    assert (await explode_bom(client, ctx_b.company_id, bom_a["id"])).status_code == 404
    assert (
        await client.get(
            f"/api/v1/mrp/work-orders/{wo_a['id']}", headers=company_headers(ctx_b.company_id)
        )
    ).status_code == 404
    assert (await confirm_work_order(client, ctx_b.company_id, wo_a["id"])).status_code == 404
    assert (
        await calculate_net_requirements(client, ctx_b.company_id, demands=[(fg_a, "5")])
    ).status_code == 422

    # Manual Journal Entry touching 1310 Work in Process fails 422 (control account protection)
    accs_resp = await client.get("/api/v1/accounts", headers=company_headers(ctx_a.company_id))
    accs = {a["code"]: a["id"] for a in accs_resp.json()}
    manual_je = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-11-24",
            "description": "Direct WIP hack",
            "lines": balanced_lines(uuid.UUID(accs["1310"]), uuid.UUID(accs["1300"]), "50"),
        },
        headers=company_headers(ctx_a.company_id),
    )
    assert manual_je.status_code == 422
    assert "control account" in manual_je.text.lower()

    # Empty role fails 403 on MRP endpoints
    await create_role_without_permissions(db_session, "no_mrp")
    await create_user_with_role(
        db_session,
        email="nomrp@example.com",
        password="pw",
        company_id=ctx_a.company_id,
        role_code="no_mrp",
    )
    tok = (
        await client.post("/auth/login", json={"email": "nomrp@example.com", "password": "pw"})
    ).json()["access_token"]
    assert (
        await client.get("/api/v1/mrp/boms", headers={"Authorization": f"Bearer {tok}"})
    ).status_code == 403


@pytest.mark.asyncio
async def test_concurrent_material_issue_contention_and_rebuild_mrp_balances(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two WOs racing scarce component stock serialize; rebuild_mrp_balances repairs drift."""
    from app.main import app

    ctx = await setup_mrp_company(client, "MRPRACE")
    raw_id = await create_product(client, ctx.company_id, "SCARCE-RAW", standard_cost="20")
    fg_id = await create_product(client, ctx.company_id, "RACE-FG")
    await seed_stock_via_po(client, ctx.company_id, ctx.supplier_id, [(raw_id, "5", "20")])

    assert (
        await create_bom(
            client, ctx.company_id, code="B-RACE", product_id=fg_id, lines=[bom_line(raw_id, "4")]
        )
    ).status_code == 201

    wo1 = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="1")
    ).json()
    wo2 = (
        await create_work_order(client, ctx.company_id, product_id=fg_id, planned_qty="1")
    ).json()
    assert (await confirm_work_order(client, ctx.company_id, wo1["id"])).status_code == 200
    assert (await confirm_work_order(client, ctx.company_id, wo2["id"])).status_code == 200

    # Concurrent material issues each asking 4 units out of 5 -> 1 succeeds (200), 1 fails (409)
    async def _issue(wo_id: str, ref: str) -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            r = await issue_materials(ac, ctx.company_id, wo_id, request_ref=ref)
            return r.status_code

    statuses = await asyncio.gather(_issue(wo1["id"], "race-1"), _issue(wo2["id"], "race-2"))
    assert sorted(statuses) == [200, 409]
    assert await on_hand(client, ctx.company_id, raw_id) == Decimal("1")

    # Identify winning WO and test rebuild_mrp_balances recovery after SQL corruption
    winner_id = wo1["id"] if statuses[0] == 200 else wo2["id"]
    before = (
        await client.get(
            f"/api/v1/mrp/work-orders/{winner_id}", headers=company_headers(ctx.company_id)
        )
    ).json()
    assert Decimal(before["wip_balance"]) == Decimal("80")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE work_orders SET wip_balance = 99999, completed_qty = 0.5 WHERE id = :id"),
            {"id": winner_id},
        )
        await conn.execute(
            text(
                "UPDATE work_order_lines SET issued_qty = 0, issued_cost = 0 "
                "WHERE work_order_id = :id"
            ),
            {"id": winner_id},
        )

    await rebuild_mrp_balances.rebuild_mrp_balances()
    after = (
        await client.get(
            f"/api/v1/mrp/work-orders/{winner_id}", headers=company_headers(ctx.company_id)
        )
    ).json()
    assert Decimal(after["wip_balance"]) == Decimal("80")
    assert Decimal(after["completed_qty"]) == Decimal("0")
    assert Decimal(after["lines"][0]["issued_qty"]) == Decimal("4")
    assert Decimal(after["lines"][0]["issued_cost"]) == Decimal("80")
