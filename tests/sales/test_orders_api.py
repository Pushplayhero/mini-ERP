"""tests/sales/test_orders_api.py — order lifecycle CRUD + state machine (ADR-006).

Covers: draft creation + server-computed totals (with and without a client
`unit_price` override), draft editability, non-draft edit rejection, confirm
success + snapshot freezing + outbox/event emission, R2 (empty/zero-total
confirm 422), post-confirm immutability, cancel from draft/confirmed, cancel
of an already-cancelled order (409, this implementation's documented
choice — see `service.cancel_order`'s docstring), and cross-company
isolation for both orders and lines.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.masterdata.models import OutboxEvent
from tests.sales._helpers import (
    cancel_order,
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)

# ---------------------------------------------------------------------------
# Draft creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_order_computes_total_from_lines(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOA1")
    customer_id = await create_customer(client, company_id, "CUSTA1")
    product_id = await create_product(client, company_id, "SKU-A1", list_price="50")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "3", unit_price="20")]
    )

    assert order["status"] == "draft"
    assert Decimal(order["total"]) == Decimal("60")
    assert len(order["lines"]) == 1
    assert Decimal(order["lines"][0]["amount"]) == Decimal("60")
    assert order["order_no"].startswith("SO-")


@pytest.mark.asyncio
async def test_create_draft_order_defaults_unit_price_from_product_list_price(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "SOA2")
    customer_id = await create_customer(client, company_id, "CUSTA2")
    product_id = await create_product(client, company_id, "SKU-A2", list_price="42.5")

    order = await create_draft_order(client, company_id, customer_id, [order_line(product_id, "2")])

    line = order["lines"][0]
    assert Decimal(line["unit_price"]) == Decimal("42.5")
    assert Decimal(line["amount"]) == Decimal("85.0")
    assert Decimal(order["total"]) == Decimal("85.0")


@pytest.mark.asyncio
async def test_create_draft_order_can_start_with_zero_lines(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOA3")
    customer_id = await create_customer(client, company_id, "CUSTA3")

    order = await create_draft_order(client, company_id, customer_id, [])

    assert order["status"] == "draft"
    assert order["lines"] == []
    assert Decimal(order["total"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Draft editability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_order_can_be_edited_and_total_recomputed(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOB1")
    customer_id = await create_customer(client, company_id, "CUSTB1")
    product_id = await create_product(client, company_id, "SKU-B1", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )

    patch = await client.patch(
        f"/api/v1/sales-orders/{order['id']}",
        json={"lines": [order_line(product_id, "5", unit_price="10")]},
        headers={"X-Company-Id": str(company_id)},
    )
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert Decimal(body["total"]) == Decimal("50")
    assert len(body["lines"]) == 1
    assert body["lines"][0]["line_no"] == 1


@pytest.mark.asyncio
async def test_non_draft_order_edit_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOB2")
    customer_id = await create_customer(client, company_id, "CUSTB2")
    product_id = await create_product(client, company_id, "SKU-B2", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text

    patch = await client.patch(
        f"/api/v1/sales-orders/{order['id']}",
        json={"custom_data": {"note": "trying to sneak an edit in"}},
        headers={"X-Company-Id": str(company_id)},
    )
    assert patch.status_code == 409


# ---------------------------------------------------------------------------
# Confirm: success, snapshot freezing, event/outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_freezes_customer_snapshot_against_later_masterdata_edits(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "SOC1")
    customer_id = await create_customer(client, company_id, "CUSTC1")
    product_id = await create_product(client, company_id, "SKU-C1", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()
    assert confirmed["snapshot_customer_name"] == "CUSTC1 customer"

    rename = await client.patch(
        f"/api/v1/customers/{customer_id}",
        json={"name": "Renamed After Confirm"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert rename.status_code == 200, rename.text

    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert reread.json()["snapshot_customer_name"] == "CUSTC1 customer"


@pytest.mark.asyncio
async def test_confirm_publishes_event_and_writes_one_outbox_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(client, "SOC2")
    customer_id = await create_customer(client, company_id, "CUSTC2")
    product_id = await create_product(client, company_id, "SKU-C2", list_price="15")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "2", unit_price="15")]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text

    result = await db_session.execute(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "sales.order_confirmed",
            OutboxEvent.payload["source_id"].astext == order["id"],
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["company_id"] == str(company_id)
    assert rows[0].payload["customer_id"] == str(customer_id)
    assert Decimal(rows[0].payload["total"]) == Decimal("30")


@pytest.mark.asyncio
async def test_confirmed_order_cannot_be_confirmed_again(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOC3")
    customer_id = await create_customer(client, company_id, "CUSTC3")
    product_id = await create_product(client, company_id, "SKU-C3", list_price="5")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="5")]
    )
    first = await confirm_order(client, company_id, order["id"])
    assert first.status_code == 200, first.text

    second = await confirm_order(client, company_id, order["id"])
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# R2: empty / zero-total orders cannot confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_empty_order_is_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOD1")
    customer_id = await create_customer(client, company_id, "CUSTD1")

    order = await create_draft_order(client, company_id, customer_id, [])

    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 422

    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert reread.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_confirm_zero_total_order_is_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOD2")
    customer_id = await create_customer(client, company_id, "CUSTD2")
    product_id = await create_product(client, company_id, "SKU-D2", list_price="0")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="0")]
    )

    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 422

    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert reread.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_draft_order(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOE1")
    customer_id = await create_customer(client, company_id, "CUSTE1")

    order = await create_draft_order(client, company_id, customer_id, [])
    resp = await cancel_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_cancel_confirmed_order(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOE2")
    customer_id = await create_customer(client, company_id, "CUSTE2")
    product_id = await create_product(client, company_id, "SKU-E2", list_price="5")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="5")]
    )
    confirmed = await confirm_order(client, company_id, order["id"])
    assert confirmed.status_code == 200

    resp = await cancel_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_of_already_cancelled_order_is_409(client: AsyncClient) -> None:
    company_id = await create_company(client, "SOE3")
    customer_id = await create_customer(client, company_id, "CUSTE3")

    order = await create_draft_order(client, company_id, customer_id, [])
    first = await cancel_order(client, company_id, order["id"])
    assert first.status_code == 200

    second = await cancel_order(client, company_id, order["id"])
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Cross-company isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sales_order_cross_company_get_is_404(client: AsyncClient) -> None:
    company_a = await create_company(client, "SOFA1")
    company_b = await create_company(client, "SOFB1")
    customer_id = await create_customer(client, company_a, "CUSTFA1")

    order = await create_draft_order(client, company_a, customer_id, [])

    own = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_a)}
    )
    assert own.status_code == 200

    cross = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_b)}
    )
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_sales_order_list_never_leaks_across_companies(client: AsyncClient) -> None:
    company_a = await create_company(client, "SOFA2")
    company_b = await create_company(client, "SOFB2")
    customer_id = await create_customer(client, company_a, "CUSTFA2")

    await create_draft_order(client, company_a, customer_id, [])

    list_b = await client.get("/api/v1/sales-orders", headers={"X-Company-Id": str(company_b)})
    assert list_b.json() == []


@pytest.mark.asyncio
async def test_sales_order_cross_company_confirm_and_cancel_are_404(client: AsyncClient) -> None:
    company_a = await create_company(client, "SOFA3")
    company_b = await create_company(client, "SOFB3")
    customer_id = await create_customer(client, company_a, "CUSTFA3")
    product_id = await create_product(client, company_a, "SKU-FA3", list_price="10")

    order = await create_draft_order(
        client, company_a, customer_id, [order_line(product_id, "1", unit_price="10")]
    )

    cross_confirm = await confirm_order(client, company_b, order["id"])
    assert cross_confirm.status_code == 404

    cross_cancel = await cancel_order(client, company_b, order["id"])
    assert cross_cancel.status_code == 404

    # Still draft for company A.
    still_own = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_a)}
    )
    assert still_own.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_sales_order_lines_do_not_leak_across_companies(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.exceptions import TenancyContextError
    from app.core.tenancy import company_context
    from app.modules.sales.models import SalesOrderLine

    company_a = await create_company(client, "SOFA4")
    company_b = await create_company(client, "SOFB4")
    customer_id = await create_customer(client, company_a, "CUSTFA4")
    product_id = await create_product(client, company_a, "SKU-FA4", list_price="10")

    await create_draft_order(
        client, company_a, customer_id, [order_line(product_id, "1", unit_price="10")]
    )

    with company_context(company_b):
        result = await db_session.execute(select(SalesOrderLine))
        assert result.scalars().all() == []

    with pytest.raises(TenancyContextError):
        await db_session.execute(select(SalesOrderLine))
