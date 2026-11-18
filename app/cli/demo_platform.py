"""One-click Phase 2 Platform governance interactive demo (live HTTP client).

Requires a running, migrated API server (`uvicorn app.main:app`) with at
least the initial company/admin bootstrapped (`app.cli.bootstrap_admin`).
Complements `app.cli.demo_o2c` (the Phase 1 O2C baseline) by exercising:

1. **Act 1 — Dynamic Plugin Governance (`platform.plugins`)**:
   Demonstrates the built-in `credit_limit` plugin blocking an over-limit
   draft order with HTTP 409, toggling the plugin off/on at runtime via
   `PATCH /api/v1/plugins/credit_limit`, and raising `grace_amount` with
   6-decimal-place precision to clear the credit gate.
2. **Act 2 — Dynamic Custom Fields (`platform.customfields`)**:
   Defines `ENUM`, `DECIMAL`, and required regex-constrained `TEXT` fields
   on `sales_order`, proves fail-closed HTTP 422 rejections (unknown key,
   missing required field, regex mismatch, invalid enum), and verifies default
   population plus Banker's Rounding (`ROUND_HALF_EVEN`) to 6 decimal places.
3. **Act 3 — Multi-Level Workflow, SoD & Step RBAC (`platform.workflow`)**:
   Uses two least-privilege approver accounts (logged in via real
   `POST /auth/login`), enforces a 2-step `sales_order.confirm` workflow with
   `allow_self_approval=False`, demonstrates HTTP 409 missing-approval gate,
   HTTP 403 Separation-of-Duties (SoD) self-approval block, Step 1 -> HTTP 403
   Step RBAC block, Step 2 sign-off, order confirmation (`APPROVED -> CONSUMED`),
   O2C settlement (`ship -> invoice -> pay -> allocate`), and balanced ledger
   trial balance.
4. **Act 4 — Soft Deactivation & Guardrail Cleanup**:
   Demonstrates ADR-011 soft deactivation (retaining historical `custom_data`
   while rejecting writes to retired keys with HTTP 422), then restores temporary
   demo guardrails so reruns are idempotent and `make demo` (`demo_o2c`)
   continues working cleanly.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.core.settings import get_settings
from app.platform.permissions.models import Role, RolePermission, User, UserCompanyRole
from app.platform.permissions.service import hash_password

PLATFORM_CUSTOMER_CODE = "PLAT-CUST-CREDIT"
PLATFORM_PRODUCT_SKU = "PLAT-SKU-01"
DEFAULT_CREDIT_LIMIT = "500.000000"
PRODUCT_LIST_PRICE = "200.000000"
PRODUCT_STANDARD_COST = "50.000000"
ORDER_QTY = Decimal("3")
TARGET_STOCK_QTY = Decimal("10")

DEMO_FIELD_PRIORITY = "plat_priority"
DEMO_FIELD_DISCOUNT_CAP = "plat_discount_cap"
DEMO_FIELD_CONTRACT_REF = "plat_contract_ref"
DEMO_FIELD_KEYS = (DEMO_FIELD_PRIORITY, DEMO_FIELD_DISCOUNT_CAP, DEMO_FIELD_CONTRACT_REF)

APPROVER_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "demo-sales-lead@mini-erp.local",
        "demo_sales_lead",
        "Demo Sales Lead",
        (
            "sales.order.read",
            "sales.order.confirm",
            "platform.workflow.read",
            "platform.workflow.approve",
        ),
    ),
    (
        "demo-finance-ctrl@mini-erp.local",
        "demo_finance_ctrl",
        "Demo Finance Controller",
        (
            "sales.order.read",
            "platform.workflow.read",
            "platform.workflow.approve",
            "ledger.journal_entry.reverse",
        ),
    ),
)

REQUIRED_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("1000", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("1300", "Inventory", "asset"),
    ("4000", "Revenue", "revenue"),
    ("5000", "COGS", "expense"),
)


@dataclass(frozen=True)
class DemoPlatformResult:
    """Structured summary returned by `run_demo_platform` for CLI display and tests."""

    company_id: str
    canonical_order_id: str
    canonical_order_no: str
    probe_order_id: str
    credit_limit_blocked_status: int
    plugin_disabled_workflow_status: int
    customfield_reject_statuses: list[int]
    customfield_normalized_cap: str
    customfield_deactivated_reject_status: int
    workflow_missing_status: int
    workflow_sod_status: int
    workflow_step_rbac_status: int
    workflow_final_status: str
    trial_balance_debit: str
    trial_balance_credit: str
    stock_on_hand: str
    settled_canonical_in_this_run: bool


def _resolve_base_url(base_url: str | None) -> str:
    raw = base_url or os.environ.get("DEMO_BASE_URL") or "http://127.0.0.1:8000"
    return raw.rstrip("/")


def _load_admin_credentials() -> tuple[str, str]:
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL") or os.environ.get("DEMO_ADMIN_EMAIL")
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or os.environ.get("DEMO_ADMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "demo_platform: BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD (or "
            "DEMO_ADMIN_EMAIL/DEMO_ADMIN_PASSWORD) must be provided in the environment."
        )
    return email, password


def _validate_approver_env_preconditions(
    admin_email: str,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Fail-closed upfront check for approver env vars and production guard.

    Runs before any HTTP or DB mutation so invalid env configuration or
    `APP_ENVIRONMENT=production` without explicit approver credentials fails
    without leaving partial demo state behind.
    """
    a1_email = (os.environ.get("DEMO_APPROVER_1_EMAIL") or "").strip()
    a1_pw = os.environ.get("DEMO_APPROVER_1_PASSWORD") or ""
    a2_email = (os.environ.get("DEMO_APPROVER_2_EMAIL") or "").strip()
    a2_pw = os.environ.get("DEMO_APPROVER_2_PASSWORD") or ""

    provided_flags = (bool(a1_email), bool(a1_pw), bool(a2_email), bool(a2_pw))
    if any(provided_flags):
        if not all(provided_flags):
            raise RuntimeError(
                "demo_platform: partial DEMO_APPROVER_* configuration detected; "
                "all four of DEMO_APPROVER_1_EMAIL, DEMO_APPROVER_1_PASSWORD, "
                "DEMO_APPROVER_2_EMAIL, and DEMO_APPROVER_2_PASSWORD must be set together."
            )
        norm_admin = admin_email.strip().lower()
        norm_a1 = a1_email.lower()
        norm_a2 = a2_email.lower()
        if len({norm_admin, norm_a1, norm_a2}) < 3:
            raise RuntimeError(
                "demo_platform: admin, DEMO_APPROVER_1_EMAIL, and DEMO_APPROVER_2_EMAIL "
                "must be three distinct user accounts to satisfy Separation of Duties."
            )
        return (a1_email, a1_pw), (a2_email, a2_pw)

    settings = get_settings()
    if settings.environment.strip().lower() == "production":
        raise RuntimeError(
            "demo_platform: APP_ENVIRONMENT=production forbids auto-provisioning demo "
            "approver accounts. Supply DEMO_APPROVER_1_EMAIL/PASSWORD and "
            "DEMO_APPROVER_2_EMAIL/PASSWORD explicitly."
        )
    return None


async def _login_http(
    client: httpx.AsyncClient,
    base_url: str,
    email: str,
    password: str,
    *,
    company_id: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload: dict[str, Any] = {"email": email, "password": password}
    if company_id is not None:
        payload["company_id"] = company_id
    resp = await client.post(f"{base_url}/auth/login", json=payload)
    if resp.status_code == 401:
        raise RuntimeError(
            f"demo_platform: login failed (401) for {email!r}. "
            "Have you run bootstrap_admin and verified credentials?"
        )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    return headers, data


async def _get_or_create(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    list_path: str,
    create_path: str,
    payload: dict[str, Any],
    match: Callable[[dict[str, Any]], bool],
    compatible: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    existing_resp = await client.get(list_path, headers=headers)
    existing_resp.raise_for_status()
    rows: list[dict[str, Any]] = existing_resp.json()
    for row in rows:
        if match(row):
            if compatible is not None and not compatible(row):
                raise RuntimeError(f"Incompatible existing record at {list_path}: {row!r}")
            return row

    create_resp = await client.post(create_path, json=payload, headers=headers)
    create_resp.raise_for_status()
    created: dict[str, Any] = create_resp.json()
    return created


async def _ensure_prerequisite_masterdata(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
            "demo_platform: no UoM records found. Run manage_reference_data or seed_demo first."
        )
    ea_id = ea_uom["id"]

    customer = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/customers",
        create_path=f"{api}/customers",
        payload={
            "code": PLATFORM_CUSTOMER_CODE,
            "name": "Platform Governance Demo Customer",
            "credit_limit": DEFAULT_CREDIT_LIMIT,
            "currency_code": "TWD",
            "payment_terms_days": 30,
        },
        match=lambda row: row["code"] == PLATFORM_CUSTOMER_CODE,
    )
    if (
        Decimal(customer["credit_limit"]) != Decimal(DEFAULT_CREDIT_LIMIT)
        or not customer["is_active"]
    ):
        patch_cust = await client.patch(
            f"{api}/customers/{customer['id']}",
            json={"credit_limit": DEFAULT_CREDIT_LIMIT, "is_active": True},
            headers=headers,
        )
        patch_cust.raise_for_status()
        customer = patch_cust.json()

    product = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/products",
        create_path=f"{api}/products",
        payload={
            "sku": PLATFORM_PRODUCT_SKU,
            "name": "Platform Governance Widget",
            "uom_id": ea_id,
            "list_price": PRODUCT_LIST_PRICE,
            "standard_cost": PRODUCT_STANDARD_COST,
        },
        match=lambda row: row["sku"] == PLATFORM_PRODUCT_SKU,
        compatible=lambda row: (
            Decimal(row["list_price"]) == Decimal(PRODUCT_LIST_PRICE)
            and Decimal(row["standard_cost"]) == Decimal(PRODUCT_STANDARD_COST)
        ),
    )
    if not product["is_active"]:
        patch_prod = await client.patch(
            f"{api}/products/{product['id']}",
            json={"is_active": True},
            headers=headers,
        )
        patch_prod.raise_for_status()
        product = patch_prod.json()

    summary_resp = await client.get(
        f"{api}/inventory/summary",
        params={"product_id": product["id"]},
        headers=headers,
    )
    summary_resp.raise_for_status()
    summaries: list[dict[str, Any]] = summary_resp.json()
    on_hand = sum((Decimal(s["on_hand"]) for s in summaries), Decimal("0"))
    if on_hand < ORDER_QTY:
        delta = TARGET_STOCK_QTY - on_hand
        adj_resp = await client.post(
            f"{api}/inventory/adjustments",
            json={
                "product_id": product["id"],
                "qty_delta": str(delta),
                "reason": "Phase 2 platform demo stock intake",
            },
            headers=headers,
        )
        adj_resp.raise_for_status()

    return customer, product


async def _ensure_approver_credentials(
    company_id: str,
    *,
    admin_email: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Resolve approver credentials from env or provision least-privilege demo users."""
    env_creds = _validate_approver_env_preconditions(admin_email)
    if env_creds is not None:
        return env_creds

    factory = session_factory or get_session_factory()
    company_uuid = uuid.UUID(company_id)
    credentials: list[tuple[str, str]] = []

    async with factory() as session:
        for email, role_code, role_name, perm_codes in APPROVER_SPECS:
            role_res = await session.execute(select(Role).where(Role.code == role_code))
            role = role_res.scalars().first()
            if role is None:
                role = Role(code=role_code, name=role_name)
                session.add(role)
                await session.flush()

            existing_perms_res = await session.execute(
                select(RolePermission.permission_code).where(RolePermission.role_code == role_code)
            )
            existing_perms = set(existing_perms_res.scalars().all())
            for perm_code in perm_codes:
                if perm_code not in existing_perms:
                    session.add(RolePermission(role_code=role_code, permission_code=perm_code))

            ephemeral_pw = secrets.token_urlsafe(24)
            user_res = await session.execute(select(User).where(User.email == email))
            user = user_res.scalars().first()
            if user is None:
                user = User(
                    email=email,
                    password_hash=hash_password(ephemeral_pw),
                    is_active=True,
                )
                session.add(user)
                await session.flush()
            else:
                user.password_hash = hash_password(ephemeral_pw)
                user.is_active = True

            ucr_res = await session.execute(
                select(UserCompanyRole).where(
                    UserCompanyRole.user_id == user.id,
                    UserCompanyRole.company_id == company_uuid,
                    UserCompanyRole.role_code == role_code,
                )
            )
            if ucr_res.scalars().first() is None:
                session.add(
                    UserCompanyRole(
                        user_id=user.id,
                        company_id=company_uuid,
                        role_code=role_code,
                    )
                )

            credentials.append((email, ephemeral_pw))

        await session.commit()

    return credentials[0], credentials[1]


async def _upsert_custom_field(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    existing_by_key: dict[str, dict[str, Any]],
    *,
    field_key: str,
    label: str,
    field_type: str,
    is_required: bool,
    default_value: Any,
    validation_rules: dict[str, Any],
) -> dict[str, Any]:
    existing = existing_by_key.get(field_key)
    if existing is None:
        resp = await client.post(
            f"{api}/custom-fields",
            json={
                "entity_type": "sales_order",
                "field_key": field_key,
                "label": label,
                "field_type": field_type,
                "is_required": is_required,
                "default_value": default_value,
                "validation_rules": validation_rules,
            },
            headers=headers,
        )
        resp.raise_for_status()
        created: dict[str, Any] = resp.json()
        return created

    patch_resp = await client.patch(
        f"{api}/custom-fields/{existing['id']}",
        json={
            "label": label,
            "is_required": is_required,
            "is_active": True,
            "default_value": default_value,
            "validation_rules": validation_rules,
        },
        headers=headers,
    )
    patch_resp.raise_for_status()
    updated: dict[str, Any] = patch_resp.json()
    return updated


async def _upsert_demo_workflow(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    existing_defs: list[dict[str, Any]],
) -> dict[str, Any]:
    steps = [
        {
            "step_order": 1,
            "step_name": "Sales Lead Review",
            "min_amount": "100.000000",
            "required_permission": "sales.order.confirm",
        },
        {
            "step_order": 2,
            "step_name": "Finance Control Sign-off",
            "min_amount": "500.000000",
            "required_permission": "ledger.journal_entry.reverse",
        },
    ]
    if not existing_defs:
        resp = await client.post(
            f"{api}/workflows/definitions",
            json={
                "entity_type": "sales_order",
                "transition": "confirm",
                "name": "Phase 2 High-Value Order SoD Approval",
                "description": "Two-level approval with strict SoD for demo_platform",
                "is_active": True,
                "allow_self_approval": False,
                "steps": steps,
            },
            headers=headers,
        )
        resp.raise_for_status()
        created: dict[str, Any] = resp.json()
        return created

    def_id = existing_defs[0]["id"]
    patch_resp = await client.patch(
        f"{api}/workflows/definitions/{def_id}",
        json={
            "name": "Phase 2 High-Value Order SoD Approval",
            "description": "Two-level approval with strict SoD for demo_platform",
            "is_active": True,
            "allow_self_approval": False,
            "steps": steps,
        },
        headers=headers,
    )
    patch_resp.raise_for_status()
    updated: dict[str, Any] = patch_resp.json()
    return updated


async def _settle_canonical_o2c(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    order: dict[str, Any],
) -> dict[str, Any]:
    order_id = order["id"]
    if order["status"] == "draft":
        confirm_resp = await client.post(f"{api}/sales-orders/{order_id}/confirm", headers=headers)
        confirm_resp.raise_for_status()
        order = confirm_resp.json()

    if order["status"] == "confirmed":
        ship_resp = await client.post(f"{api}/sales-orders/{order_id}/ship", headers=headers)
        ship_resp.raise_for_status()
        order = ship_resp.json()

    invoice = await _get_or_create(
        client,
        headers,
        list_path=f"{api}/receivables/invoices",
        create_path=f"{api}/receivables/invoices",
        payload={"order_id": order_id},
        match=lambda row: row["order_id"] == order_id and row["status"] != "voided",
    )

    if Decimal(invoice["settled_amount"]) < Decimal(invoice["total"]):
        ext_ref = f"plat-demo-pay-{order_id}"
        payments_resp = await client.get(f"{api}/receivables/payments", headers=headers)
        payments_resp.raise_for_status()
        existing_payment = next(
            (p for p in payments_resp.json() if p["external_ref"] == ext_ref),
            None,
        )
        if existing_payment is None:
            pay_resp = await client.post(
                f"{api}/receivables/payments",
                json={
                    "customer_id": order["customer_id"],
                    "external_ref": ext_ref,
                    "amount": invoice["total"],
                    "allocations": [{"invoice_id": invoice["id"], "amount": invoice["total"]}],
                },
                headers=headers,
            )
            pay_resp.raise_for_status()
        elif Decimal(existing_payment["allocated_amount"]) < Decimal(invoice["total"]):
            alloc_resp = await client.post(
                f"{api}/receivables/payments/{existing_payment['id']}/allocations",
                json={
                    "request_ref": f"plat-demo-alloc-{order_id}",
                    "allocations": [{"invoice_id": invoice["id"], "amount": invoice["total"]}],
                },
                headers=headers,
            )
            alloc_resp.raise_for_status()

    return order


async def _deactivate_demo_custom_fields(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
) -> None:
    cf_resp = await client.get(
        f"{api}/custom-fields",
        params={"entity_type": "sales_order"},
        headers=headers,
    )
    cf_resp.raise_for_status()
    for field_def in cf_resp.json():
        if field_def["field_key"] in DEMO_FIELD_KEYS and field_def["is_active"]:
            del_resp = await client.delete(
                f"{api}/custom-fields/{field_def['id']}",
                headers=headers,
            )
            del_resp.raise_for_status()


async def _cancel_active_instances(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api: str,
    *,
    entity_id: str,
    comment: str,
) -> None:
    inst_resp = await client.get(
        f"{api}/workflows/instances",
        params={
            "entity_type": "sales_order",
            "entity_id": entity_id,
            "transition": "confirm",
        },
        headers=headers,
    )
    inst_resp.raise_for_status()
    for inst in inst_resp.json():
        if inst["status"] in ("PENDING", "APPROVED"):
            cancel_resp = await client.post(
                f"{api}/workflows/instances/{inst['id']}/cancel",
                json={"comment": comment},
                headers=headers,
            )
            cancel_resp.raise_for_status()


async def _run_act_1_plugins(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    probe_id: str,
) -> tuple[int, int]:
    """Demonstrate runtime `credit_limit` 409 block, off toggle, and grace_amount bump."""
    p_zero = await client.patch(
        f"{api}/plugins/credit_limit",
        json={"is_enabled": True, "config": {"grace_amount": "0.000000"}},
        headers=admin_headers,
    )
    p_zero.raise_for_status()

    blocked_resp = await client.post(
        f"{api}/sales-orders/{probe_id}/confirm", headers=admin_headers
    )
    assert blocked_resp.status_code == 409, (
        f"Expected 409 credit limit rejection, got {blocked_resp.status_code}: "
        f"{blocked_resp.text}"
    )
    assert "credit limit" in blocked_resp.json()["detail"].lower()

    p_off = await client.patch(
        f"{api}/plugins/credit_limit",
        json={"is_enabled": False},
        headers=admin_headers,
    )
    p_off.raise_for_status()
    disabled_confirm = await client.post(
        f"{api}/sales-orders/{probe_id}/confirm", headers=admin_headers
    )
    assert disabled_confirm.status_code == 409
    assert "workflow approval" in disabled_confirm.json()["detail"].lower()

    p_grace = await client.patch(
        f"{api}/plugins/credit_limit",
        json={"is_enabled": True, "config": {"grace_amount": "150.000000"}},
        headers=admin_headers,
    )
    p_grace.raise_for_status()
    grace_confirm = await client.post(
        f"{api}/sales-orders/{probe_id}/confirm", headers=admin_headers
    )
    assert grace_confirm.status_code == 409
    assert "workflow approval" in grace_confirm.json()["detail"].lower()

    print(
        "[Act 1: Plugins] credit_limit enabled (grace=0) blocked 600 > 500 with HTTP 409; "
        "disabled toggle & grace=150.000000 dynamically cleared credit gate."
    )
    return blocked_resp.status_code, disabled_confirm.status_code


async def _run_act_2_customfields(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    *,
    customer_id: str,
    product_id: str,
    probe_id: str,
    canonical_order: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[int]]:
    """Define custom fields, prove 422 rejections, and verify defaults & HALF_EVEN 6dp."""
    cf_list_resp = await client.get(
        f"{api}/custom-fields",
        params={"entity_type": "sales_order"},
        headers=admin_headers,
    )
    cf_list_resp.raise_for_status()
    cf_by_key = {row["field_key"]: row for row in cf_list_resp.json()}

    await _upsert_custom_field(
        client,
        admin_headers,
        api,
        cf_by_key,
        field_key=DEMO_FIELD_PRIORITY,
        label="Order Priority",
        field_type="ENUM",
        is_required=False,
        default_value="NORMAL",
        validation_rules={"choices": ["LOW", "NORMAL", "URGENT"]},
    )
    await _upsert_custom_field(
        client,
        admin_headers,
        api,
        cf_by_key,
        field_key=DEMO_FIELD_DISCOUNT_CAP,
        label="Discount Cap (%)",
        field_type="DECIMAL",
        is_required=False,
        default_value=None,
        validation_rules={"min_value": "0", "max_value": "100"},
    )
    contract_ref_def = await _upsert_custom_field(
        client,
        admin_headers,
        api,
        cf_by_key,
        field_key=DEMO_FIELD_CONTRACT_REF,
        label="Contract Reference",
        field_type="TEXT",
        is_required=True,
        default_value=None,
        validation_rules={"regex": r"^CTR-[0-9]{4}$"},
    )

    invalid_custom_payloads: list[dict[str, Any]] = [
        {DEMO_FIELD_CONTRACT_REF: "CTR-2026", "rogue_field": "rejected"},
        {},
        {DEMO_FIELD_CONTRACT_REF: "BAD-REF"},
        {DEMO_FIELD_CONTRACT_REF: "CTR-2026", DEMO_FIELD_PRIORITY: "CRITICAL"},
    ]
    cf_reject_statuses: list[int] = []
    for bad_custom_data in invalid_custom_payloads:
        bad_resp = await client.post(
            f"{api}/sales-orders",
            json={
                "customer_id": customer_id,
                "lines": [{"product_id": product_id, "qty": "1"}],
                "custom_data": bad_custom_data,
            },
            headers=admin_headers,
        )
        assert bad_resp.status_code == 422, (
            f"Expected 422 for custom_data={bad_custom_data!r}, got "
            f"{bad_resp.status_code}: {bad_resp.text}"
        )
        cf_reject_statuses.append(bad_resp.status_code)

    if canonical_order is None:
        canon_create = await client.post(
            f"{api}/sales-orders",
            json={
                "customer_id": customer_id,
                "lines": [{"product_id": product_id, "qty": str(ORDER_QTY)}],
                "custom_data": {
                    DEMO_FIELD_DISCOUNT_CAP: "12.3456785",
                    DEMO_FIELD_CONTRACT_REF: "CTR-2026",
                },
            },
            headers=admin_headers,
        )
        canon_create.raise_for_status()
        resolved_canonical: dict[str, Any] = canon_create.json()
    else:
        resolved_canonical = canonical_order
        if (
            resolved_canonical["status"] == "draft"
            and resolved_canonical["custom_data"].get(DEMO_FIELD_CONTRACT_REF) != "CTR-2026"
        ):
            patch_canon = await client.patch(
                f"{api}/sales-orders/{resolved_canonical['id']}",
                json={
                    "custom_data": {
                        DEMO_FIELD_PRIORITY: "NORMAL",
                        DEMO_FIELD_DISCOUNT_CAP: "12.3456785",
                        DEMO_FIELD_CONTRACT_REF: "CTR-2026",
                    }
                },
                headers=admin_headers,
            )
            patch_canon.raise_for_status()
            resolved_canonical = patch_canon.json()

    patch_probe = await client.patch(
        f"{api}/sales-orders/{probe_id}",
        json={
            "custom_data": {
                DEMO_FIELD_PRIORITY: "NORMAL",
                DEMO_FIELD_DISCOUNT_CAP: "12.3456785",
                DEMO_FIELD_CONTRACT_REF: "CTR-2026",
            }
        },
        headers=admin_headers,
    )
    patch_probe.raise_for_status()
    probe_order: dict[str, Any] = patch_probe.json()

    canon_get = await client.get(
        f"{api}/sales-orders/{resolved_canonical['id']}", headers=admin_headers
    )
    canon_get.raise_for_status()
    resolved_canonical = canon_get.json()

    assert resolved_canonical["custom_data"][DEMO_FIELD_PRIORITY] == "NORMAL"
    assert resolved_canonical["custom_data"][DEMO_FIELD_DISCOUNT_CAP] == "12.345678"
    assert resolved_canonical["custom_data"][DEMO_FIELD_CONTRACT_REF] == "CTR-2026"
    assert probe_order["custom_data"][DEMO_FIELD_DISCOUNT_CAP] == "12.345678"

    print(
        "[Act 2: CustomFields] Rejected 4 invalid payloads with HTTP 422; "
        "defaulted plat_priority='NORMAL' and quantized '12.3456785' -> '12.345678'."
    )
    return resolved_canonical, contract_ref_def, cf_reject_statuses


async def _run_act_3_workflow_o2c(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    resolved_base_url: str,
    api: str,
    *,
    admin_email: str,
    company_id: str,
    product_id: str,
    probe_id: str,
    canonical_order: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> tuple[dict[str, Any], int, int, int, str, Decimal, Decimal, Decimal, bool]:
    """Run 2-step workflow with SoD/RBAC checks, O2C settlement, and trial-balance proof."""
    (a1_email, a1_pw), (a2_email, a2_pw) = await _ensure_approver_credentials(
        company_id, admin_email=admin_email, session_factory=session_factory
    )
    a1_headers, _ = await _login_http(
        client, resolved_base_url, a1_email, a1_pw, company_id=company_id
    )
    a2_headers, _ = await _login_http(
        client, resolved_base_url, a2_email, a2_pw, company_id=company_id
    )

    settled_this_run = canonical_order["status"] != "shipped"
    target_order_id = str(canonical_order["id"]) if settled_this_run else probe_id
    await _cancel_active_instances(
        client,
        admin_headers,
        api,
        entity_id=target_order_id,
        comment="Clear stale active instance prior to Act 3 workflow walk",
    )

    missing_wf_resp = await client.post(
        f"{api}/sales-orders/{target_order_id}/confirm", headers=admin_headers
    )
    assert missing_wf_resp.status_code == 409

    submit_resp = await client.post(
        f"{api}/workflows/instances",
        json={
            "entity_type": "sales_order",
            "entity_id": target_order_id,
            "transition": "confirm",
        },
        headers=admin_headers,
    )
    submit_resp.raise_for_status()
    wf_inst: dict[str, Any] = submit_resp.json()
    wf_inst_id = str(wf_inst["id"])
    assert wf_inst["status"] == "PENDING"
    assert wf_inst["current_step_order"] == 1
    assert len(wf_inst["steps_snapshot"]) == 2

    sod_resp = await client.post(
        f"{api}/workflows/instances/{wf_inst_id}/approve",
        json={"comment": "Attempting invalid self-approval"},
        headers=admin_headers,
    )
    assert (
        sod_resp.status_code == 403
    ), f"Expected 403 SoD block, got {sod_resp.status_code}: {sod_resp.text}"

    step1_resp = await client.post(
        f"{api}/workflows/instances/{wf_inst_id}/approve",
        json={"comment": "Sales lead checks credit/margin"},
        headers=a1_headers,
    )
    step1_resp.raise_for_status()
    assert step1_resp.json()["status"] == "PENDING"
    assert step1_resp.json()["current_step_order"] == 2

    rbac_resp = await client.post(
        f"{api}/workflows/instances/{wf_inst_id}/approve",
        json={"comment": "Unauthorized Step 2 attempt"},
        headers=a1_headers,
    )
    assert (
        rbac_resp.status_code == 403
    ), f"Expected 403 Step RBAC block, got {rbac_resp.status_code}: {rbac_resp.text}"

    step2_resp = await client.post(
        f"{api}/workflows/instances/{wf_inst_id}/approve",
        json={"comment": "Finance controller sign-off"},
        headers=a2_headers,
    )
    step2_resp.raise_for_status()
    assert step2_resp.json()["status"] == "APPROVED"

    canonical_order = await _settle_canonical_o2c(client, admin_headers, api, canonical_order)
    if settled_this_run:
        inst_check = await client.get(
            f"{api}/workflows/instances/{wf_inst_id}", headers=admin_headers
        )
        inst_check.raise_for_status()
        wf_final_status = str(inst_check.json()["status"])
        assert wf_final_status == "CONSUMED"
    else:
        cancel_probe_wf = await client.post(
            f"{api}/workflows/instances/{wf_inst_id}/cancel",
            json={"comment": "Keep reusable probe draft clean across reruns"},
            headers=admin_headers,
        )
        cancel_probe_wf.raise_for_status()
        wf_final_status = str(cancel_probe_wf.json()["status"])
        assert wf_final_status == "CANCELLED"

    tb_resp = await client.get(f"{api}/reports/trial-balance", headers=admin_headers)
    tb_resp.raise_for_status()
    tb_rows: list[dict[str, Any]] = tb_resp.json()
    total_debit = sum((Decimal(r["total_debit"]) for r in tb_rows), Decimal("0"))
    total_credit = sum((Decimal(r["total_credit"]) for r in tb_rows), Decimal("0"))
    assert total_debit == total_credit and total_debit > 0

    inv_resp = await client.get(
        f"{api}/inventory/summary",
        params={"product_id": product_id},
        headers=admin_headers,
    )
    inv_resp.raise_for_status()
    on_hand_after = sum((Decimal(r["on_hand"]) for r in inv_resp.json()), Decimal("0"))

    print(
        f"[Act 3: Workflow + O2C] Gate 409 -> SoD 403 -> Step 1 OK -> Step RBAC 403 -> "
        f"Step 2 APPROVED -> Workflow {wf_final_status}; Trial Balance={total_debit}"
    )
    return (
        canonical_order,
        missing_wf_resp.status_code,
        sod_resp.status_code,
        rbac_resp.status_code,
        wf_final_status,
        total_debit,
        total_credit,
        on_hand_after,
        settled_this_run,
    )


async def _run_act_4_deactivation(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    *,
    contract_ref_def_id: str,
    probe_id: str,
    canonical_id: str,
) -> int:
    """Soft-deactivate one custom field and verify historical retention vs 422 write block."""
    deact_resp = await client.delete(
        f"{api}/custom-fields/{contract_ref_def_id}", headers=admin_headers
    )
    deact_resp.raise_for_status()

    deact_write_resp = await client.patch(
        f"{api}/sales-orders/{probe_id}",
        json={"custom_data": {DEMO_FIELD_CONTRACT_REF: "CTR-9999"}},
        headers=admin_headers,
    )
    assert deact_write_resp.status_code == 422

    hist_resp = await client.get(f"{api}/sales-orders/{canonical_id}", headers=admin_headers)
    hist_resp.raise_for_status()
    assert hist_resp.json()["custom_data"][DEMO_FIELD_CONTRACT_REF] == "CTR-2026"

    print(
        "[Act 4: Cleanup & History] Soft-deactivated plat_contract_ref "
        "(write blocked HTTP 422; historical order metadata intact)."
    )
    return deact_write_resp.status_code


async def _restore_custom_fields(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    initial_cf_by_key: dict[str, dict[str, Any]],
) -> None:
    cf_resp = await client.get(
        f"{api}/custom-fields",
        params={"entity_type": "sales_order"},
        headers=admin_headers,
    )
    cf_resp.raise_for_status()
    for field_def in cf_resp.json():
        key = field_def["field_key"]
        if key not in DEMO_FIELD_KEYS:
            continue
        prior = initial_cf_by_key.get(key)
        if prior is not None and prior["is_active"]:
            patch_resp = await client.patch(
                f"{api}/custom-fields/{field_def['id']}",
                json={
                    "label": prior["label"],
                    "is_required": prior["is_required"],
                    "is_active": True,
                    "default_value": prior["default_value"],
                    "validation_rules": prior["validation_rules"],
                },
                headers=admin_headers,
            )
            patch_resp.raise_for_status()
        elif field_def["is_active"]:
            del_resp = await client.delete(
                f"{api}/custom-fields/{field_def['id']}",
                headers=admin_headers,
            )
            del_resp.raise_for_status()


async def _restore_guardrails(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    *,
    initial_cf_by_key: dict[str, dict[str, Any]],
    initial_plugin: dict[str, Any],
    prior_active_wf: dict[str, Any] | None,
    demo_wf_def: dict[str, Any] | None,
) -> None:
    errors: list[Exception] = []

    try:
        await _restore_custom_fields(client, admin_headers, api, initial_cf_by_key)
    except Exception as exc:
        errors.append(exc)

    try:
        if prior_active_wf is not None:
            prior_steps = [
                {
                    "step_order": s["step_order"],
                    "step_name": s["step_name"],
                    "min_amount": s["min_amount"],
                    "required_permission": s["required_permission"],
                }
                for s in prior_active_wf["steps"]
            ]
            restore_wf = await client.patch(
                f"{api}/workflows/definitions/{prior_active_wf['id']}",
                json={
                    "name": prior_active_wf["name"],
                    "description": prior_active_wf["description"],
                    "is_active": True,
                    "allow_self_approval": prior_active_wf["allow_self_approval"],
                    "steps": prior_steps,
                },
                headers=admin_headers,
            )
            restore_wf.raise_for_status()
        elif demo_wf_def is not None:
            del_wf = await client.delete(
                f"{api}/workflows/definitions/{demo_wf_def['id']}",
                headers=admin_headers,
            )
            del_wf.raise_for_status()
    except Exception as exc:
        errors.append(exc)

    try:
        restore_plugin = await client.patch(
            f"{api}/plugins/credit_limit",
            json={
                "is_enabled": initial_plugin["is_enabled"],
                "config": initial_plugin["config"],
            },
            headers=admin_headers,
        )
        restore_plugin.raise_for_status()
    except Exception as exc:
        errors.append(exc)

    if errors:
        raise errors[0]


async def _snapshot_and_prepare_demo_orders(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    api: str,
    *,
    customer_id: str,
    product_id: str,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any] | None,
]:
    init_plugin_resp = await client.get(f"{api}/plugins/credit_limit", headers=admin_headers)
    init_plugin_resp.raise_for_status()
    initial_plugin: dict[str, Any] = init_plugin_resp.json()

    init_wf_resp = await client.get(
        f"{api}/workflows/definitions",
        params={"entity_type": "sales_order", "transition": "confirm"},
        headers=admin_headers,
    )
    init_wf_resp.raise_for_status()
    initial_wf_defs: list[dict[str, Any]] = init_wf_resp.json()
    prior_active_wf: dict[str, Any] | None = next(
        (d for d in initial_wf_defs if d["is_active"]),
        None,
    )

    init_cf_resp = await client.get(
        f"{api}/custom-fields",
        params={"entity_type": "sales_order"},
        headers=admin_headers,
    )
    init_cf_resp.raise_for_status()
    initial_cf_by_key: dict[str, dict[str, Any]] = {
        row["field_key"]: row for row in init_cf_resp.json() if row["field_key"] in DEMO_FIELD_KEYS
    }

    # Deactivate demo custom fields prior to probe setup so probe creation succeeds cleanly.
    await _deactivate_demo_custom_fields(client, admin_headers, api)

    orders_resp = await client.get(f"{api}/sales-orders", headers=admin_headers)
    orders_resp.raise_for_status()
    cust_orders = [o for o in orders_resp.json() if o["customer_id"] == customer_id]
    draft_orders = [o for o in cust_orders if o["status"] == "draft"]
    settled_orders = [o for o in cust_orders if o["status"] in ("confirmed", "shipped")]
    probe_order = draft_orders[0] if draft_orders else None
    canonical_order = (
        settled_orders[0]
        if settled_orders
        else (draft_orders[1] if len(draft_orders) > 1 else None)
    )

    if probe_order is None:
        probe_create = await client.post(
            f"{api}/sales-orders",
            json={
                "customer_id": customer_id,
                "lines": [{"product_id": product_id, "qty": str(ORDER_QTY)}],
            },
            headers=admin_headers,
        )
        probe_create.raise_for_status()
        probe_order = probe_create.json()

    return (
        initial_plugin,
        initial_wf_defs,
        prior_active_wf,
        initial_cf_by_key,
        probe_order,
        canonical_order,
    )


async def run_demo_platform(
    client: httpx.AsyncClient | None = None,
    *,
    base_url: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> DemoPlatformResult:
    """Execute the full idempotent Phase 2 platform showcase and return key observations."""
    resolved_base_url = _resolve_base_url(base_url)
    api = f"{resolved_base_url}/api/v1"
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)

    try:
        admin_email, admin_password = _load_admin_credentials()
        _validate_approver_env_preconditions(admin_email)

        admin_headers, admin_login = await _login_http(
            client, resolved_base_url, admin_email, admin_password
        )
        company_id = str(admin_login["active_company_id"])

        print(f"=== mini-ERP Phase 2 Platform Demo ({resolved_base_url}) ===")
        print(f"[Setup] Logged in as admin {admin_email} (company_id={company_id})")

        customer, product = await _ensure_prerequisite_masterdata(client, admin_headers, api)
        (
            initial_plugin,
            initial_wf_defs,
            prior_active_wf,
            initial_cf_by_key,
            probe_order,
            canonical_order,
        ) = await _snapshot_and_prepare_demo_orders(
            client,
            admin_headers,
            api,
            customer_id=str(customer["id"]),
            product_id=str(product["id"]),
        )

        probe_id = str(probe_order["id"])
        demo_wf_def: dict[str, Any] | None = None

        try:
            demo_wf_def = await _upsert_demo_workflow(client, admin_headers, api, initial_wf_defs)
            await _cancel_active_instances(
                client,
                admin_headers,
                api,
                entity_id=probe_id,
                comment="Reset probe before Act 1 plugin demo",
            )

            blocked_status, disabled_status = await _run_act_1_plugins(
                client, admin_headers, api, probe_id
            )
            canonical_order, contract_ref_def, cf_reject_statuses = await _run_act_2_customfields(
                client,
                admin_headers,
                api,
                customer_id=str(customer["id"]),
                product_id=str(product["id"]),
                probe_id=probe_id,
                canonical_order=canonical_order,
            )
            (
                canonical_order,
                wf_missing_status,
                wf_sod_status,
                wf_rbac_status,
                wf_final_status,
                total_debit,
                total_credit,
                on_hand_after,
                settled_this_run,
            ) = await _run_act_3_workflow_o2c(
                client,
                admin_headers,
                resolved_base_url,
                api,
                admin_email=admin_email,
                company_id=company_id,
                product_id=str(product["id"]),
                probe_id=probe_id,
                canonical_order=canonical_order,
                session_factory=session_factory,
            )
            deact_reject_status = await _run_act_4_deactivation(
                client,
                admin_headers,
                api,
                contract_ref_def_id=str(contract_ref_def["id"]),
                probe_id=probe_id,
                canonical_id=str(canonical_order["id"]),
            )

            return DemoPlatformResult(
                company_id=company_id,
                canonical_order_id=str(canonical_order["id"]),
                canonical_order_no=str(canonical_order["order_no"]),
                probe_order_id=probe_id,
                credit_limit_blocked_status=blocked_status,
                plugin_disabled_workflow_status=disabled_status,
                customfield_reject_statuses=cf_reject_statuses,
                customfield_normalized_cap=str(
                    canonical_order["custom_data"][DEMO_FIELD_DISCOUNT_CAP]
                ),
                customfield_deactivated_reject_status=deact_reject_status,
                workflow_missing_status=wf_missing_status,
                workflow_sod_status=wf_sod_status,
                workflow_step_rbac_status=wf_rbac_status,
                workflow_final_status=wf_final_status,
                trial_balance_debit=f"{total_debit:.6f}",
                trial_balance_credit=f"{total_credit:.6f}",
                stock_on_hand=f"{on_hand_after:.6f}",
                settled_canonical_in_this_run=settled_this_run,
            )
        finally:
            await _restore_guardrails(
                client,
                admin_headers,
                api,
                initial_cf_by_key=initial_cf_by_key,
                initial_plugin=initial_plugin,
                prior_active_wf=prior_active_wf,
                demo_wf_def=demo_wf_def,
            )
    finally:
        if owns_client:
            await client.aclose()


def main() -> int:
    try:
        result = asyncio.run(run_demo_platform())
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
        f"Demo complete: Order {result.canonical_order_no} ({result.canonical_order_id}), "
        f"TrialBalance={result.trial_balance_debit}/{result.trial_balance_credit}, "
        f"StockOnHand={result.stock_on_hand}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
