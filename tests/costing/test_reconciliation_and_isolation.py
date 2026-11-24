"""Cross-company isolation, domain guard & rebuild_cost_summaries tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.cli import rebuild_cost_summaries
from tests.conftest import company_headers
from tests.costing._helpers import (
    create_adjustment,
    get_cost_moves,
    get_summaries,
    setup_costing_company,
    void_adjustment,
)
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)
from tests.purchase._helpers import create_draft_order, create_product, order_line
from tests.sales._helpers import (
    confirm_order as confirm_sales_order,
)
from tests.sales._helpers import (
    create_customer,
    ship_order,
)
from tests.sales._helpers import (
    create_draft_order as create_sales_draft,
)
from tests.sales._helpers import (
    order_line as sales_order_line,
)


@pytest.mark.asyncio
async def test_cross_company_isolation_and_rbac_fail_closed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Tenant B cannot view or reference Tenant A's costing records; empty role gets 403."""
    ctx_a = await setup_costing_company(client, "ISOCOA", standard_cost="30", initial_po_qty="4")
    ctx_b = await setup_costing_company(client, "ISOCOB", standard_cost="20", initial_po_qty="2")

    adj_a = await create_adjustment(
        client,
        ctx_a.company_id,
        adjustment_type="landed_cost",
        product_id=ctx_a.product_id,
        order_id=ctx_a.order["id"],
        amount="12",
        reason="Freight A",
    )
    assert adj_a.status_code == 201
    adj_a_id = adj_a.json()["id"]

    # Company B cannot read or void Company A's adjustment
    assert (
        await client.get(
            f"/api/v1/costing/adjustments/{adj_a_id}", headers=company_headers(ctx_b.company_id)
        )
    ).status_code == 404
    assert (await void_adjustment(client, ctx_b.company_id, adj_a_id)).status_code == 404

    # Company B cannot list Company A's summaries or moves via product_id filter
    assert await get_summaries(client, ctx_b.company_id, product_id=ctx_a.product_id) == []
    assert await get_cost_moves(client, ctx_b.company_id, product_id=ctx_a.product_id) == []

    # Company B cannot reference Company A's product or PO
    cross_prod = await create_adjustment(
        client,
        ctx_b.company_id,
        adjustment_type="revaluation",
        product_id=ctx_a.product_id,
        amount="10",
        reason="Cross-tenant attack",
    )
    assert cross_prod.status_code == 422

    cross_po = await create_adjustment(
        client,
        ctx_b.company_id,
        adjustment_type="ppv",
        product_id=ctx_b.product_id,
        order_id=ctx_a.order["id"],
        amount="10",
        reason="Cross-tenant PO",
    )
    assert cross_po.status_code == 422

    # Unprivileged role gets 403 across endpoints
    await create_role_without_permissions(db_session, "no_costing")
    await create_user_with_role(
        db_session,
        email="nocost@example.com",
        password="pw",
        company_id=ctx_a.company_id,
        role_code="no_costing",
    )
    login = await client.post("/auth/login", json={"email": "nocost@example.com", "password": "pw"})
    tok = login.json()["access_token"]
    resp = await client.get("/api/v1/costing/summaries", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_adjustment_domain_and_state_guards(client: AsyncClient) -> None:
    """Validate PO state, SKU membership, negative value guards & insufficient stock void block."""
    ctx = await setup_costing_company(
        client, "GUARDCO", standard_cost="40", list_price="100", initial_po_qty="4"
    )
    other_prod_id = await create_product(client, ctx.company_id, "OTHER-SKU", standard_cost="15")
    draft_po = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(other_prod_id, "2", unit_cost="15")]
    )

    # 1. Draft (non-received) PO rejected for LANDED_COST & PPV
    assert (
        await create_adjustment(
            client,
            ctx.company_id,
            adjustment_type="landed_cost",
            product_id=other_prod_id,
            order_id=draft_po["id"],
            amount="10",
            reason="Draft PO",
        )
    ).status_code == 422

    # 2. Product not present on PO rejected
    assert (
        await create_adjustment(
            client,
            ctx.company_id,
            adjustment_type="ppv",
            product_id=other_prod_id,
            order_id=ctx.order["id"],
            amount="10",
            reason="Wrong SKU",
        )
    ).status_code == 422

    # 3. Schema invariants: zero amount, negative landed_cost, missing order_id, order_id on reval
    for bad_payload in (
        {
            "adjustment_type": "revaluation",
            "product_id": str(ctx.product_id),
            "amount": "0",
            "reason": "Zero",
        },
        {
            "adjustment_type": "revaluation",
            "product_id": str(ctx.product_id),
            "order_id": ctx.order["id"],
            "amount": "10",
            "reason": "Revaluation should not reference PO",
        },
        {
            "adjustment_type": "landed_cost",
            "product_id": str(ctx.product_id),
            "order_id": ctx.order["id"],
            "amount": "-5",
            "reason": "Neg LC",
        },
        {
            "adjustment_type": "ppv",
            "product_id": str(ctx.product_id),
            "amount": "5",
            "reason": "Missing PO",
        },
    ):
        r = await client.post(
            "/api/v1/costing/adjustments",
            json=bad_payload,
            headers=company_headers(ctx.company_id),
        )
        assert r.status_code == 422, r.text

    # 4. Zero tracked qty product rejects REVALUATION (409) and downward below zero (409)
    assert (
        await create_adjustment(
            client,
            ctx.company_id,
            adjustment_type="revaluation",
            product_id=other_prod_id,
            amount="5",
            reason="Untracked SKU",
        )
    ).status_code == 409

    assert (
        await create_adjustment(
            client,
            ctx.company_id,
            adjustment_type="revaluation",
            product_id=ctx.product_id,
            amount="-500",
            reason="Excessive write-down",
        )
    ).status_code == 409

    # 5. Capitalize +40, ship all 4 units, then voiding landed_cost fails 409 (tracked qty is 0)
    lc = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="landed_cost",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="40",
        reason="Freight before full ship",
    )
    assert lc.status_code == 201
    cust_id = await create_customer(client, ctx.company_id, "GRDCUST")
    so = await create_sales_draft(
        client, ctx.company_id, cust_id, [sales_order_line(ctx.product_id, "4")]
    )
    assert (await confirm_sales_order(client, ctx.company_id, so["id"])).status_code == 200
    assert (await ship_order(client, ctx.company_id, so["id"])).status_code == 200

    assert (await void_adjustment(client, ctx.company_id, lc.json()["id"])).status_code == 409


@pytest.mark.asyncio
async def test_rebuild_cost_summaries_recovers_corrupted_and_deleted_rows(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """rebuild_cost_summaries restores exact MWA summary from append-only cost_moves."""
    ctx = await setup_costing_company(
        client, "RBLDCO", standard_cost="40", list_price="100", initial_po_qty="4"
    )
    lc = await create_adjustment(
        client,
        ctx.company_id,
        adjustment_type="landed_cost",
        product_id=ctx.product_id,
        order_id=ctx.order["id"],
        amount="20",
        reason="Freight",
    )
    assert lc.status_code == 201
    expected_before = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(expected_before[0]["total_cost_value"]) == Decimal("180")
    assert Decimal(expected_before[0]["moving_avg_cost"]) == Decimal("45")

    # Corrupt summary row directly in PostgreSQL
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE product_cost_summaries "
                "SET tracked_qty = 999, total_cost_value = 99999, moving_avg_cost = 99 "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(ctx.company_id), "pid": str(ctx.product_id)},
        )

    await rebuild_cost_summaries.rebuild_cost_summaries()
    rebuilt_1 = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert rebuilt_1 == expected_before

    # Delete summary row completely and rebuild again
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM product_cost_summaries "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(ctx.company_id), "pid": str(ctx.product_id)},
        )

    await rebuild_cost_summaries.rebuild_cost_summaries()
    rebuilt_2 = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert rebuilt_2 == expected_before

    # Ship all 4 units so tracked inventory reaches 0 while preserving moving_avg_cost = 45
    cust_id = await create_customer(client, ctx.company_id, "RBLDCUST")
    so = await create_sales_draft(
        client, ctx.company_id, cust_id, [sales_order_line(ctx.product_id, "4")]
    )
    assert (await confirm_sales_order(client, ctx.company_id, so["id"])).status_code == 200
    assert (await ship_order(client, ctx.company_id, so["id"])).status_code == 200

    # Force identical created_at and put SHIPMENT UUID before RECEIPT/LANDED_COST UUIDs;
    # deterministic qty_delta DESC ordering must still preserve moving_avg_cost = 45 at qty=0.
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE cost_moves "
                "SET created_at = '2026-11-24T00:00:00+00:00', "
                "    id = CASE move_type "
                "        WHEN 'SHIPMENT' THEN '00000000-0000-0000-0000-000000000001'::uuid "
                "        WHEN 'LANDED_COST' THEN 'ffffffff-ffff-ffff-ffff-fffffffffff1'::uuid "
                "        ELSE 'ffffffff-ffff-ffff-ffff-fffffffffff2'::uuid "
                "    END "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(ctx.company_id), "pid": str(ctx.product_id)},
        )
        await conn.execute(
            text(
                "UPDATE product_cost_summaries "
                "SET moving_avg_cost = 999 "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(ctx.company_id), "pid": str(ctx.product_id)},
        )

    await rebuild_cost_summaries.rebuild_cost_summaries()
    rebuilt_3 = await get_summaries(client, ctx.company_id, product_id=ctx.product_id)
    assert Decimal(rebuilt_3[0]["tracked_qty"]) == Decimal("0")
    assert Decimal(rebuilt_3[0]["total_cost_value"]) == Decimal("0")
    assert Decimal(rebuilt_3[0]["moving_avg_cost"]) == Decimal("45")
