"""tests/inventory/test_adjustments.py — manual intake + queries (ADR-007).

Covers the Phase 1 manual-adjustment stand-in end to end: positive/negative
adjustments, the `on_hand >= 0` floor (409, no partial write), `reason`
being mandatory, summary/move query correctness, and cross-company
isolation for both the summary and moves endpoints.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.inventory._helpers import (
    create_adjustment,
    create_company,
    create_product,
    get_summary,
    list_moves,
    on_hand,
)


@pytest.mark.asyncio
async def test_positive_adjustment_increases_on_hand(client: AsyncClient) -> None:
    company_id = await create_company(client, "INV1")
    product_id = await create_product(client, company_id, "INV-SKU-1")

    resp = await create_adjustment(client, company_id, product_id, "10", reason="initial stock")
    assert resp.status_code == 201, resp.text
    move = resp.json()
    # The immediate POST response is built from the in-memory object right
    # after `flush()`/`commit()`, not re-fetched from the DB (same
    # non-refreshing convention `masterdata.service.create_product` uses for
    # `list_price` — see `tests/masterdata/test_products_and_accounts_api.py`
    # for the analogous precision note), so it carries whatever precision
    # the *request* had rather than the column's full `NUMERIC(20,6)`
    # precision. Compare numerically, not as a zero-padded string.
    assert Decimal(move["qty_delta"]) == Decimal("10")
    assert move["move_type"] == "adjustment"
    assert move["source_type"] is None
    assert move["source_id"] is None
    assert move["reason"] == "initial stock"

    assert await on_hand(client, company_id, product_id) == Decimal("10")


@pytest.mark.asyncio
async def test_negative_adjustment_decreases_on_hand(client: AsyncClient) -> None:
    company_id = await create_company(client, "INV2")
    product_id = await create_product(client, company_id, "INV-SKU-2")

    await create_adjustment(client, company_id, product_id, "10", reason="initial stock")
    resp = await create_adjustment(client, company_id, product_id, "-4", reason="damaged")
    assert resp.status_code == 201, resp.text

    assert await on_hand(client, company_id, product_id) == Decimal("6")


@pytest.mark.asyncio
async def test_negative_adjustment_below_zero_is_409_and_does_not_write(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "INV3")
    product_id = await create_product(client, company_id, "INV-SKU-3")

    await create_adjustment(client, company_id, product_id, "5", reason="initial stock")
    resp = await create_adjustment(client, company_id, product_id, "-6", reason="oops")
    assert resp.status_code == 409, resp.text

    # No partial write: on_hand is unchanged, and no move was recorded for
    # the rejected adjustment.
    assert await on_hand(client, company_id, product_id) == Decimal("5")
    moves_resp = await list_moves(client, company_id, product_id)
    assert len(moves_resp.json()) == 1


@pytest.mark.asyncio
async def test_adjustment_reason_is_required(client: AsyncClient) -> None:
    company_id = await create_company(client, "INV4")
    product_id = await create_product(client, company_id, "INV-SKU-4")

    resp = await client.post(
        "/api/v1/inventory/adjustments",
        json={"product_id": str(product_id), "qty_delta": "5"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_adjustment_qty_delta_zero_is_rejected(client: AsyncClient) -> None:
    company_id = await create_company(client, "INV5")
    product_id = await create_product(client, company_id, "INV-SKU-5")

    resp = await create_adjustment(client, company_id, product_id, "0", reason="no-op")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_adjustment_unknown_product_is_422(client: AsyncClient) -> None:
    import uuid

    company_id = await create_company(client, "INV6")
    resp = await create_adjustment(client, company_id, uuid.uuid4(), "1", reason="ghost product")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_summary_and_moves_queries(client: AsyncClient) -> None:
    company_id = await create_company(client, "INV7")
    product_a = await create_product(client, company_id, "INV-SKU-7A")
    product_b = await create_product(client, company_id, "INV-SKU-7B")

    await create_adjustment(client, company_id, product_a, "3", reason="a in")
    await create_adjustment(client, company_id, product_a, "2", reason="a in again")
    await create_adjustment(client, company_id, product_b, "7", reason="b in")

    all_summary = await get_summary(client, company_id)
    assert all_summary.status_code == 200
    by_product = {row["product_id"]: row["on_hand"] for row in all_summary.json()}
    assert by_product[str(product_a)] == "5.000000"
    assert by_product[str(product_b)] == "7.000000"

    filtered_summary = await get_summary(client, company_id, product_a)
    assert len(filtered_summary.json()) == 1
    assert filtered_summary.json()[0]["on_hand"] == "5.000000"

    moves_a = await list_moves(client, company_id, product_a)
    assert len(moves_a.json()) == 2
    all_moves = await list_moves(client, company_id)
    assert len(all_moves.json()) == 3


@pytest.mark.asyncio
async def test_inventory_cross_company_isolation(client: AsyncClient) -> None:
    company_a = await create_company(client, "INV8A")
    company_b = await create_company(client, "INV8B")
    product_a = await create_product(client, company_a, "INV-SKU-8A")

    await create_adjustment(client, company_a, product_a, "9", reason="a stock")

    # Company B sees nothing of company A's stock, even querying by A's
    # product_id explicitly.
    summary_b = await get_summary(client, company_b)
    assert summary_b.json() == []
    summary_b_filtered = await get_summary(client, company_b, product_a)
    assert summary_b_filtered.json() == []

    moves_b = await list_moves(client, company_b)
    assert moves_b.json() == []

    # Company B cannot adjust a product that belongs to company A.
    cross_adjust = await create_adjustment(client, company_b, product_a, "1", reason="cross-tenant")
    assert cross_adjust.status_code == 422
