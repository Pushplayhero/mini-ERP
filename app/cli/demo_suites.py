"""One-click Phase 3 Business Suites interactive demo (live HTTP client).

Requires a running, migrated API server (`uvicorn app.main:app`) with at
least the initial company/admin bootstrapped (`app.cli.bootstrap_admin`).
Complements `app.cli.demo_o2c` and `app.cli.demo_platform` by exercising:

1. **Act 1 — Procure-to-Pay (`purchase` & `payables`)**:
   Creates a Purchase Order (`SUITES-RAW-01`), confirms and receives it
   (`Dr 1300 Inventory / Cr 2110 Accrued Purchases`), issues a 1:1 Vendor
   Bill (`Dr 2110 / Cr 2000 Accounts Payable`), creates and allocates an
   outgoing Vendor Payment (`Dr 2000 / Cr 1000 Cash`), and verifies AP Aging.
2. **Act 2 — Moving Weighted Average & Landed Cost (`costing`)**:
   Capitalizes freight (`LANDED_COST` +50.000000 -> `Dr 1300 / Cr 2110`)
   onto the received Purchase Order, updating `SUITES-RAW-01`'s MWA unit cost
   from `20.000000` to `25.000000`, and verifies Valuation Variance.
3. **Act 3 — Multi-Level BOM, MRP Planning & WIP Work Orders (`mrp`)**:
   Creates 2-level active BOMs (`SUITES-FG-01 <- SUITES-SUB-01 <- SUITES-RAW-01`),
   proves DAG cycle rejection (HTTP 422), runs multi-level BOM explosion and
   topological MRP net-requirements planning, and executes both subassembly and
   finished-good Work Orders with zero-residue `1310 Work in Process` settlement.
4. **Act 4 — Sales Fulfillment & General Ledger Reconciliation**:
   Aligns `SUITES-FG-01`'s `standard_cost` to its realized MWA (`50.000000`),
   creates/confirms/ships a Sales Order (`Dr 5000 COGS / Cr 1300 Inventory`),
   collects AR payment, and asserts a balanced Trial Balance (`ΣDebit == ΣCredit`).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.core.settings import get_settings

SUITES_SUPPLIER_CODE = "SUITES-SUP-01"
SUITES_CUSTOMER_CODE = "SUITES-CUST-01"
SUITES_RAW_SKU = "SUITES-RAW-01"
SUITES_SUB_SKU = "SUITES-SUB-01"
SUITES_FG_SKU = "SUITES-FG-01"

SUITES_BOM_SUB_CODE = "SUITES-BOM-SUB"
SUITES_BOM_FG_CODE = "SUITES-BOM-FG"
SUITES_BOM_CYCLE_CODE = "SUITES-BOM-CYCLE"

SUITES_BILL_INV_NO = "SUITES-VINV-01"
SUITES_AP_PAY_REF = "SUITES-APPAY-01"
SUITES_AP_ALLOC_REF = "SUITES-APALLOC-01"
SUITES_LANDED_COST_REASON = "SUITES-FREIGHT-01"

SUITES_WO_SUB_NOTES = "SUITES-WO-SUB"
SUITES_WO_FG_NOTES = "SUITES-WO-FG"
SUITES_WO_SUB_ISSUE_REF = "suites-sub-issue-1"
SUITES_WO_SUB_RCV_REF = "suites-sub-rcv-1"
SUITES_WO_FG_ISSUE_REF = "suites-fg-issue-1"
SUITES_WO_FG_RCV_REF = "suites-fg-rcv-1"

SUITES_AR_PAY_REF = "SUITES-ARPAY-01"
SUITES_AR_ALLOC_REF = "SUITES-ARALLOC-01"

RAW_PURCHASE_QTY = Decimal("10")
RAW_UNIT_COST = "20.000000"
EXPECTED_PO_TOTAL = Decimal("200")
LANDED_COST_AMOUNT = "50.000000"
EXPECTED_RAW_MWA = Decimal("25")
EXPECTED_RAW_UNIT_VARIANCE = Decimal("5")
SUB_BUILD_QTY = Decimal("4")
FG_BUILD_QTY = Decimal("4")
FG_SELL_QTY = Decimal("2")
FG_LIST_PRICE = "260.000000"
EXPECTED_SO_TOTAL = Decimal("520")
MRP_DEMAND_QTY = Decimal("6")
CUSTOMER_CREDIT_LIMIT = "500000.000000"

REQUIRED_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("1000", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("1300", "Inventory", "asset"),
    ("1310", "Work in Process", "asset"),
    ("2000", "Accounts Payable", "liability"),
    ("2110", "Accrued Purchases", "liability"),
    ("4000", "Revenue", "revenue"),
    ("5000", "COGS", "expense"),
    ("5110", "Purchase Price Variance", "expense"),
)


@dataclass(frozen=True)
class DemoSuitesResult:
    """Structured summary returned by `run_demo_suites` for CLI display and tests."""

    company_id: str
    purchase_order_id: str
    purchase_order_no: str
    bill_id: str
    bill_no: str
    landed_cost_id: str
    raw_mwa_cost: str
    bom_cycle_rejected_status: int
    bom_explosion_unit_cost: str
    mrp_fg_net_req: str
    mrp_raw_action: str
    wo_sub_id: str
    wo_sub_no: str
    wo_sub_wip_balance: str
    wo_fg_id: str
    wo_fg_no: str
    wo_fg_wip_balance: str
    fg_mwa_cost: str
    fg_on_hand: str
    sales_order_id: str
    sales_order_no: str
    trial_balance_debit: str
    trial_balance_credit: str
    settled_canonical_in_this_run: bool


@dataclass(frozen=True)
class _SuitesMasterdata:
    company_id: str
    supplier: dict[str, Any]
    customer: dict[str, Any]
    raw_product: dict[str, Any]
    sub_product: dict[str, Any]
    fg_product: dict[str, Any]


def _resolve_base_url(base_url: str | None) -> str:
    raw = base_url or os.environ.get("DEMO_BASE_URL") or "http://127.0.0.1:8000"
    return raw.rstrip("/")


def _load_admin_credentials() -> tuple[str, str]:
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL") or os.environ.get("DEMO_ADMIN_EMAIL")
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or os.environ.get("DEMO_ADMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "demo_suites: BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD (or "
            "DEMO_ADMIN_EMAIL/DEMO_ADMIN_PASSWORD) must be provided in the environment."
        )
    return email, password


def _assert_not_blocked_in_production() -> None:
    settings = get_settings()
    allow_prod = os.environ.get("ALLOW_DEMO_SUITES_IN_PRODUCTION", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if settings.environment.strip().lower() == "production" and not allow_prod:
        raise RuntimeError(
            "demo_suites: APP_ENVIRONMENT=production forbids seeding demo business transactions "
            "unless ALLOW_DEMO_SUITES_IN_PRODUCTION=1 is explicitly set."
        )


async def _login_http(
    client: httpx.AsyncClient, base_url: str, email: str, password: str
) -> tuple[dict[str, str], str]:
    resp = await client.post(
        f"{base_url}/auth/login",
        json={"email": email, "password": password},
    )
    if resp.status_code == 401:
        raise RuntimeError(
            f"demo_suites: login failed (401) for {email!r}. "
            "Have you run bootstrap_admin and verified credentials?"
        )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, str(data["active_company_id"])


async def _find_compatible_row(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    list_path: str,
    match: Callable[[dict[str, Any]], bool],
    compatible: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    resp = await client.get(list_path, headers=headers)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    for row in rows:
        if match(row):
            if compatible is not None and not compatible(row):
                raise RuntimeError(f"Incompatible existing record at {list_path}: {row!r}")
            return row
    return None


async def _get_or_create(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    list_path: str,
    create_path: str,
    payload: dict[str, Any],
    match: Callable[[dict[str, Any]], bool],
    compatible: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, Any], bool]:
    existing = await _find_compatible_row(
        client, headers, list_path=list_path, match=match, compatible=compatible
    )
    if existing is not None:
        return existing, False

    create_resp = await client.post(create_path, json=payload, headers=headers)
    if create_resp.status_code == 409:
        recovered = await _find_compatible_row(
            client, headers, list_path=list_path, match=match, compatible=compatible
        )
        if recovered is not None:
            return recovered, False
        raise RuntimeError(
            f"demo_suites: creation at {create_path} returned 409 conflict with no matching "
            f"compatible record on retry: {create_resp.text}"
        )
    create_resp.raise_for_status()
    created: dict[str, Any] = create_resp.json()
    return created, True


async def _ensure_active_record(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    patch_path: str,
    row: dict[str, Any],
    patch_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if row["is_active"] and patch_payload is None:
        return row
    body = {"is_active": True} if patch_payload is None else patch_payload
    resp = await client.patch(patch_path, json=body, headers=headers)
    resp.raise_for_status()
    updated: dict[str, Any] = resp.json()
    return updated


async def _ensure_period_and_chart(
    client: httpx.AsyncClient, headers: dict[str, str], api: str
) -> str:
    today = datetime.now(UTC).date()
    await _get_or_create(
        client,
        headers,
        list_path=f"{api}/periods",
        create_path=f"{api}/periods",
        payload={"year": today.year, "month": today.month},
        match=lambda row: (row["year"], row["month"]) == (today.year, today.month),
        compatible=lambda row: row["status"] == "open",
    )

    for code, name, acct_type in REQUIRED_ACCOUNTS:

        def _match_account(row: dict[str, Any], target_code: str = code) -> bool:
            return str(row["code"]) == target_code

        def _compat_account(row: dict[str, Any], target_type: str = acct_type) -> bool:
            return str(row["type"]) == target_type

        await _get_or_create(
            client,
            headers,
            list_path=f"{api}/accounts",
            create_path=f"{api}/accounts",
            payload={"code": code, "name": name, "type": acct_type},
            match=_match_account,
            compatible=_compat_account,
        )

    uom_resp = await client.get(f"{api}/uom", headers=headers)
    uom_resp.raise_for_status()
    uoms: list[dict[str, Any]] = uom_resp.json()
    ea_uom = next((u for u in uoms if u["code"] == "EA"), uoms[0] if uoms else None)
    if ea_uom is None:
        raise RuntimeError(
            "demo_suites: no UoM records found. Run manage_reference_data or seed_demo first."
        )
    return str(ea_uom["id"])


async def _ensure_product(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    *,
    sku: str,
    name: str,
    ea_id: str,
    list_price: str,
    standard_cost: str,
    strict_list_price: bool = False,
) -> dict[str, Any]:
    def _compat(row: dict[str, Any]) -> bool:
        uom_ok = str(row["uom_id"]) == ea_id
        if strict_list_price:
            return uom_ok and Decimal(row["list_price"]) == Decimal(list_price)
        return uom_ok

    product, _ = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/products",
        create_path=f"{api}/products",
        payload={
            "sku": sku,
            "name": name,
            "uom_id": ea_id,
            "list_price": list_price,
            "standard_cost": standard_cost,
        },
        match=lambda row: row["sku"] == sku,
        compatible=_compat,
    )
    return await _ensure_active_record(client, headers, f"{api}/products/{product['id']}", product)


async def _ensure_masterdata(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    *,
    company_id: str,
) -> _SuitesMasterdata:
    ea_id = await _ensure_period_and_chart(client, headers, api)

    supplier, _ = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/suppliers",
        create_path=f"{api}/suppliers",
        payload={
            "code": SUITES_SUPPLIER_CODE,
            "name": "Suites Raw Material Vendor",
            "currency_code": "TWD",
            "payment_terms_days": 30,
        },
        match=lambda row: row["code"] == SUITES_SUPPLIER_CODE,
        compatible=lambda row: row["currency_code"] == "TWD",
    )
    supplier = await _ensure_active_record(
        client, headers, f"{api}/suppliers/{supplier['id']}", supplier
    )

    customer, _ = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/customers",
        create_path=f"{api}/customers",
        payload={
            "code": SUITES_CUSTOMER_CODE,
            "name": "Suites Finished Goods Customer",
            "currency_code": "TWD",
            "credit_limit": CUSTOMER_CREDIT_LIMIT,
            "payment_terms_days": 30,
        },
        match=lambda row: row["code"] == SUITES_CUSTOMER_CODE,
        compatible=lambda row: row["currency_code"] == "TWD",
    )
    if not customer["is_active"] or Decimal(customer["credit_limit"]) < Decimal(
        CUSTOMER_CREDIT_LIMIT
    ):
        customer = await _ensure_active_record(
            client,
            headers,
            f"{api}/customers/{customer['id']}",
            customer,
            {"credit_limit": CUSTOMER_CREDIT_LIMIT, "is_active": True},
        )

    raw_product = await _ensure_product(
        client,
        headers,
        api,
        sku=SUITES_RAW_SKU,
        name="Suites Raw Steel Plate",
        ea_id=ea_id,
        list_price="40.000000",
        standard_cost=RAW_UNIT_COST,
    )
    sub_product = await _ensure_product(
        client,
        headers,
        api,
        sku=SUITES_SUB_SKU,
        name="Suites Machined Subassembly",
        ea_id=ea_id,
        list_price="120.000000",
        standard_cost="45.000000",
    )
    fg_product = await _ensure_product(
        client,
        headers,
        api,
        sku=SUITES_FG_SKU,
        name="Suites Finished Actuator",
        ea_id=ea_id,
        list_price=FG_LIST_PRICE,
        standard_cost="100.000000",
        strict_list_price=True,
    )

    return _SuitesMasterdata(
        company_id=company_id,
        supplier=supplier,
        customer=customer,
        raw_product=raw_product,
        sub_product=sub_product,
        fg_product=fg_product,
    )


async def _run_act_1_p2p(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    md: _SuitesMasterdata,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    raw_id = md.raw_product["id"]
    po, po_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/purchase-orders",
        create_path=f"{api}/purchase-orders",
        payload={
            "supplier_id": md.supplier["id"],
            "lines": [
                {
                    "product_id": raw_id,
                    "qty": str(RAW_PURCHASE_QTY),
                    "unit_cost": RAW_UNIT_COST,
                }
            ],
        },
        match=lambda row: (
            row["supplier_id"] == md.supplier["id"] and row["status"] != "cancelled"
        ),
        compatible=lambda row: (
            len(row["lines"]) == 1
            and row["lines"][0]["product_id"] == raw_id
            and Decimal(row["lines"][0]["qty"]) == RAW_PURCHASE_QTY
            and Decimal(row["total"]) == EXPECTED_PO_TOTAL
        ),
    )

    progressed = po_created
    if po["status"] == "draft":
        conf = await client.post(f"{api}/purchase-orders/{po['id']}/confirm", headers=headers)
        conf.raise_for_status()
        po = conf.json()
        progressed = True
    if po["status"] == "confirmed":
        rcv = await client.post(f"{api}/purchase-orders/{po['id']}/receive", headers=headers)
        rcv.raise_for_status()
        po = rcv.json()
        progressed = True

    bill, bill_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/payables/bills",
        create_path=f"{api}/payables/bills",
        payload={"order_id": po["id"], "supplier_invoice_no": SUITES_BILL_INV_NO},
        match=lambda row: row["order_id"] == po["id"] and row["status"] != "voided",
        compatible=lambda row: (
            row["supplier_id"] == md.supplier["id"]
            and Decimal(row["total"]) == Decimal(po["total"])
        ),
    )

    ap_payment, pay_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/payables/payments",
        create_path=f"{api}/payables/payments",
        payload={
            "supplier_id": md.supplier["id"],
            "external_ref": SUITES_AP_PAY_REF,
            "amount": bill["total"],
        },
        match=lambda row: row["external_ref"] == SUITES_AP_PAY_REF,
        compatible=lambda row: (
            row["status"] != "voided"
            and row["supplier_id"] == md.supplier["id"]
            and Decimal(row["amount"]) == Decimal(bill["total"])
        ),
    )

    needs_alloc = Decimal(ap_payment["allocated_amount"]) < Decimal(bill["total"])
    alloc_resp = await client.post(
        f"{api}/payables/payments/{ap_payment['id']}/allocations",
        json={
            "request_ref": SUITES_AP_ALLOC_REF,
            "allocations": [{"bill_id": bill["id"], "amount": bill["total"]}],
        },
        headers=headers,
    )
    alloc_resp.raise_for_status()
    if Decimal(alloc_resp.json()["allocated_amount"]) != Decimal(bill["total"]):
        raise RuntimeError("demo_suites: vendor bill allocation did not settle expected total")

    aging_resp = await client.get(f"{api}/payables/reports/ap-aging", headers=headers)
    aging_resp.raise_for_status()
    sup_aging_net = sum(
        (
            Decimal(r["net_total"])
            for r in aging_resp.json()
            if r["supplier_id"] == md.supplier["id"]
        ),
        Decimal("0"),
    )
    if sup_aging_net != Decimal("0"):
        raise RuntimeError(f"demo_suites: expected AP aging net_total=0, got {sup_aging_net}")

    return po, bill, (progressed or bill_created or pay_created or needs_alloc)


async def _run_act_2_costing(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    md: _SuitesMasterdata,
    po: dict[str, Any],
) -> tuple[dict[str, Any], str, bool]:
    raw_id = str(md.raw_product["id"])
    landed, landed_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/costing/adjustments?product_id={raw_id}",
        create_path=f"{api}/costing/adjustments",
        payload={
            "adjustment_type": "landed_cost",
            "product_id": raw_id,
            "order_id": po["id"],
            "amount": LANDED_COST_AMOUNT,
            "reason": SUITES_LANDED_COST_REASON,
        },
        match=lambda row: (
            row["order_id"] == po["id"]
            and row["reason"] == SUITES_LANDED_COST_REASON
            and row["status"] != "voided"
        ),
        compatible=lambda row: (
            row["adjustment_type"] == "landed_cost"
            and row["product_id"] == raw_id
            and Decimal(row["amount"]) == Decimal(LANDED_COST_AMOUNT)
        ),
    )

    sum_resp = await client.get(
        f"{api}/costing/summaries",
        params={"product_id": raw_id},
        headers=headers,
    )
    sum_resp.raise_for_status()
    raw_mwa = str(sum_resp.json()[0]["moving_avg_cost"])
    if Decimal(raw_mwa) != EXPECTED_RAW_MWA:
        raise RuntimeError(f"demo_suites: expected RAW MWA={EXPECTED_RAW_MWA}, got {raw_mwa}")

    var_resp = await client.get(
        f"{api}/costing/reports/valuation-variance",
        params={"product_id": raw_id},
        headers=headers,
    )
    var_resp.raise_for_status()
    var_rows: list[dict[str, Any]] = var_resp.json()
    if (
        len(var_rows) != 1
        or Decimal(var_rows[0]["unit_cost_variance"]) != EXPECTED_RAW_UNIT_VARIANCE
    ):
        raise RuntimeError(f"demo_suites: unexpected valuation variance payload: {var_rows!r}")
    return landed, raw_mwa, landed_created


async def _ensure_active_bom(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    *,
    code: str,
    product_id: str,
    component_id: str,
    qty_per: str,
) -> tuple[dict[str, Any], bool]:
    bom, created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/mrp/boms",
        create_path=f"{api}/mrp/boms",
        payload={
            "code": code,
            "product_id": product_id,
            "output_qty": "1",
            "is_active": True,
            "lines": [{"component_product_id": component_id, "qty_per": qty_per}],
        },
        match=lambda row: row["code"] == code,
        compatible=lambda row: (
            row["product_id"] == product_id
            and Decimal(row["output_qty"]) == Decimal("1")
            and len(row["lines"]) == 1
            and row["lines"][0]["component_product_id"] == component_id
            and Decimal(row["lines"][0]["qty_per"]) == Decimal(qty_per)
        ),
    )
    if not bom["is_active"]:
        bom = await _ensure_active_record(client, headers, f"{api}/mrp/boms/{bom['id']}", bom)
        created = True
    return bom, created


async def _ensure_boms_and_plan(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    md: _SuitesMasterdata,
) -> tuple[dict[str, Any], dict[str, Any], int, str, str, str, bool]:
    raw_id = str(md.raw_product["id"])
    sub_id = str(md.sub_product["id"])
    fg_id = str(md.fg_product["id"])

    bom_sub, sub_bom_changed = await _ensure_active_bom(
        client,
        headers,
        api,
        code=SUITES_BOM_SUB_CODE,
        product_id=sub_id,
        component_id=raw_id,
        qty_per="2",
    )
    bom_fg, fg_bom_changed = await _ensure_active_bom(
        client,
        headers,
        api,
        code=SUITES_BOM_FG_CODE,
        product_id=fg_id,
        component_id=sub_id,
        qty_per="1",
    )

    cycle_resp = await client.post(
        f"{api}/mrp/boms",
        json={
            "code": SUITES_BOM_CYCLE_CODE,
            "product_id": raw_id,
            "output_qty": "1",
            "is_active": True,
            "lines": [{"component_product_id": fg_id, "qty_per": "1"}],
        },
        headers=headers,
    )
    if cycle_resp.status_code != 422 or "circular" not in cycle_resp.text.lower():
        raise RuntimeError(
            "demo_suites: expected cyclic BOM creation to return 422 Circular BOM dependency, "
            f"got {cycle_resp.status_code}: {cycle_resp.text}"
        )

    explode_resp = await client.get(
        f"{api}/mrp/boms/{bom_fg['id']}/explode",
        params={"qty": str(FG_BUILD_QTY)},
        headers=headers,
    )
    explode_resp.raise_for_status()
    unit_rolled_up_cost = str(explode_resp.json()["unit_rolled_up_cost"])

    plan_resp = await client.post(
        f"{api}/mrp/planning/net-requirements",
        json={"demands": [{"product_id": fg_id, "qty": str(MRP_DEMAND_QTY)}]},
        headers=headers,
    )
    plan_resp.raise_for_status()
    rows_by_sku = {row["sku"]: row for row in plan_resp.json()["requirements"]}
    fg_net_req = str(rows_by_sku[SUITES_FG_SKU]["net_requirement"])
    raw_action = str(rows_by_sku[SUITES_RAW_SKU]["action"])
    return (
        bom_sub,
        bom_fg,
        cycle_resp.status_code,
        unit_rolled_up_cost,
        fg_net_req,
        raw_action,
        (sub_bom_changed or fg_bom_changed),
    )


async def _execute_work_order(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    *,
    product_id: str,
    bom_id: str,
    planned_qty: Decimal,
    notes: str,
    issue_ref: str,
    receive_ref: str,
) -> tuple[dict[str, Any], bool]:
    wo, progressed = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/mrp/work-orders",
        create_path=f"{api}/mrp/work-orders",
        payload={
            "bom_id": bom_id,
            "product_id": product_id,
            "planned_qty": str(planned_qty),
            "notes": notes,
        },
        match=lambda row: row["notes"] == notes and row["status"] != "cancelled",
        compatible=lambda row: (
            row["bom_id"] == bom_id
            and row["product_id"] == product_id
            and Decimal(row["planned_qty"]) == planned_qty
        ),
    )

    if wo["status"] == "draft":
        conf = await client.post(f"{api}/mrp/work-orders/{wo['id']}/confirm", headers=headers)
        conf.raise_for_status()
        wo = conf.json()
        progressed = True

    if wo["status"] == "confirmed":
        issue = await client.post(
            f"{api}/mrp/work-orders/{wo['id']}/issue-materials",
            json={"request_ref": issue_ref},
            headers=headers,
        )
        issue.raise_for_status()
        wo = issue.json()
        progressed = True

    if wo["status"] == "in_progress":
        remaining = planned_qty - Decimal(wo["completed_qty"])
        if remaining > Decimal("0"):
            rcv = await client.post(
                f"{api}/mrp/work-orders/{wo['id']}/receive-production",
                json={"request_ref": receive_ref, "qty": str(remaining)},
                headers=headers,
            )
            rcv.raise_for_status()
            wo = rcv.json()
            progressed = True

    if wo["status"] != "completed" or Decimal(wo["wip_balance"]) != Decimal("0"):
        raise RuntimeError(
            f"demo_suites: WorkOrder {wo['wo_no']} expected completed with zero WIP, "
            f"got status={wo['status']}, wip_balance={wo['wip_balance']}"
        )
    return wo, progressed


async def _run_act_3_mrp(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    md: _SuitesMasterdata,
) -> tuple[int, str, str, str, dict[str, Any], dict[str, Any], bool]:
    (
        bom_sub,
        bom_fg,
        cycle_status,
        explode_unit_cost,
        fg_net_req,
        raw_action,
        boms_changed,
    ) = await _ensure_boms_and_plan(client, headers, api, md)

    wo_sub, sub_progressed = await _execute_work_order(
        client,
        headers,
        api,
        product_id=str(md.sub_product["id"]),
        bom_id=str(bom_sub["id"]),
        planned_qty=SUB_BUILD_QTY,
        notes=SUITES_WO_SUB_NOTES,
        issue_ref=SUITES_WO_SUB_ISSUE_REF,
        receive_ref=SUITES_WO_SUB_RCV_REF,
    )
    wo_fg, fg_progressed = await _execute_work_order(
        client,
        headers,
        api,
        product_id=str(md.fg_product["id"]),
        bom_id=str(bom_fg["id"]),
        planned_qty=FG_BUILD_QTY,
        notes=SUITES_WO_FG_NOTES,
        issue_ref=SUITES_WO_FG_ISSUE_REF,
        receive_ref=SUITES_WO_FG_RCV_REF,
    )
    return (
        cycle_status,
        explode_unit_cost,
        fg_net_req,
        raw_action,
        wo_sub,
        wo_fg,
        (boms_changed or sub_progressed or fg_progressed),
    )


async def _run_act_4_o2c_and_ledger(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    md: _SuitesMasterdata,
) -> tuple[dict[str, Any], str, str, str, str, bool]:
    fg_id = str(md.fg_product["id"])
    cost_sum_resp = await client.get(
        f"{api}/costing/summaries",
        params={"product_id": fg_id},
        headers=headers,
    )
    cost_sum_resp.raise_for_status()
    fg_mwa_cost = str(cost_sum_resp.json()[0]["moving_avg_cost"])

    synced_std_cost = False
    if Decimal(md.fg_product["standard_cost"]) != Decimal(fg_mwa_cost):
        sync_resp = await client.post(
            f"{api}/costing/summaries/{fg_id}/sync-standard-cost",
            headers=headers,
        )
        sync_resp.raise_for_status()
        fg_mwa_cost = str(sync_resp.json()["moving_avg_cost"])
        synced_std_cost = True

    so, so_progressed = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/sales-orders",
        create_path=f"{api}/sales-orders",
        payload={
            "customer_id": md.customer["id"],
            "lines": [{"product_id": fg_id, "qty": str(FG_SELL_QTY), "unit_price": FG_LIST_PRICE}],
        },
        match=lambda row: (
            row["customer_id"] == md.customer["id"] and row["status"] != "cancelled"
        ),
        compatible=lambda row: (
            len(row["lines"]) == 1
            and row["lines"][0]["product_id"] == fg_id
            and Decimal(row["lines"][0]["qty"]) == FG_SELL_QTY
            and Decimal(row["total"]) == EXPECTED_SO_TOTAL
        ),
    )

    if so["status"] == "draft":
        conf = await client.post(f"{api}/sales-orders/{so['id']}/confirm", headers=headers)
        conf.raise_for_status()
        so = conf.json()
        so_progressed = True
    if so["status"] == "confirmed":
        ship = await client.post(f"{api}/sales-orders/{so['id']}/ship", headers=headers)
        ship.raise_for_status()
        so = ship.json()
        so_progressed = True

    invoice, inv_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/receivables/invoices",
        create_path=f"{api}/receivables/invoices",
        payload={"order_id": so["id"]},
        match=lambda row: row["order_id"] == so["id"] and row["status"] != "voided",
        compatible=lambda row: Decimal(row["total"]) == Decimal(so["total"]),
    )

    ar_payment, pay_created = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/receivables/payments",
        create_path=f"{api}/receivables/payments",
        payload={
            "customer_id": md.customer["id"],
            "external_ref": SUITES_AR_PAY_REF,
            "amount": invoice["total"],
        },
        match=lambda row: row["external_ref"] == SUITES_AR_PAY_REF,
        compatible=lambda row: (
            row["status"] != "voided"
            and row["customer_id"] == md.customer["id"]
            and Decimal(row["amount"]) == Decimal(invoice["total"])
        ),
    )

    needs_alloc = Decimal(ar_payment["allocated_amount"]) < Decimal(invoice["total"])
    ar_alloc = await client.post(
        f"{api}/receivables/payments/{ar_payment['id']}/allocations",
        json={
            "request_ref": SUITES_AR_ALLOC_REF,
            "allocations": [{"invoice_id": invoice["id"], "amount": invoice["total"]}],
        },
        headers=headers,
    )
    ar_alloc.raise_for_status()
    if Decimal(ar_alloc.json()["allocated_amount"]) != Decimal(invoice["total"]):
        raise RuntimeError("demo_suites: customer invoice allocation did not settle expected total")

    inv_sum = await client.get(
        f"{api}/inventory/summary",
        params={"product_id": fg_id},
        headers=headers,
    )
    inv_sum.raise_for_status()
    fg_on_hand = str(inv_sum.json()[0]["on_hand"])

    tb_resp = await client.get(f"{api}/reports/trial-balance", headers=headers)
    tb_resp.raise_for_status()
    rows: list[dict[str, Any]] = tb_resp.json()
    total_debit = sum((Decimal(r["total_debit"]) for r in rows), Decimal("0"))
    total_credit = sum((Decimal(r["total_credit"]) for r in rows), Decimal("0"))
    if total_debit != total_credit:
        raise RuntimeError(
            f"demo_suites: trial balance out of balance: debit={total_debit}, credit={total_credit}"
        )

    settled_this_run = synced_std_cost or so_progressed or inv_created or pay_created or needs_alloc
    return (
        so,
        fg_mwa_cost,
        fg_on_hand,
        f"{total_debit:.6f}",
        f"{total_credit:.6f}",
        settled_this_run,
    )


async def run_demo_suites(
    client: httpx.AsyncClient | None = None,
    *,
    base_url: str | None = None,
) -> DemoSuitesResult:
    """Execute the complete Phase 3 Business Suites end-to-end live walkthrough."""
    _assert_not_blocked_in_production()
    resolved_base_url = _resolve_base_url(base_url)
    api = f"{resolved_base_url}/api/v1"
    admin_email, admin_password = _load_admin_credentials()

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)

    try:
        headers, company_id = await _login_http(
            client, resolved_base_url, admin_email, admin_password
        )
        md = await _ensure_masterdata(client, headers, api, company_id=company_id)

        po, bill, act1_progressed = await _run_act_1_p2p(client, headers, api, md)
        landed, raw_mwa, act2_progressed = await _run_act_2_costing(client, headers, api, md, po)
        (
            cycle_status,
            explode_unit_cost,
            fg_net_req,
            raw_action,
            wo_sub,
            wo_fg,
            act3_progressed,
        ) = await _run_act_3_mrp(client, headers, api, md)
        (
            so,
            fg_mwa_cost,
            fg_on_hand,
            tb_debit,
            tb_credit,
            act4_progressed,
        ) = await _run_act_4_o2c_and_ledger(client, headers, api, md)

        return DemoSuitesResult(
            company_id=company_id,
            purchase_order_id=str(po["id"]),
            purchase_order_no=str(po["order_no"]),
            bill_id=str(bill["id"]),
            bill_no=str(bill["bill_no"]),
            landed_cost_id=str(landed["id"]),
            raw_mwa_cost=raw_mwa,
            bom_cycle_rejected_status=cycle_status,
            bom_explosion_unit_cost=explode_unit_cost,
            mrp_fg_net_req=fg_net_req,
            mrp_raw_action=raw_action,
            wo_sub_id=str(wo_sub["id"]),
            wo_sub_no=str(wo_sub["wo_no"]),
            wo_sub_wip_balance=str(wo_sub["wip_balance"]),
            wo_fg_id=str(wo_fg["id"]),
            wo_fg_no=str(wo_fg["wo_no"]),
            wo_fg_wip_balance=str(wo_fg["wip_balance"]),
            fg_mwa_cost=fg_mwa_cost,
            fg_on_hand=fg_on_hand,
            sales_order_id=str(so["id"]),
            sales_order_no=str(so["order_no"]),
            trial_balance_debit=tb_debit,
            trial_balance_credit=tb_credit,
            settled_canonical_in_this_run=(
                act1_progressed or act2_progressed or act3_progressed or act4_progressed
            ),
        )
    finally:
        if owns_client:
            await client.aclose()


def main() -> int:
    try:
        res = asyncio.run(run_demo_suites())
    except httpx.HTTPStatusError as exc:
        print(
            f"HTTP error {exc.response.status_code} on {exc.request.method} "
            f"{exc.request.url}: {exc.response.text}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"Phase 3 Suites demo complete: PO={res.purchase_order_no}, Bill={res.bill_no}, "
        f"RawMWA={res.raw_mwa_cost}, WOs=({res.wo_sub_no}, {res.wo_fg_no}), "
        f"FgMWA={res.fg_mwa_cost}, SO={res.sales_order_no}, "
        f"FgOnHand={res.fg_on_hand}, "
        f"TrialBalance={res.trial_balance_debit}/{res.trial_balance_credit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
