"""tests/e2e/test_seed_idempotent.py — running `app.cli.seed_demo` twice

must be a no-op the second time (Week 7 hardening brief, Decision 4).

This is the run-twice proof for the idempotency design Codex's diff
review pushed hard on across three rounds (v1: "stable codes" alone is
insufficient; v2: recovery must be specified across the WHOLE O2C chain,
plus the stock-reconciliation over-correction trap; slice-3 diff review:
the snapshot itself must assert exact row counts and the real set of
posted journal-entry `source_id`s, not just dict-shaped equality that a
duplicate row with the same key could silently hide) — every document
type seed_demo touches (orders, invoices, payments, allocations, journal
entries, stock) must land in the exact same state after a second run as
after the first.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cli.seed_demo import seed_demo
from app.core.db import get_session_factory
from app.core.tenancy import company_context
from app.modules.inventory import service as inventory_service
from app.modules.ledger import service as ledger_service
from app.modules.receivables import service as receivables_service
from app.modules.sales import service as sales_service


async def _snapshot(company_id: uuid.UUID) -> dict[str, Any]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        with company_context(company_id):
            orders = await sales_service.list_orders(session)
            invoices = await receivables_service.list_invoices(session)
            payments = await receivables_service.list_payments(session)
            stock = await inventory_service.list_stock_summary(session)
            trial_balance = await ledger_service.get_trial_balance(session)
            aging = await receivables_service.get_ar_aging(session)
            journal_entries = await ledger_service.list_journal_entries(session)

    orders_by_scenario = {
        o.custom_data.get("seed_scenario"): (o.status.value, str(o.total)) for o in orders
    }
    invoices_by_order = {
        i.order_id: (i.status.value, str(i.total), str(i.settled_amount)) for i in invoices
    }
    payments_by_ref = {
        p.external_ref: (p.status.value, str(p.amount), str(p.allocated_amount)) for p in payments
    }

    return {
        "orders": orders_by_scenario,
        "orders_count": len(orders),
        "invoices": invoices_by_order,
        "invoices_count": len(invoices),
        "payments": payments_by_ref,
        "payments_count": len(payments),
        "stock": {str(s.product_id): str(s.on_hand) for s in stock},
        "trial_balance": {
            line.account_code: (str(line.total_debit), str(line.total_credit))
            for line in trial_balance
        },
        "aging": {
            row.customer_code: (str(row.net_total), str(row.unapplied_credits)) for row in aging
        },
        # The direct proof no event re-posted a second time: the exact SET
        # of (source_type, source_id) pairs every posted journal entry
        # carries (ADR-003's own idempotency key) must be identical across
        # both runs — a re-post would either add a new pair (if it weren't
        # actually idempotent) or the set comparison catches it either way,
        # unlike comparing net trial-balance totals alone, which a
        # duplicate-post-plus-compensating-entry could in principle hide.
        "journal_entry_source_ids": {
            (e.source_type, e.source_id) for e in journal_entries if e.source_id is not None
        },
        "journal_entries_count": len(journal_entries),
    }


@pytest.mark.asyncio
async def test_seed_demo_is_idempotent(db_engine: AsyncEngine) -> None:
    summary_1 = await seed_demo()
    company_id = summary_1.company_id
    assert summary_1.scenarios_resumed == 4
    assert summary_1.invoices_issued == 3  # every scenario except s4-shipped-uninvoiced
    assert summary_1.payments_created == 2  # s1 (full) and s2 (partial); s3 has none
    assert summary_1.allocations_submitted == 2

    snap_1 = await _snapshot(company_id)
    # Explicit row-count assertions (Codex diff review 2026-08-15, finding
    # 5) — a dict-shaped snapshot alone would silently collapse a
    # duplicate row sharing the same key, hiding exactly the bug this test
    # exists to catch.
    assert snap_1["orders_count"] == 4
    assert snap_1["invoices_count"] == 3
    assert snap_1["payments_count"] == 2
    assert snap_1["journal_entries_count"] > 0

    # A second run must recover every document by its stable key and post
    # nothing new — this is the actual idempotency proof, not summary_1's
    # counts (which only prove the FIRST run did the right amount of work).
    summary_2 = await seed_demo()
    assert summary_2.company_id == company_id
    assert summary_2.scenarios_resumed == 4
    assert summary_2.invoices_issued == 0, "rerun must recover existing invoices, not reissue"
    assert summary_2.payments_created == 0, "rerun must recover existing payments, not recreate"
    # Allocation calls ARE submitted again every run (by design — the
    # module's own R14 fingerprint makes the *repeat* a no-op, not the
    # seed script itself skipping the call); assert the STATE is unchanged
    # instead of asserting this counter is zero.
    assert summary_2.allocations_submitted == 2

    snap_2 = await _snapshot(company_id)

    assert snap_2["orders_count"] == snap_1["orders_count"]
    assert snap_2["invoices_count"] == snap_1["invoices_count"]
    assert snap_2["payments_count"] == snap_1["payments_count"]
    assert snap_2["journal_entries_count"] == snap_1["journal_entries_count"]
    assert snap_2["journal_entry_source_ids"] == snap_1["journal_entry_source_ids"]

    assert snap_1["orders"] == snap_2["orders"]
    assert snap_1["invoices"] == snap_2["invoices"]
    assert snap_1["payments"] == snap_2["payments"]
    assert snap_1["stock"] == snap_2["stock"]
    assert snap_1["trial_balance"] == snap_2["trial_balance"]
    assert snap_1["aging"] == snap_2["aging"]

    # Sanity on the actual scenario shape, not just "unchanged" — proves
    # the four scenarios really did land at the four distinct target
    # states the brief specifies.
    assert snap_2["orders"]["s1-fully-paid"][0] == "shipped"
    assert snap_2["orders"]["s4-shipped-uninvoiced"][0] == "shipped"
    statuses = sorted(status for status, _total, _settled in snap_2["invoices"].values())
    assert statuses == ["open", "paid", "partial"]

    # Stock landed on the desired final target — the same explicit check
    # `seed_demo` now performs on itself (finding 4), re-asserted here.
    assert {Decimal(v) for v in snap_2["stock"].values()} == {Decimal("100")}

    # Trial balance still balances after the full seed.
    total_debit = sum((Decimal(d) for d, _c in snap_2["trial_balance"].values()), Decimal("0"))
    total_credit = sum((Decimal(c) for _d, c in snap_2["trial_balance"].values()), Decimal("0"))
    assert total_debit == total_credit
