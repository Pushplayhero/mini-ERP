"""app/cli/demo_o2c.py — the "30-second wow": walk ONE order through the

*live* O2C REST API and print the resulting trial balance (Week 7
hardening brief, Decision 4).

Unlike `app.cli.seed_demo` (direct service-layer calls against the DB,
meant to populate a rich dataset before the server or this script has run
at all), this script is an HTTP client hitting a *running* server — it
proves the live API actually works end to end, the same way a `curl`
walkthrough would, which is exactly why it's a real script instead of
inline `curl`/jq piped through a Makefile recipe (Codex diff review
2026-08-15, v1 P2: brittle, not cross-platform, hard to read).

Deliberately self-contained — it does NOT depend on `seed_demo` having run
first, including reference data: a genuinely fresh migrated database has
no `currencies`/`uom` rows at all (same fact `seed_demo`'s own module
docstring documents), so this script ensures `TWD`/`EA` itself, over HTTP,
before ever creating a company (Codex diff review 2026-08-15, slice-3
finding 1 — a prior revision skipped this and only worked by accident,
against the pytest fixture's pre-seeded reference data).

**Resumable, not repeat-creating** (v1 P2): every rerun resumes the SAME
stable demo order/invoice/payment/allocation, walking forward from
whatever state it's already in. Stock reconciliation is
**remaining-work-aware**, the same discipline `seed_demo` uses (Codex diff
review 2026-08-15, finding 2 — an earlier revision reconciled to a fixed
target unconditionally, which silently topped stock back up and created a
fresh adjustment record on every rerun once the demo order had already
shipped): pre-run on-hand is set to `initial_stock + qty still awaiting
shipment for the demo order`, so a rerun against an already-shipped order
reconciles straight to `initial_stock` and posts no new adjustment.

**Get-or-create validates compatibility, not just a code match** (finding
3): reusing an existing row with the same natural key but different
attributes (wrong account type, wrong product cost, etc.) would silently
post against something other than what this script intends — every lookup
compares the fields it cares about and raises loudly on a mismatch instead
of assuming "same code" means "same row we created".

Entry point: `uv run python -m app.cli.demo_o2c` (or `make demo`, which
waits for `/health` first — see the Makefile).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")
API = f"{BASE_URL}/api/v1"

COMPANY_CODE = "MAKEDEMO"
CUSTOMER_CODE = "MAKEDEMOCUST"
PRODUCT_SKU = "MAKEDEMO-SKU"
ORDER_TAG = "make-demo-order-1"
UNIT_PRICE = "100"
STANDARD_COST = "30"
QTY = Decimal("3")
INITIAL_STOCK = Decimal("50")
CHART = (
    ("1000", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("1300", "Inventory", "asset"),
    ("4000", "Revenue", "revenue"),
    ("5000", "COGS", "expense"),
)


def _headers(company_id: str) -> dict[str, str]:
    return {"X-Company-Id": company_id}


async def _get_or_create(
    client: httpx.AsyncClient,
    *,
    list_path: str,
    create_path: str,
    payload: dict[str, Any],
    match: Any,
    headers: dict[str, str] | None = None,
    entity: str,
    compatible: Any = None,
) -> dict[str, Any]:
    """The same get-or-create-by-code idempotency `seed_demo` uses at the

    service layer, replayed over HTTP: check the list endpoint first
    (never blind-POST), and treat a 409 from a lost create-time race as
    "look it up again", not a failure.

    `compatible`, if given, is `(existing_row) -> bool` — checked whenever
    an existing row is found (by lookup OR by 409-recovery); a `False`
    raises loudly instead of silently reusing a same-code-but-different
    row (finding 3).
    """

    def _check(row: dict[str, Any]) -> dict[str, Any]:
        if compatible is not None and not compatible(row):
            raise RuntimeError(
                f"demo_o2c: an existing {entity} (code-matched) has incompatible attributes "
                f"— {row!r} does not match what this script expects. Investigate before "
                "rerunning; this is not a race, it's a real data mismatch."
            )
        return row

    list_resp = await client.get(list_path, headers=headers)
    list_resp.raise_for_status()
    existing: dict[str, Any] | None = next((row for row in list_resp.json() if match(row)), None)
    if existing is not None:
        return _check(existing)

    create_resp = await client.post(create_path, json=payload, headers=headers)
    if create_resp.status_code == 409:
        list_resp = await client.get(list_path, headers=headers)
        list_resp.raise_for_status()
        existing = next((row for row in list_resp.json() if match(row)), None)
        if existing is not None:
            return _check(existing)
        raise RuntimeError(
            f"demo_o2c: {entity} creation conflicted but no matching row was found on retry "
            "— a genuinely incompatible existing row, not a race."
        )
    create_resp.raise_for_status()
    return create_resp.json()  # type: ignore[no-any-return]


async def _ensure_currency_and_uom(client: httpx.AsyncClient) -> None:
    """No `X-Company-Id` needed — currencies/UoM are global reference data,

    not tenant-scoped. Must run before anything that references them
    (see module docstring) — a fresh migrated database has neither.
    """
    await _get_or_create(
        client,
        list_path=f"{API}/currencies",
        create_path=f"{API}/currencies",
        payload={"code": "TWD", "name": "New Taiwan Dollar", "decimal_places": 0},
        match=lambda row: row["code"] == "TWD",
        compatible=lambda row: row["decimal_places"] == 0,
        entity="currency TWD",
    )
    await _get_or_create(
        client,
        list_path=f"{API}/uom",
        create_path=f"{API}/uom",
        payload={"code": "EA", "name": "Each"},
        match=lambda row: row["code"] == "EA",
        entity="uom EA",
    )


async def _ensure_company(client: httpx.AsyncClient) -> str:
    company = await _get_or_create(
        client,
        list_path=f"{API}/companies",
        create_path=f"{API}/companies",
        payload={
            "code": COMPANY_CODE,
            "name": "Makefile Demo Co.",
            "functional_currency_code": "TWD",
        },
        match=lambda row: row["code"] == COMPANY_CODE,
        compatible=lambda row: row["functional_currency_code"] == "TWD",
        entity="company",
    )
    return str(company["id"])


async def _ensure_period(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    today = date.today()
    await _get_or_create(
        client,
        list_path=f"{API}/periods",
        create_path=f"{API}/periods",
        payload={"year": today.year, "month": today.month},
        match=lambda row: (row["year"], row["month"]) == (today.year, today.month),
        compatible=lambda row: row["status"] == "open",
        headers=headers,
        entity="period",
    )


async def _ensure_chart(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    for code, name, acct_type in CHART:
        await _get_or_create(
            client,
            list_path=f"{API}/accounts",
            create_path=f"{API}/accounts",
            payload={"code": code, "name": name, "type": acct_type},
            match=lambda row, code=code: row["code"] == code,
            compatible=lambda row, acct_type=acct_type: row["type"] == acct_type,
            headers=headers,
            entity=f"account {code}",
        )


async def _ensure_customer_and_product(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[str, str]:
    customer = await _get_or_create(
        client,
        list_path=f"{API}/customers",
        create_path=f"{API}/customers",
        payload={
            "code": CUSTOMER_CODE,
            "name": "Makefile Demo Customer",
            "currency_code": "TWD",
            "credit_limit": "100000",
        },
        match=lambda row: row["code"] == CUSTOMER_CODE,
        compatible=lambda row: row["currency_code"] == "TWD",
        headers=headers,
        entity="customer",
    )

    uom_resp = await client.get(f"{API}/uom", headers=headers)
    uom_resp.raise_for_status()
    ea_id = next(u["id"] for u in uom_resp.json() if u["code"] == "EA")

    product = await _get_or_create(
        client,
        list_path=f"{API}/products",
        create_path=f"{API}/products",
        payload={
            "sku": PRODUCT_SKU,
            "name": "Makefile Demo Widget",
            "uom_id": ea_id,
            "list_price": UNIT_PRICE,
            "standard_cost": STANDARD_COST,
        },
        match=lambda row: row["sku"] == PRODUCT_SKU,
        compatible=lambda row: (
            row["uom_id"] == ea_id
            and Decimal(row["standard_cost"]) == Decimal(STANDARD_COST)
            and Decimal(row["list_price"]) == Decimal(UNIT_PRICE)
        ),
        headers=headers,
        entity="product",
    )
    return str(customer["id"]), str(product["id"])


async def _find_order(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any] | None:
    orders_resp = await client.get(f"{API}/sales-orders", headers=headers)
    orders_resp.raise_for_status()
    return next(
        (o for o in orders_resp.json() if o.get("custom_data", {}).get("demo_tag") == ORDER_TAG),
        None,
    )


async def _ensure_stock(
    client: httpx.AsyncClient, headers: dict[str, str], product_id: str, remaining_to_ship: Decimal
) -> None:
    """Remaining-work-aware (Codex diff review 2026-08-15, finding 2 — see

    module docstring): reconcile to `INITIAL_STOCK + remaining_to_ship`, not
    a fixed target, so a rerun against an already-shipped demo order lands
    on `INITIAL_STOCK` with no new adjustment, exactly like `seed_demo`'s
    `_reconcile_stock` for the same reason.
    """
    summary_resp = await client.get(
        f"{API}/inventory/summary", params={"product_id": product_id}, headers=headers
    )
    summary_resp.raise_for_status()
    rows = summary_resp.json()
    on_hand = Decimal(rows[0]["on_hand"]) if rows else Decimal("0")
    target = INITIAL_STOCK + remaining_to_ship
    if on_hand != target:
        adjust_resp = await client.post(
            f"{API}/inventory/adjustments",
            json={
                "product_id": product_id,
                "qty_delta": str(target - on_hand),
                "reason": (
                    f"demo_o2c: reconcile on-hand to initial {INITIAL_STOCK} + "
                    f"{remaining_to_ship} still awaiting shipment"
                ),
            },
            headers=headers,
        )
        adjust_resp.raise_for_status()


async def _ensure_order(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    customer_id: str,
    product_id: str,
    order: dict[str, Any] | None,
) -> dict[str, Any]:
    if order is None:
        create_resp = await client.post(
            f"{API}/sales-orders",
            json={
                "customer_id": customer_id,
                "lines": [{"product_id": product_id, "qty": str(QTY), "unit_price": UNIT_PRICE}],
                "custom_data": {"demo_tag": ORDER_TAG},
            },
            headers=headers,
        )
        create_resp.raise_for_status()
        order = create_resp.json()
    assert order is not None

    # A fresh, explicitly-typed local (rather than continuing to reassign
    # the `dict[str, Any] | None`-typed parameter) — mypy's flow narrowing
    # doesn't reliably carry a "not None" fact across a `.json()` (`Any`)
    # reassignment inside a later conditional block, so working through a
    # variable whose declared type never includes `None` sidesteps that
    # instead of fighting it.
    result: dict[str, Any] = order

    if result["status"] == "draft":
        confirm_resp = await client.post(
            f"{API}/sales-orders/{result['id']}/confirm", headers=headers
        )
        confirm_resp.raise_for_status()
        result = confirm_resp.json()

    if result["status"] == "confirmed":
        ship_resp = await client.post(f"{API}/sales-orders/{result['id']}/ship", headers=headers)
        ship_resp.raise_for_status()
        result = ship_resp.json()

    return result


async def _ensure_invoice(
    client: httpx.AsyncClient, headers: dict[str, str], order_id: str
) -> dict[str, Any]:
    invoices_resp = await client.get(f"{API}/receivables/invoices", headers=headers)
    invoices_resp.raise_for_status()
    invoice: dict[str, Any] | None = next(
        (i for i in invoices_resp.json() if i["order_id"] == order_id and i["status"] != "voided"),
        None,
    )
    if invoice is not None:
        return invoice

    issue_resp = await client.post(
        f"{API}/receivables/invoices", json={"order_id": order_id}, headers=headers
    )
    issue_resp.raise_for_status()
    return issue_resp.json()  # type: ignore[no-any-return]


async def _ensure_payment_and_allocation(
    client: httpx.AsyncClient, headers: dict[str, str], customer_id: str, invoice: dict[str, Any]
) -> None:
    external_ref = f"demo-{ORDER_TAG}-pay1"
    payments_resp = await client.get(f"{API}/receivables/payments", headers=headers)
    payments_resp.raise_for_status()
    payment = next((p for p in payments_resp.json() if p["external_ref"] == external_ref), None)
    if payment is None:
        create_resp = await client.post(
            f"{API}/receivables/payments",
            json={
                "customer_id": customer_id,
                "external_ref": external_ref,
                "amount": invoice["total"],
            },
            headers=headers,
        )
        create_resp.raise_for_status()
        payment = create_resp.json()

    # Stable request_ref + identical payload every run — the module's own
    # allocation-command fingerprint (ADR-008 R14) makes a repeat a no-op
    # replay; this script relies on that rather than checking "already
    # allocated?" itself (same discipline seed_demo follows).
    request_ref = f"demo-{ORDER_TAG}-alloc1"
    alloc_resp = await client.post(
        f"{API}/receivables/payments/{payment['id']}/allocations",
        json={
            "request_ref": request_ref,
            "allocations": [{"invoice_id": invoice["id"], "amount": invoice["total"]}],
        },
        headers=headers,
    )
    alloc_resp.raise_for_status()


async def _print_trial_balance(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    tb_resp = await client.get(f"{API}/reports/trial-balance", headers=headers)
    tb_resp.raise_for_status()
    rows = tb_resp.json()

    print("\nTrial balance:")
    print(f"  {'code':<6} {'account':<24} {'debit':>16} {'credit':>16}")
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for row in sorted(rows, key=lambda r: r["account_code"]):
        debit = Decimal(row["total_debit"])
        credit = Decimal(row["total_credit"])
        total_debit += debit
        total_credit += credit
        print(f"  {row['account_code']:<6} {row['account_name']:<24} {debit:>16} {credit:>16}")
    print(f"  {'':<6} {'TOTAL':<24} {total_debit:>16} {total_credit:>16}")
    assert total_debit == total_credit, "demo_o2c: trial balance does not balance — real bug"


async def run_demo(client: httpx.AsyncClient | None = None) -> None:
    """`client` is injectable so a test can pass an ASGI-transport client

    bound to the real app object (same pattern `tests/conftest.py`'s own
    `client` fixture uses) — proving this script's logic actually works
    against the real app without needing a live network server. `make
    demo`'s real invocation leaves it `None` and gets a real network
    client bound to `BASE_URL`.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        await _ensure_currency_and_uom(client)
        company_id = await _ensure_company(client)
        headers = _headers(company_id)

        await _ensure_period(client, headers)
        await _ensure_chart(client, headers)
        customer_id, product_id = await _ensure_customer_and_product(client, headers)

        # Remaining-work-aware stock reconciliation needs to know the demo
        # order's CURRENT state before deciding the target (see
        # `_ensure_stock`'s docstring) — look it up first, reconcile stock,
        # THEN actually walk it forward.
        existing_order = await _find_order(client, headers)
        remaining_to_ship = (
            QTY
            if existing_order is None or existing_order["status"] in ("draft", "confirmed")
            else Decimal("0")
        )
        await _ensure_stock(client, headers, product_id, remaining_to_ship)

        order = await _ensure_order(client, headers, customer_id, product_id, existing_order)
        print(f"Order {order['order_no']}: {order['status']}, total {order['total']}")

        invoice = await _ensure_invoice(client, headers, order["id"])
        print(f"Invoice {invoice['invoice_no']}: {invoice['status']}, total {invoice['total']}")

        await _ensure_payment_and_allocation(client, headers, customer_id, invoice)
        print("Payment received and fully allocated.")

        await _print_trial_balance(client, headers)
    finally:
        if owns_client:
            await client.aclose()


def main() -> int:
    try:
        asyncio.run(run_demo())
    except httpx.HTTPStatusError as exc:
        print(f"demo_o2c: request failed: {exc}", file=sys.stderr)
        print(f"  response body: {exc.response.text}", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(
            f"demo_o2c: could not reach {BASE_URL} — is the server running? ({exc})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
