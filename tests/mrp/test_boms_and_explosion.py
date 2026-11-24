"""BOM CRUD, active version switching, concurrent DAG cycle detection & cost explosion tests."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import company_headers
from tests.mrp._helpers import (
    bom_line,
    create_bom,
    create_product,
    explode_bom,
    seed_stock_via_po,
    setup_mrp_company,
    update_bom,
)


@pytest.mark.asyncio
async def test_bom_crud_active_version_switch_and_multilevel_explosion(
    client: AsyncClient,
) -> None:
    """Create 2-level BOM with scrap rate, switch active BOM version, and verify MWA roll-up."""
    ctx = await setup_mrp_company(client, "BOMCO1")
    raw_a = await create_product(client, ctx.company_id, "RAW-A", standard_cost="10")
    raw_b = await create_product(client, ctx.company_id, "RAW-B", standard_cost="20")
    sub_s = await create_product(client, ctx.company_id, "SUB-S", standard_cost="50")
    fin_f = await create_product(client, ctx.company_id, "FIN-F", standard_cost="200")

    # Purchase RAW-A at override cost 12 and RAW-B at 25 so MWA > standard_cost
    await seed_stock_via_po(
        client,
        ctx.company_id,
        ctx.supplier_id,
        [(raw_a, "10", "12"), (raw_b, "10", "25")],
    )

    # Sub-assembly SUB-S needs 2 RAW-A (no scrap) + 1 RAW-B (10% scrap -> eff 1.1)
    bom_sub = await create_bom(
        client,
        ctx.company_id,
        code="BOM-SUB-1",
        product_id=sub_s,
        output_qty="1",
        lines=[
            bom_line(raw_a, "2"),
            bom_line(raw_b, "1", scrap_rate="0.1"),
        ],
    )
    assert bom_sub.status_code == 201, bom_sub.text

    # Finished FIN-F BOM-V1: 2 SUB-S -> 2 * (2*12 + 1.1*25) = 2 * (24 + 27.5) = 103
    bom_fin_1 = await create_bom(
        client,
        ctx.company_id,
        code="BOM-FIN-V1",
        product_id=fin_f,
        output_qty="1",
        lines=[bom_line(sub_s, "2")],
    )
    assert bom_fin_1.status_code == 201, bom_fin_1.text
    fin_1_id = bom_fin_1.json()["id"]

    # Creating a second ACTIVE BOM for FIN-F conflicts (409 uq_boms_company_product_active)
    conflict_bom = await create_bom(
        client,
        ctx.company_id,
        code="BOM-FIN-V2",
        product_id=fin_f,
        is_active=True,
        lines=[bom_line(sub_s, "3")],
    )
    assert conflict_bom.status_code == 409

    # Create inactive V2, deactivate V1, activate V2 -> succeeds cleanly
    bom_fin_2 = await create_bom(
        client,
        ctx.company_id,
        code="BOM-FIN-V2",
        product_id=fin_f,
        is_active=False,
        lines=[bom_line(sub_s, "3")],
    )
    assert bom_fin_2.status_code == 201, bom_fin_2.text
    fin_2_id = bom_fin_2.json()["id"]

    assert (
        await update_bom(client, ctx.company_id, fin_1_id, {"is_active": False})
    ).status_code == 200
    assert (
        await update_bom(client, ctx.company_id, fin_2_id, {"is_active": True})
    ).status_code == 200

    # Explode BOM-FIN-V1 for qty=2 (even though inactive, still explodes against active sub-BOMs!)
    # Effective SUB-S = 4 -> 8 RAW-A (@12=96) + 4.4 RAW-B (@25=110) => total MWA rolled-up = 206
    exp = await explode_bom(client, ctx.company_id, fin_1_id, qty="2")
    assert exp.status_code == 200, exp.text
    body = exp.json()
    assert Decimal(body["requested_qty"]) == Decimal("2")
    assert Decimal(body["total_rolled_up_cost"]) == Decimal("206")
    assert Decimal(body["unit_rolled_up_cost"]) == Decimal("103")
    # standard_cost roll-up: 8*10 + 4.4*20 = 80 + 88 = 168
    assert Decimal(body["total_standard_material_cost"]) == Decimal("168")
    assert [n["level"] for n in body["nodes"]] == [1, 2, 2]

    # List active_only returns BOM-SUB-1 and BOM-FIN-V2 only
    listed = await client.get(
        "/api/v1/mrp/boms", params={"active_only": "true"}, headers=company_headers(ctx.company_id)
    )
    assert listed.status_code == 200
    assert {b["code"] for b in listed.json()} == {"BOM-SUB-1", "BOM-FIN-V2"}


@pytest.mark.asyncio
async def test_bom_cycle_detection_blocks_self_indirect_and_concurrent_cycles(
    client: AsyncClient,
) -> None:
    """Reject A->A, 3-level A->B->C->A, and concurrent A->B + B->A race (write-skew proof)."""
    from app.main import app

    ctx = await setup_mrp_company(client, "CYCCO1")
    prod_a = await create_product(client, ctx.company_id, "CYC-A")
    prod_b = await create_product(client, ctx.company_id, "CYC-B")
    prod_c = await create_product(client, ctx.company_id, "CYC-C")

    # 1. Self-loop A -> A rejected 422
    self_resp = await create_bom(
        client,
        ctx.company_id,
        code="SELF-A",
        product_id=prod_a,
        lines=[bom_line(prod_a, "1")],
    )
    assert self_resp.status_code == 422

    # 2. 3-level cycle: A -> B, B -> C, then C -> A rejected 422
    assert (
        await create_bom(
            client, ctx.company_id, code="BOM-A", product_id=prod_a, lines=[bom_line(prod_b, "1")]
        )
    ).status_code == 201
    assert (
        await create_bom(
            client, ctx.company_id, code="BOM-B", product_id=prod_b, lines=[bom_line(prod_c, "1")]
        )
    ).status_code == 201
    cyc_resp = await create_bom(
        client, ctx.company_id, code="BOM-C", product_id=prod_c, lines=[bom_line(prod_a, "1")]
    )
    assert cyc_resp.status_code == 422
    assert "Circular BOM dependency" in cyc_resp.text

    # 3. Concurrent write-skew race: D -> E vs E -> D simultaneously
    prod_d = await create_product(client, ctx.company_id, "CYC-D")
    prod_e = await create_product(client, ctx.company_id, "CYC-E")

    async def _post_edge(code: str, parent: uuid.UUID, child: uuid.UUID) -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            r = await create_bom(
                ac, ctx.company_id, code=code, product_id=parent, lines=[bom_line(child, "1")]
            )
            return r.status_code

    statuses = await asyncio.gather(
        _post_edge("RACE-DE", prod_d, prod_e),
        _post_edge("RACE-ED", prod_e, prod_d),
    )
    assert sorted(statuses) == [201, 422]
