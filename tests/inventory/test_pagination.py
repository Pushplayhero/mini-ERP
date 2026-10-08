from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import company_headers
from tests.inventory._helpers import create_adjustment, create_company, create_product


@pytest.mark.asyncio
async def test_inventory_lists_return_stable_bounded_pages(client: AsyncClient) -> None:
    company_id = await create_company(client, "INVPAGE1")
    product_a = await create_product(client, company_id, "INV-PAGE-A")
    product_b = await create_product(client, company_id, "INV-PAGE-B")

    await create_adjustment(client, company_id, product_a, "5", reason="page seed 1")
    await create_adjustment(client, company_id, product_a, "1", reason="page seed 2")
    await create_adjustment(client, company_id, product_b, "2", reason="page seed 3")

    headers = company_headers(company_id)
    summary_pages = []
    move_pages = []
    summary_cursor = None
    move_cursor = None
    for _ in range(2):
        summary_params = {"limit": 2}
        if summary_cursor is not None:
            summary_params["after_product_id"] = summary_cursor
        move_params = {"limit": 2}
        if move_cursor is not None:
            move_params.update(move_cursor)
        summary_pages.append(
            await client.get(
                "/api/v1/inventory/summary",
                params=summary_params,
                headers=headers,
            )
        )
        move_pages.append(
            await client.get(
                "/api/v1/inventory/moves",
                params=move_params,
                headers=headers,
            )
        )
        if summary_pages[-1].json():
            summary_cursor = summary_pages[-1].json()[-1]["product_id"]
        if move_pages[-1].json():
            last_move = move_pages[-1].json()[-1]
            move_cursor = {
                "after_created_at": last_move["created_at"],
                "after_id": last_move["id"],
            }

    assert all(response.status_code == 200 for response in [*summary_pages, *move_pages])
    assert all(isinstance(response.json(), list) for response in [*summary_pages, *move_pages])

    summary_rows = [row for response in summary_pages for row in response.json()]
    assert [row["product_id"] for row in summary_rows] == sorted([str(product_a), str(product_b)])
    assert len(summary_pages[0].json()) == 2
    assert len(summary_pages[1].json()) == 0

    move_rows = [row for response in move_pages for row in response.json()]
    all_moves = await client.get("/api/v1/inventory/moves", headers=headers)
    assert all_moves.status_code == 200
    assert len(move_rows) == len(all_moves.json()) == 3
    assert len({row["id"] for row in move_rows}) == 3
    assert [row["id"] for row in move_rows] == [row["id"] for row in all_moves.json()]
    assert len(move_pages[0].json()) == 2
    assert len(move_pages[1].json()) == 1


@pytest.mark.asyncio
async def test_inventory_list_endpoints_reject_out_of_range_pagination(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "INVPAGE2")
    headers = company_headers(company_id)

    for path in ("/api/v1/inventory/summary", "/api/v1/inventory/moves"):
        oversized_page = await client.get(path, params={"limit": 501}, headers=headers)
        invalid_page_size = await client.get(path, params={"limit": 0}, headers=headers)

        assert oversized_page.status_code == 422
        assert invalid_page_size.status_code == 422


@pytest.mark.asyncio
async def test_inventory_list_pagination_preserves_product_filter(client: AsyncClient) -> None:
    company_id = await create_company(client, "INVPAGE3")
    product_id = await create_product(client, company_id, "INV-PAGE-FILTER")

    await create_adjustment(client, company_id, product_id, "1", reason="first")
    await create_adjustment(client, company_id, product_id, "1", reason="second")

    headers = company_headers(company_id)
    summary_page = await client.get(
        "/api/v1/inventory/summary",
        params={"product_id": str(product_id), "limit": 1},
        headers=headers,
    )
    move_page = await client.get(
        "/api/v1/inventory/moves",
        params={"product_id": str(product_id), "limit": 1},
        headers=headers,
    )

    assert summary_page.status_code == 200
    assert len(summary_page.json()) == 1
    assert summary_page.json()[0]["product_id"] == str(product_id)
    assert move_page.status_code == 200
    assert len(move_page.json()) == 1
    assert move_page.json()[0]["product_id"] == str(product_id)
    assert uuid.UUID(move_page.json()[0]["id"])


@pytest.mark.asyncio
async def test_inventory_moves_reject_partial_cursor(client: AsyncClient) -> None:
    company_id = await create_company(client, "INVPAGE4")
    headers = company_headers(company_id)

    response = await client.get(
        "/api/v1/inventory/moves",
        params={"after_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_inventory_moves_reject_naive_and_unrepresentable_cursor_times(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "INVPAGE5")
    headers = company_headers(company_id)
    cursor_id = str(uuid.uuid4())

    for timestamp in ("0001-01-01T00:00:00", "0001-01-01T00:00:00+14:00"):
        response = await client.get(
            "/api/v1/inventory/moves",
            params={"after_created_at": timestamp, "after_id": cursor_id},
            headers=headers,
        )

        assert response.status_code == 422


@pytest.mark.asyncio
async def test_inventory_moves_cursor_handles_equal_timestamps(
    client: AsyncClient, db_engine
) -> None:
    company_id = await create_company(client, "INVPAGE6")
    product_id = await create_product(client, company_id, "INV-PAGE-TIE")
    for index in range(3):
        await create_adjustment(client, company_id, product_id, "1", reason=f"tie {index}")

    timestamp = datetime(2026, 10, 8, 4, 0, tzinfo=UTC)
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE stock_moves SET created_at = :created_at WHERE product_id = :product_id"),
            {"created_at": timestamp, "product_id": product_id},
        )

    headers = company_headers(company_id)
    first_page = await client.get("/api/v1/inventory/moves", params={"limit": 2}, headers=headers)
    last_move = first_page.json()[-1]
    second_page = await client.get(
        "/api/v1/inventory/moves",
        params={
            "limit": 2,
            "after_created_at": last_move["created_at"],
            "after_id": last_move["id"],
        },
        headers=headers,
    )

    assert first_page.status_code == second_page.status_code == 200
    first_ids = [row["id"] for row in first_page.json()]
    second_ids = [row["id"] for row in second_page.json()]
    assert len(first_ids) == 2
    assert len(second_ids) == 1
    assert len(set(first_ids + second_ids)) == 3
    assert first_ids + second_ids == sorted(first_ids + second_ids)
