"""tests/e2e/test_p2p_end_to_end.py — complete Procure-to-Pay step-by-step ledger proof."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.inventory._helpers import on_hand
from tests.ledger._helpers import create_account, create_period
from tests.payables._helpers import (
    allocate_payment,
    allocation,
    create_payment,
    get_ap_aging,
    get_trial_balance_by_code,
    issue_bill,
    void_bill,
)
from tests.purchase._helpers import (
    confirm_order,
    create_company,
    create_draft_order,
    create_product,
    create_supplier,
    order_line,
    receive_order,
)

_ZERO = Decimal("0.000000")


async def _account_balances(
    client: AsyncClient, company_id: uuid.UUID
) -> dict[str, tuple[Decimal, Decimal]]:
    tb = await get_trial_balance_by_code(client, company_id)
    return {
        code: (Decimal(row["total_debit"]), Decimal(row["total_credit"]))
        for code, row in tb.items()
    }


def _delta(
    before: dict[str, tuple[Decimal, Decimal]],
    after: dict[str, tuple[Decimal, Decimal]],
) -> dict[str, tuple[Decimal, Decimal]]:
    moved: dict[str, tuple[Decimal, Decimal]] = {}
    for code in set(before) | set(after):
        b_debit, b_credit = before.get(code, (_ZERO, _ZERO))
        a_debit, a_credit = after.get(code, (_ZERO, _ZERO))
        d_debit = a_debit - b_debit
        d_credit = a_credit - b_credit
        if d_debit != _ZERO or d_credit != _ZERO:
            moved[code] = (d_debit, d_credit)
    return moved


@pytest.mark.asyncio
async def test_full_p2p_flow_posts_and_clears_grir_and_ap_at_every_step(
    client: AsyncClient,
) -> None:
    # 1. Setup company, open UTC period, standard chart (1000/1300/2000/2110), supplier & SKU
    company_id = await create_company(client, "E2EP2P")
    today_utc = datetime.now(timezone.utc).date()  # noqa: UP017
    await create_period(client, company_id, today_utc.year, today_utc.month)
    for code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1300", "Inventory", "asset"),
        ("2000", "Accounts Payable", "liability"),
        ("2110", "Accrued Purchases", "liability"),
    ):
        await create_account(client, company_id, code, name, acct_type)

    supplier_id = await create_supplier(client, company_id, "E2EP2PSUP", payment_terms_days=30)
    product_id = await create_product(
        client, company_id, "E2EP2P-SKU", list_price="100", standard_cost="30"
    )

    snap_setup = await _account_balances(client, company_id)
    assert snap_setup == {}
    assert await on_hand(client, company_id, product_id) == Decimal("0")

    # 2. Draft & confirm PO -> ZERO ledger delta, ZERO stock delta
    po = await create_draft_order(
        client, company_id, supplier_id, [order_line(product_id, "4", unit_cost="35")]
    )
    po_total = Decimal(po["total"])
    assert po_total == Decimal("140.000000")
    assert (await confirm_order(client, company_id, po["id"])).status_code == 200
    snap_confirm = await _account_balances(client, company_id)
    assert _delta(snap_setup, snap_confirm) == {}
    assert await on_hand(client, company_id, product_id) == Decimal("0")

    # 3. Receive PO -> Dr 1300 Inventory / Cr 2110 Accrued Purchases + stock intake (+4)
    rcv = await receive_order(client, company_id, po["id"])
    assert rcv.status_code == 200, rcv.text
    snap_receive = await _account_balances(client, company_id)
    assert _delta(snap_confirm, snap_receive) == {
        "1300": (po_total, _ZERO),
        "2110": (_ZERO, po_total),
    }
    assert await on_hand(client, company_id, product_id) == Decimal("4")

    # 4. Issue vendor bill -> clears 2110 GR/IR into 2000 AP (Dr 2110 / Cr 2000)
    bill_resp = await issue_bill(client, company_id, po["id"], supplier_invoice_no="V-2026-01")
    assert bill_resp.status_code == 201, bill_resp.text
    bill = bill_resp.json()
    assert Decimal(bill["total"]) == po_total

    snap_bill = await _account_balances(client, company_id)
    assert _delta(snap_receive, snap_bill) == {
        "2110": (po_total, _ZERO),
        "2000": (_ZERO, po_total),
    }

    # Verify GR/IR 2110 netted to 0, and AP aging ties out non-zero to 2000
    tb_bill = await get_trial_balance_by_code(client, company_id)
    assert (
        Decimal(tb_bill["2110"]["total_credit"]) - Decimal(tb_bill["2110"]["total_debit"]) == _ZERO
    )
    ap_bal = Decimal(tb_bill["2000"]["total_credit"]) - Decimal(tb_bill["2000"]["total_debit"])
    aging_row = next(
        r
        for r in (await get_ap_aging(client, company_id)).json()
        if r["supplier_id"] == str(supplier_id)
    )
    assert Decimal(aging_row["net_total"]) == ap_bal == po_total

    # Void & re-bill cleanly resets and restores 2110 / 2000
    assert (await void_bill(client, company_id, bill["id"])).status_code == 200
    snap_void = await _account_balances(client, company_id)
    assert _delta(snap_bill, snap_void) == {
        "2000": (po_total, _ZERO),
        "2110": (_ZERO, po_total),
    }
    bill_2 = (
        await issue_bill(client, company_id, po["id"], supplier_invoice_no="V-2026-01")
    ).json()
    snap_rebill = await _account_balances(client, company_id)
    assert _delta(snap_void, snap_rebill) == {
        "2110": (po_total, _ZERO),
        "2000": (_ZERO, po_total),
    }

    # 5. Issue vendor payment -> Dr 2000 AP / Cr 1000 Cash
    pay_resp = await create_payment(client, company_id, supplier_id, po_total, "E2EP2P-PAY-1")
    assert pay_resp.status_code == 201, pay_resp.text
    payment = pay_resp.json()
    snap_pay = await _account_balances(client, company_id)
    assert _delta(snap_rebill, snap_pay) == {
        "2000": (po_total, _ZERO),
        "1000": (_ZERO, po_total),
    }

    # 6. Allocate payment to bill -> subledger only, zero GL delta, everything reconciles to 0
    alloc_resp = await allocate_payment(
        client, company_id, payment["id"], "E2EP2P-ALLOC-1", [allocation(bill_2["id"], po_total)]
    )
    assert alloc_resp.status_code == 200
    snap_alloc = await _account_balances(client, company_id)
    assert _delta(snap_pay, snap_alloc) == {}

    b2_final = (
        await client.get(
            f"/api/v1/payables/bills/{bill_2['id']}", headers=company_headers(company_id)
        )
    ).json()
    assert b2_final["status"] == "paid"
    assert Decimal(b2_final["settled_amount"]) == po_total

    final_tb = await get_trial_balance_by_code(client, company_id)
    total_debit = sum((Decimal(r["total_debit"]) for r in final_tb.values()), _ZERO)
    total_credit = sum((Decimal(r["total_credit"]) for r in final_tb.values()), _ZERO)
    assert total_debit == total_credit
    assert (
        Decimal(final_tb["2000"]["total_credit"]) - Decimal(final_tb["2000"]["total_debit"])
        == _ZERO
    )
    assert (
        Decimal(final_tb["2110"]["total_credit"]) - Decimal(final_tb["2110"]["total_debit"])
        == _ZERO
    )
    assert (await get_ap_aging(client, company_id)).json() == []
