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
from tests.conftest import company_headers
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
async def test_client_supplied_total_is_ignored_server_computes_it(client: AsyncClient) -> None:
    """Diff-review test-coverage fix: `total` is not a field on
    `SalesOrderCreate` at all (server-computed only, per ADR-006 Decision
    3), but nothing previously proved a client-supplied value is actually
    discarded rather than accidentally accepted.
    """
    company_id = await create_company(client, "SOA1B")
    customer_id = await create_customer(client, company_id, "CUSTA1B")
    product_id = await create_product(client, company_id, "SKU-A1B", list_price="50")

    response = await client.post(
        "/api/v1/sales-orders",
        json={
            "customer_id": str(customer_id),
            "lines": [order_line(product_id, "3", unit_price="20")],
            "total": "999999",
        },
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["total"]) == Decimal("60")


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
async def test_confirm_freezes_product_snapshot_against_later_masterdata_edits(
    client: AsyncClient,
) -> None:
    """Diff-review test-coverage fix: mirrors the customer-snapshot test
    above, but for the per-line product snapshot — nothing previously
    exercised `snapshot_sku`/`snapshot_product_name` at all.
    """
    company_id = await create_company(client, "SOC1B")
    customer_id = await create_customer(client, company_id, "CUSTC1B")
    product_id = await create_product(client, company_id, "SKU-C1B", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()
    assert confirmed["lines"][0]["snapshot_sku"] == "SKU-C1B"
    assert confirmed["lines"][0]["snapshot_product_name"] == "SKU-C1B widget"

    rename = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"name": "Renamed After Confirm"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert rename.status_code == 200, rename.text

    reread = await client.get(
        f"/api/v1/sales-orders/{order['id']}", headers={"X-Company-Id": str(company_id)}
    )
    assert reread.json()["lines"][0]["snapshot_product_name"] == "SKU-C1B widget"


@pytest.mark.asyncio
async def test_confirm_reprices_non_override_line_to_current_product_price(
    client: AsyncClient,
) -> None:
    """Diff-review regression (ADR-006 Decision 3): a line whose
    `unit_price` was never explicitly set by the client must reprice to the
    product's *current* `list_price` at confirm time — "a draft that sat
    for a week confirms at current prices unless lines carried manual
    overrides". Before the fix, `confirm_order` never touched pricing at
    all; the line kept whatever price was resolved (and frozen) at draft
    creation, even after the product's price changed.
    """
    company_id = await create_company(client, "SOC1C")
    customer_id = await create_customer(client, company_id, "CUSTC1C")
    product_id = await create_product(client, company_id, "SKU-C1C", list_price="10")

    # No `unit_price` supplied -> defaults from product.list_price (not an
    # override).
    order = await create_draft_order(client, company_id, customer_id, [order_line(product_id, "2")])
    assert Decimal(order["lines"][0]["unit_price"]) == Decimal("10")
    assert order["lines"][0]["unit_price_is_override"] is False

    reprice = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"list_price": "25"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert reprice.status_code == 200, reprice.text

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()

    assert Decimal(confirmed["lines"][0]["unit_price"]) == Decimal("25")
    assert Decimal(confirmed["lines"][0]["amount"]) == Decimal("50")  # qty 2 * 25
    assert Decimal(confirmed["total"]) == Decimal("50")


@pytest.mark.asyncio
async def test_confirm_preserves_manual_unit_price_override_despite_price_change(
    client: AsyncClient,
) -> None:
    """Diff-review regression companion: the flip side of the reprice test
    above — a line whose `unit_price` *was* explicitly set by the client
    must NOT be touched by confirm-time repricing, no matter what the
    product's current price is.
    """
    company_id = await create_company(client, "SOC1D")
    customer_id = await create_customer(client, company_id, "CUSTC1D")
    product_id = await create_product(client, company_id, "SKU-C1D", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "2", unit_price="8")]
    )
    assert order["lines"][0]["unit_price_is_override"] is True

    reprice = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"list_price": "25"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert reprice.status_code == 200, reprice.text

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()

    assert Decimal(confirmed["lines"][0]["unit_price"]) == Decimal("8")
    assert Decimal(confirmed["lines"][0]["amount"]) == Decimal("16")  # qty 2 * 8
    assert Decimal(confirmed["total"]) == Decimal("16")


@pytest.mark.asyncio
async def test_confirm_rejects_when_reprice_drops_total_to_zero(client: AsyncClient) -> None:
    """Diff-review regression (round 2): R2's "positive total" check must
    run *after* repricing, against the recomputed total — not the stale
    pre-reprice value. A non-override line priced positively at draft time
    whose product is later repriced to 0 must still be rejected at confirm,
    even though the order's *stale* total was positive.
    """
    company_id = await create_company(client, "SOC1E")
    customer_id = await create_customer(client, company_id, "CUSTC1E")
    product_id = await create_product(client, company_id, "SKU-C1E", list_price="10")

    order = await create_draft_order(client, company_id, customer_id, [order_line(product_id, "1")])
    assert Decimal(order["total"]) == Decimal("10")

    reprice = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"list_price": "0"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert reprice.status_code == 200, reprice.text

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 422, confirm_resp.text


@pytest.mark.asyncio
async def test_confirm_accepts_when_reprice_raises_total_above_zero(client: AsyncClient) -> None:
    """Diff-review regression (round 2), the flip side: a non-override line
    priced at 0 at draft time whose product is later repriced above 0 must
    be allowed to confirm — the stale (0) total must not reject it before
    repricing runs.
    """
    company_id = await create_company(client, "SOC1F")
    customer_id = await create_customer(client, company_id, "CUSTC1F")
    product_id = await create_product(client, company_id, "SKU-C1F", list_price="0")

    order = await create_draft_order(client, company_id, customer_id, [order_line(product_id, "1")])
    assert Decimal(order["total"]) == Decimal("0")

    reprice = await client.patch(
        f"/api/v1/products/{product_id}",
        json={"list_price": "15"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert reprice.status_code == 200, reprice.text

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert Decimal(confirm_resp.json()["total"]) == Decimal("15")


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


@pytest.mark.asyncio
async def test_confirming_a_cancelled_order_is_409(client: AsyncClient) -> None:
    """Diff-review test-coverage fix: the state machine's only legal
    transitions into `confirmed` are from `draft`; a cancelled order must
    not be confirmable, but nothing previously exercised this specific
    illegal edge (only cancel-of-cancelled and confirm-of-confirmed were
    covered).
    """
    company_id = await create_company(client, "SOE4")
    customer_id = await create_customer(client, company_id, "CUSTE4")
    product_id = await create_product(client, company_id, "SKU-E4", list_price="10")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    cancel_resp = await cancel_order(client, company_id, order["id"])
    assert cancel_resp.status_code == 200

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 409, confirm_resp.text


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


@pytest.mark.asyncio
async def test_confirmed_order_without_snapshot_is_rejected_by_db_check(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Diff-review regression (migration 0006): a bypass writer inserting a
    `CONFIRMED` order directly (skipping `service.confirm_order`, which
    always freezes the customer snapshot first) must be rejected at the DB
    layer by `ck_sales_orders_confirmed_has_snapshot`, not silently allowed
    to persist a confirmed order with no snapshot.
    """
    from sqlalchemy.exc import DBAPIError

    from app.core.tenancy import company_context
    from app.modules.sales.models import SalesOrder, SalesOrderStatus

    company_id = await create_company(client, "SOSNAP1")
    customer_id = await create_customer(client, company_id, "CUSTSNAP1")

    with company_context(company_id):
        db_session.add(
            SalesOrder(
                company_id=company_id,
                order_no="SO-2026-999999",
                customer_id=customer_id,
                status=SalesOrderStatus.CONFIRMED,
                currency_code="TWD",
                total=Decimal("0"),
                snapshot_customer_code=None,
                snapshot_customer_name=None,
            )
        )
        with pytest.raises(DBAPIError):
            await db_session.commit()
        await db_session.rollback()


@pytest.mark.asyncio
async def test_shipped_order_without_snapshot_is_rejected_by_db_check(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Diff-review regression (round 2): the state machine only reaches
    `SHIPPED` via `CONFIRMED`, so a `SHIPPED` order with no snapshot is the
    same integrity violation as the `CONFIRMED` case above — a bypass
    writer must not be able to reach it by inserting `SHIPPED` directly.
    """
    from sqlalchemy.exc import DBAPIError

    from app.core.tenancy import company_context
    from app.modules.sales.models import SalesOrder, SalesOrderStatus

    company_id = await create_company(client, "SOSNAP2")
    customer_id = await create_customer(client, company_id, "CUSTSNAP2")

    with company_context(company_id):
        db_session.add(
            SalesOrder(
                company_id=company_id,
                order_no="SO-2026-999998",
                customer_id=customer_id,
                status=SalesOrderStatus.SHIPPED,
                currency_code="TWD",
                total=Decimal("0"),
                snapshot_customer_code=None,
                snapshot_customer_name=None,
            )
        )
        with pytest.raises(DBAPIError):
            await db_session.commit()
        await db_session.rollback()
