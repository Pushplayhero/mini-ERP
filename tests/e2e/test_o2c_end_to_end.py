"""tests/e2e/test_o2c_end_to_end.py — the complete order-to-cash line, in

one test, against a real PostgreSQL (Week 7 hardening brief, Decision 1).

Every other test in this repo proves one module's behavior in isolation;
this is the one place that proves the whole declarative posting engine
turns a real O2C sequence into correct double-entry bookkeeping, step by
step — create → confirm → ship → invoice → pay → allocate — with the
accounting invariant checked *at each step*, not just at the end.

**Why per-step delta assertions, not just a final "balances" check**
(Codex v1 P2 on the Week 7 brief): a bare "trial balance still balances"
after some step would pass a balanced-but-*wrong* entry (e.g. COGS posted
to the wrong account, for the wrong amount, as long as debit==credit
somewhere). Instead every step snapshots per-account debit/credit totals
before and after (`_account_balances` via the trial-balance query) and
asserts the exact *delta set* — precisely which accounts moved, and by how
much on which side — so a step that posts the right amount to the wrong
account, or the wrong amount to the right account, fails loudly.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.conftest import company_headers
from tests.inventory._helpers import create_adjustment, on_hand
from tests.ledger._helpers import create_account, create_period
from tests.receivables._helpers import (
    allocate_payment,
    allocation,
    create_payment,
    get_ar_aging,
    get_trial_balance_by_code,
    issue_invoice,
)
from tests.sales._helpers import (
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
    ship_order,
)

_ZERO = Decimal("0.000000")


async def _account_balances(
    client: AsyncClient, company_id: uuid.UUID
) -> dict[str, tuple[Decimal, Decimal]]:
    """`{account_code: (total_debit, total_credit)}` — a snapshot of every

    account with any posted activity so far, via the same trial-balance
    query every report in this repo relies on. An account with no activity
    at all simply doesn't appear (ADR-005 Decision 4: the trial balance is
    a `GROUP BY` over `journal_lines`, not a row-per-account table).
    """
    tb = await get_trial_balance_by_code(client, company_id)
    return {
        code: (Decimal(row["total_debit"]), Decimal(row["total_credit"]))
        for code, row in tb.items()
    }


def _delta(
    before: dict[str, tuple[Decimal, Decimal]],
    after: dict[str, tuple[Decimal, Decimal]],
) -> dict[str, tuple[Decimal, Decimal]]:
    """Which accounts moved, and by how much, between two snapshots.

    Only accounts with a nonzero debit or credit delta appear — this is
    the "exact delta set" the module docstring above explains the
    rationale for. Comparing this dict against a literal expected dict is
    the whole assertion mechanism this test relies on.
    """
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
async def test_full_o2c_flow_posts_correctly_at_every_step(client: AsyncClient) -> None:
    # ------------------------------------------------------------------
    # 1. Setup: company, open period, standard chart, customer (with a
    #    real, generous credit limit so the credit-limit plugin's hook
    #    actually runs on confirm, not just no-ops on the `credit_limit ==
    #    0` sentinel), product with a nonzero standard_cost (so COGS is a
    #    real, checkable number), seeded stock.
    # ------------------------------------------------------------------
    company_id = await create_company(client, "E2EO2C")
    # Derived from the real clock, not hard-coded (Codex diff review
    # 2026-08-15 finding): every event this flow posts (ship, invoice,
    # payment) defaults its own date to `date.today()`/`datetime.now()`
    # (`sales.service.ship_order`, `receivables.service.create_invoice`/
    # `create_payment`) — a hard-coded period would silently stop matching
    # the moment real wall-clock time crosses into the next month, making
    # this flagship test permanently time-bomb-expiring instead of
    # evergreen.
    today = date.today()
    await create_period(client, company_id, today.year, today.month)
    for code, name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("1300", "Inventory", "asset"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
    ):
        await create_account(client, company_id, code, name, acct_type)

    customer_id = await create_customer(client, company_id, "E2EO2CCUST", credit_limit="100000")
    product_id = await create_product(
        client, company_id, "E2EO2C-SKU", list_price="100", standard_cost="30"
    )
    adjust_resp = await create_adjustment(client, company_id, product_id, "10", "initial stock")
    assert adjust_resp.status_code == 201, adjust_resp.text

    snap_setup = await _account_balances(client, company_id)
    assert snap_setup == {}, "chart-of-accounts creation and stock seeding post nothing"
    stock_start = await on_hand(client, company_id, product_id)
    assert stock_start == Decimal("10")

    # ------------------------------------------------------------------
    # 2. Create + confirm the order: NO ledger delta, NO stock delta —
    #    confirming reserves nothing and posts nothing; only shipping
    #    does. The credit-limit plugin runs during confirm (exercising
    #    that hook path) but the order (200) is far inside the limit
    #    (100000), so it doesn't veto.
    # ------------------------------------------------------------------
    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "2", unit_price="100")]
    )
    order_total = Decimal(order["total"])
    assert order_total == Decimal("200.000000")

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text

    snap_after_confirm = await _account_balances(client, company_id)
    assert _delta(snap_setup, snap_after_confirm) == {}, "confirm must post nothing to the ledger"
    assert (
        await on_hand(client, company_id, product_id) == stock_start
    ), "confirm must not touch stock"

    # ------------------------------------------------------------------
    # 3. Ship: the ONLY journal delta is Dr 5000 (COGS) / Cr 1300
    #    (Inventory) for qty * standard_cost; stock deducted by the
    #    shipped qty, nothing else moves.
    # ------------------------------------------------------------------
    ship_resp = await ship_order(client, company_id, order["id"])
    assert ship_resp.status_code == 200, ship_resp.text

    snap_after_ship = await _account_balances(client, company_id)
    cogs = Decimal("60.000000")  # qty 2 * standard_cost 30
    assert _delta(snap_after_confirm, snap_after_ship) == {
        "5000": (cogs, _ZERO),
        "1300": (_ZERO, cogs),
    }
    assert await on_hand(client, company_id, product_id) == stock_start - Decimal("2")

    # ------------------------------------------------------------------
    # 4. Issue invoice: the ONLY journal delta is Dr 1100 (AR) / Cr 4000
    #    (Revenue) for the order total. This is also where the
    #    LOAD-BEARING tie-out assertion lives (Codex v1 P2 on the Week 7
    #    brief): a non-zero property proof, not the weak `0 == 0` a fully
    #    settled invoice would give at the end — ar-aging's net for this
    #    customer must equal the ledger's own 1100 balance, which must
    #    equal the invoice total (ADR-008 Decision 5's tie-out property,
    #    now proven across a real O2C flow, not just in an aging unit
    #    test).
    # ------------------------------------------------------------------
    invoice_resp = await issue_invoice(client, company_id, order["id"])
    assert invoice_resp.status_code == 201, invoice_resp.text
    invoice = invoice_resp.json()
    assert Decimal(invoice["total"]) == order_total

    snap_after_invoice = await _account_balances(client, company_id)
    assert _delta(snap_after_ship, snap_after_invoice) == {
        "1100": (order_total, _ZERO),
        "4000": (_ZERO, order_total),
    }

    aging_resp = await get_ar_aging(client, company_id)
    assert aging_resp.status_code == 200, aging_resp.text
    aging_row = next(r for r in aging_resp.json() if r["customer_id"] == str(customer_id))
    tb_after_invoice = await get_trial_balance_by_code(client, company_id)
    ledger_1100_balance = Decimal(tb_after_invoice["1100"]["total_debit"]) - Decimal(
        tb_after_invoice["1100"]["total_credit"]
    )
    assert Decimal(aging_row["net_total"]) == ledger_1100_balance == order_total, (
        f"tie-out property violated at invoice issue: aging net "
        f"{aging_row['net_total']}, ledger 1100 balance {ledger_1100_balance}, "
        f"invoice total {order_total}"
    )

    # ------------------------------------------------------------------
    # 5. Receive payment: the ONLY journal delta is Dr 1000 (Cash) /
    #    Cr 1100 (AR) for the payment amount. Deliberately NOT allocated
    #    inline (the payment endpoint supports that in one call) — the
    #    receipt-time delta must be observable in isolation from the
    #    allocation step that follows.
    # ------------------------------------------------------------------
    payment_resp = await create_payment(client, company_id, customer_id, order_total, "E2EO2C-PAY")
    assert payment_resp.status_code == 201, payment_resp.text
    payment = payment_resp.json()

    snap_after_payment = await _account_balances(client, company_id)
    assert _delta(snap_after_invoice, snap_after_payment) == {
        "1000": (order_total, _ZERO),
        "1100": (_ZERO, order_total),
    }

    # ------------------------------------------------------------------
    # Allocate: subledger-only bookkeeping (ADR-008 Decision 2) — moves
    # no value between accounts, so this step must post NOTHING.
    # ------------------------------------------------------------------
    alloc_resp = await allocate_payment(
        client,
        company_id,
        payment["id"],
        "E2EO2C-ALLOC",
        [allocation(invoice["id"], order_total)],
    )
    assert alloc_resp.status_code == 200, alloc_resp.text
    payment_after_alloc = alloc_resp.json()
    assert Decimal(payment_after_alloc["allocated_amount"]) == order_total

    snap_after_allocation = await _account_balances(client, company_id)
    assert (
        _delta(snap_after_payment, snap_after_allocation) == {}
    ), "allocation must post nothing to the ledger"

    invoice_get = await client.get(
        f"/api/v1/receivables/invoices/{invoice['id']}", headers=company_headers(company_id)
    )
    assert invoice_get.status_code == 200, invoice_get.text
    invoice_final = invoice_get.json()
    assert Decimal(invoice_final["settled_amount"]) == order_total
    assert invoice_final["status"] == "paid"

    # ------------------------------------------------------------------
    # 6. Final whole-flow sanity: trial balance balances overall, and the
    #    tie-out property now reads zero (the invoice is fully settled).
    #    This is retained as a whole-flow check, not the load-bearing
    #    tie-out proof — that was step 4's non-zero assertion.
    # ------------------------------------------------------------------
    final_tb = await get_trial_balance_by_code(client, company_id)
    total_debit = sum((Decimal(row["total_debit"]) for row in final_tb.values()), _ZERO)
    total_credit = sum((Decimal(row["total_credit"]) for row in final_tb.values()), _ZERO)
    assert (
        total_debit == total_credit
    ), f"trial balance does not balance: debit={total_debit} credit={total_credit}"

    final_aging = await get_ar_aging(client, company_id)
    assert final_aging.status_code == 200, final_aging.text
    final_row = next((r for r in final_aging.json() if r["customer_id"] == str(customer_id)), None)
    # A fully-settled, fully-allocated customer may not even appear in the
    # aging report at all (ADR-008 R15's union population only includes
    # customers with an open invoice balance OR nonzero unapplied credit) —
    # absence is itself the zero-net-exposure statement, not a gap to fill.
    final_customer_net = Decimal(final_row["net_total"]) if final_row is not None else _ZERO
    final_1100 = final_tb.get("1100")
    final_1100_balance = (
        Decimal(final_1100["total_debit"]) - Decimal(final_1100["total_credit"])
        if final_1100 is not None
        else _ZERO
    )
    assert final_customer_net == final_1100_balance == _ZERO
