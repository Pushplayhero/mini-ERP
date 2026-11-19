"""tests/purchase/test_receiving.py — PO goods receipt -> inventory + GR/IR ledger (ADR-014)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.cli import rebuild_stock_summary, replay_outbox
from app.core.authorization import actor_context
from app.modules.masterdata.models import OutboxEvent
from tests.conftest import TEST_ADMIN_ACTOR, company_headers
from tests.inventory._helpers import get_summary as inventory_get_summary
from tests.inventory._helpers import list_moves as inventory_list_moves
from tests.ledger._helpers import create_account, create_period
from tests.purchase._helpers import (
    cancel_order,
    confirm_order,
    create_company,
    create_draft_order,
    create_product,
    create_supplier,
    order_line,
    receive_order,
)


async def _get_trial_balance(client: AsyncClient, company_id: uuid.UUID) -> Response:
    return await client.get("/api/v1/reports/trial-balance", headers=company_headers(company_id))


async def _on_hand(client: AsyncClient, company_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    resp = await inventory_get_summary(client, company_id, product_id)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    return Decimal(rows[0]["on_hand"]) if rows else Decimal("0")


async def _setup_receivable_company(
    client: AsyncClient,
    code: str,
    *,
    standard_cost: str = "30",
    qty: str = "4",
    unit_cost: str | None = None,
    seed_accounts: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    company_id = await create_company(client, code)
    today = datetime.now(timezone.utc)  # noqa: UP017
    await create_period(client, company_id, today.year, today.month)
    if seed_accounts:
        await create_account(client, company_id, "1300", "Inventory", "asset")
        await create_account(client, company_id, "2110", "Accrued Purchases", "liability")

    supplier_id = await create_supplier(client, company_id, f"{code}SUP")
    product_id = await create_product(
        client, company_id, f"{code}-SKU", list_price="100", standard_cost=standard_cost
    )
    order = await create_draft_order(
        client,
        company_id,
        supplier_id,
        [order_line(product_id, qty, unit_cost=unit_cost)],
    )
    conf_resp = await confirm_order(client, company_id, order["id"])
    assert conf_resp.status_code == 200, conf_resp.text
    return company_id, product_id, supplier_id, conf_resp.json()


@pytest.mark.asyncio
async def test_receive_po_moves_stock_and_posts_grir_accrual(client: AsyncClient) -> None:
    """Happy path: receiving PO increases stock via RECEIPT and posts Dr 1300 / Cr 2110
    using the frozen PO total (even if product.standard_cost changes after confirmation).
    """
    company_id, product_id, _supplier_id, order = await _setup_receivable_company(
        client, "RCV1", standard_cost="25", qty="4", unit_cost="35"
    )
    # PO total is 4 * 35 = 140. Change product.standard_cost after confirmation —
    # GR/IR receipt valuation must still use the confirmed PO cost (140), not 4 * 99 = 396.
    await client.patch(
        f"/api/v1/products/{product_id}",
        json={"standard_cost": "99"},
        headers=company_headers(company_id),
    )

    resp = await receive_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    received = resp.json()
    assert received["status"] == "received"
    assert received["received_at"] is not None

    assert await _on_hand(client, company_id, product_id) == Decimal("4")

    moves_resp = await inventory_list_moves(client, company_id, product_id)
    assert moves_resp.status_code == 200
    moves = moves_resp.json()
    assert len(moves) == 1
    assert moves[0]["move_type"] == "receipt"
    assert Decimal(moves[0]["qty_delta"]) == Decimal("4")
    assert moves[0]["source_type"] == "purchase.goods_received"
    assert moves[0]["source_id"] == order["id"]

    tb_resp = await _get_trial_balance(client, company_id)
    assert tb_resp.status_code == 200
    by_code = {row["account_code"]: row for row in tb_resp.json()}
    assert Decimal(by_code["1300"]["total_debit"]) == Decimal("140")
    assert Decimal(by_code["1300"]["total_credit"]) == Decimal("0")
    assert Decimal(by_code["2110"]["total_debit"]) == Decimal("0")
    assert Decimal(by_code["2110"]["total_credit"]) == Decimal("140")


@pytest.mark.asyncio
async def test_receive_po_aggregates_repeated_product_lines_without_index_collision(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "RCVDUP")
    today = datetime.now(timezone.utc)  # noqa: UP017
    await create_period(client, company_id, today.year, today.month)
    await create_account(client, company_id, "1300", "Inventory", "asset")
    await create_account(client, company_id, "2110", "Accrued Purchases", "liability")

    supplier_id = await create_supplier(client, company_id, "SUPDUP")
    product_id = await create_product(client, company_id, "DUP-SKU", standard_cost="10")

    order = await create_draft_order(
        client,
        company_id,
        supplier_id,
        [
            order_line(product_id, "2", unit_cost="15"),
            order_line(product_id, "3", unit_cost="20"),
        ],
    )
    assert (await confirm_order(client, company_id, order["id"])).status_code == 200

    resp = await receive_order(client, company_id, order["id"])
    assert resp.status_code == 200, resp.text
    assert await _on_hand(client, company_id, product_id) == Decimal("5")

    moves = (await inventory_list_moves(client, company_id, product_id)).json()
    assert len(moves) == 1
    assert Decimal(moves[0]["qty_delta"]) == Decimal("5")

    tb_rows = (await _get_trial_balance(client, company_id)).json()
    tb = {row["account_code"]: row for row in tb_rows}
    assert Decimal(tb["1300"]["total_debit"]) == Decimal("90")  # 2*15 + 3*20
    assert Decimal(tb["2110"]["total_credit"]) == Decimal("90")


@pytest.mark.asyncio
async def test_receive_missing_grir_account_rolls_back_atomically(client: AsyncClient) -> None:
    """If 2110 Accrued Purchases is missing, ledger fails and rolls back stock move & PO state."""
    company_id, product_id, _supplier_id, order = await _setup_receivable_company(
        client, "RCVFAIL", standard_cost="20", qty="5", seed_accounts=False
    )
    # Seed only 1300, deliberately omitting 2110
    await create_account(client, company_id, "1300", "Inventory", "asset")

    resp = await receive_order(client, company_id, order["id"])
    assert resp.status_code == 422

    # Atomic rollback: stock remains 0, PO remains confirmed
    assert await _on_hand(client, company_id, product_id) == Decimal("0")
    po_resp = await client.get(
        f"/api/v1/purchase-orders/{order['id']}", headers=company_headers(company_id)
    )
    assert po_resp.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_receive_replay_outbox_and_rebuild_stock_summary_are_idempotent(
    client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    company_id, product_id, _supplier_id, order = await _setup_receivable_company(
        client, "RCVRPL", standard_cost="30", qty="6"
    )
    assert (await receive_order(client, company_id, order["id"])).status_code == 200
    assert await _on_hand(client, company_id, product_id) == Decimal("6")

    # Simulate un-replayed outbox event and run replay_outbox -> must be no-op
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.event_type == "purchase.goods_received")
        .values(dispatched_at=None)
    )
    await db_session.commit()

    replay_summary = await replay_outbox.replay_outbox()
    assert replay_summary.failed == 0

    assert await _on_hand(client, company_id, product_id) == Decimal("6")
    tb_rows = (await _get_trial_balance(client, company_id)).json()
    tb = {row["account_code"]: row for row in tb_rows}
    assert Decimal(tb["1300"]["total_debit"]) == Decimal("180")
    assert Decimal(tb["2110"]["total_credit"]) == Decimal("180")

    # Corrupt stock_summary and verify rebuild_stock_summary restores from RECEIPT stock_moves
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stock_summary SET on_hand = 999 "
                "WHERE company_id = :cid AND product_id = :pid"
            ),
            {"cid": str(company_id), "pid": str(product_id)},
        )
    assert await _on_hand(client, company_id, product_id) == Decimal("999")

    with actor_context(TEST_ADMIN_ACTOR):
        await rebuild_stock_summary.rebuild_stock_summary()

    assert await _on_hand(client, company_id, product_id) == Decimal("6")


@pytest.mark.asyncio
async def test_post_receive_state_guards_reject_double_receive_and_cancel(
    client: AsyncClient,
) -> None:
    company_id, _product_id, supplier_id, order = await _setup_receivable_company(
        client, "RCVGRD", standard_cost="10", qty="2"
    )
    p2 = await create_product(client, company_id, "GRD-SKU-2", standard_cost="10")
    draft_order = await create_draft_order(client, company_id, supplier_id, [order_line(p2, "1")])

    # Draft cannot be received
    assert (await receive_order(client, company_id, draft_order["id"])).status_code == 409

    # Receive confirmed -> 200
    assert (await receive_order(client, company_id, order["id"])).status_code == 200

    # Received cannot be received again or cancelled
    assert (await receive_order(client, company_id, order["id"])).status_code == 409
    assert (await cancel_order(client, company_id, order["id"])).status_code == 409

    # Outbox check
    resp_list = await client.get(
        f"/api/v1/purchase-orders/{order['id']}", headers=company_headers(company_id)
    )
    assert resp_list.json()["status"] == "received"


@pytest.mark.asyncio
async def test_goods_received_zero_cost_posting_skip_path(db_session: AsyncSession) -> None:
    """Verify ledger posting skips cleanly when a GoodsReceivedPayload carries total_cost=0."""
    from app.core.tenancy import company_context
    from app.modules.ledger.posting import GoodsReceivedPayload, handle_posting_event

    payload = GoodsReceivedPayload(
        company_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        order_no="PO-ZERO-001",
        supplier_id=uuid.uuid4(),
        lines=[],
        total_cost=Decimal("0"),
        received_at=datetime.now(timezone.utc),  # noqa: UP017
    )
    with company_context(payload.company_id):
        # Should skip before even resolving accounts 1300/2110
        await handle_posting_event(db_session, "purchase.goods_received", payload)
