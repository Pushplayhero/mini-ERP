"""tests/purchase/test_orders_api.py — PO CRUD, lifecycle, hooks & workflow (ADR-014)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks
from app.core.exceptions import ConflictError
from app.core.hooks import HookContext
from app.modules.masterdata.models import OutboxEvent
from app.modules.purchase.service import PURCHASE_ORDER_VALIDATE_CONFIRM
from app.platform.permissions.models import RolePermission
from tests.conftest import company_headers
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)
from tests.purchase._helpers import (
    cancel_order,
    confirm_order,
    create_company,
    create_draft_order,
    create_product,
    create_supplier,
    order_line,
)


@pytest.mark.asyncio
async def test_create_draft_po_computes_total_and_defaults_unit_cost(client: AsyncClient) -> None:
    company_id = await create_company(client, "POA1")
    supplier_id = await create_supplier(client, company_id, "SUPA1")
    product_id = await create_product(client, company_id, "PSKU-A1", standard_cost="42.5")

    order = await create_draft_order(client, company_id, supplier_id, [order_line(product_id, "2")])

    assert order["status"] == "draft"
    assert order["order_no"].startswith("PO-")
    assert Decimal(order["lines"][0]["unit_cost"]) == Decimal("42.5")
    assert order["lines"][0]["unit_cost_is_override"] is False
    assert Decimal(order["lines"][0]["amount"]) == Decimal("85.0")
    assert Decimal(order["total"]) == Decimal("85.0")


@pytest.mark.asyncio
async def test_create_draft_po_ignores_client_supplied_total_and_honors_override(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "POA2")
    supplier_id = await create_supplier(client, company_id, "SUPA2")
    product_id = await create_product(client, company_id, "PSKU-A2", standard_cost="30")

    resp = await client.post(
        "/api/v1/purchase-orders",
        json={
            "supplier_id": str(supplier_id),
            "lines": [order_line(product_id, "3", unit_cost="25")],
            "total": "999999",
        },
        headers=company_headers(company_id),
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert Decimal(order["total"]) == Decimal("75")
    assert order["lines"][0]["unit_cost_is_override"] is True


def test_purchase_order_line_schema_rounds_half_even_not_half_up() -> None:
    from app.modules.purchase.schemas import PurchaseOrderLineCreate

    line = PurchaseOrderLineCreate(
        product_id=uuid.uuid4(), qty=Decimal("1"), unit_cost=Decimal("10.0000025")
    )
    assert line.unit_cost == Decimal("10.000002")


@pytest.mark.asyncio
async def test_unit_cost_override_rounds_half_even_to_6dp_http(client: AsyncClient) -> None:
    company_id = await create_company(client, "POA3")
    supplier_id = await create_supplier(client, company_id, "SUPA3")
    product_id = await create_product(client, company_id, "PSKU-A3", standard_cost="50")

    order = await create_draft_order(
        client,
        company_id,
        supplier_id,
        [order_line(product_id, "1", unit_cost="10.0000025")],
    )
    assert Decimal(order["lines"][0]["unit_cost"]) == Decimal("10.000002")


@pytest.mark.asyncio
async def test_draft_po_can_be_edited_and_confirmed_reprices_non_override_lines(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(client, "POB1")
    supplier_id = await create_supplier(client, company_id, "SUPB1", name="Original Vendor Name")
    p_default = await create_product(client, company_id, "PSKU-DEF", standard_cost="30")
    p_override = await create_product(client, company_id, "PSKU-OVR", standard_cost="30")

    # Zero-line draft allowed
    order = await create_draft_order(client, company_id, supplier_id, [])
    assert order["lines"] == []

    # Empty lines confirm rejected (422)
    empty_confirm = await confirm_order(client, company_id, order["id"])
    assert empty_confirm.status_code == 422

    # Populate lines via PATCH
    patch_resp = await client.patch(
        f"/api/v1/purchase-orders/{order['id']}",
        json={
            "lines": [
                order_line(p_default, "2"),  # non-override, initially 30 -> 60
                order_line(p_override, "1", unit_cost="45"),  # override -> 45
            ]
        },
        headers=company_headers(company_id),
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert Decimal(patch_resp.json()["total"]) == Decimal("105")

    # Change product standard_cost and supplier name before confirm
    headers = company_headers(company_id)
    await client.patch(
        f"/api/v1/products/{p_default}",
        json={"standard_cost": "40"},
        headers=headers,
    )
    await client.patch(
        f"/api/v1/products/{p_override}",
        json={"standard_cost": "99"},
        headers=headers,
    )
    await client.patch(
        f"/api/v1/suppliers/{supplier_id}",
        json={"name": "Confirmed Snapshot Vendor"},
        headers=headers,
    )

    confirm_resp = await confirm_order(client, company_id, order["id"])
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_at"] is not None
    assert confirmed["snapshot_supplier_code"] == "SUPB1"
    assert confirmed["snapshot_supplier_name"] == "Confirmed Snapshot Vendor"
    # Repriced default line (2 * 40 = 80) + unchanged override line (1 * 45 = 45) = 125
    assert Decimal(confirmed["lines"][0]["unit_cost"]) == Decimal("40")
    assert Decimal(confirmed["lines"][0]["amount"]) == Decimal("80")
    assert Decimal(confirmed["lines"][1]["unit_cost"]) == Decimal("45")
    assert Decimal(confirmed["lines"][1]["amount"]) == Decimal("45")
    assert Decimal(confirmed["total"]) == Decimal("125")

    # Subsequent vendor rename does NOT alter frozen snapshot on confirmed PO
    await client.patch(
        f"/api/v1/suppliers/{supplier_id}",
        json={"name": "Later Renamed Vendor"},
        headers=headers,
    )
    get_resp = await client.get(f"/api/v1/purchase-orders/{order['id']}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["snapshot_supplier_name"] == "Confirmed Snapshot Vendor"

    # Confirmed PO cannot be edited via PATCH
    bad_patch = await client.patch(
        f"/api/v1/purchase-orders/{order['id']}",
        json={"lines": [order_line(p_default, "1")]},
        headers=headers,
    )
    assert bad_patch.status_code == 409

    # Verify purchase.order_confirmed outbox event exists
    outbox_rows = (
        (
            await db_session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "purchase.order_confirmed")
            )
        )
        .scalars()
        .all()
    )
    assert any(row.payload.get("source_id") == order["id"] for row in outbox_rows)


@pytest.mark.asyncio
async def test_confirm_po_with_zero_total_is_rejected_422(client: AsyncClient) -> None:
    company_id = await create_company(client, "POZERO")
    supplier_id = await create_supplier(client, company_id, "SUPZ1")
    product_id = await create_product(client, company_id, "PSKU-Z1", standard_cost="0")

    order = await create_draft_order(client, company_id, supplier_id, [order_line(product_id, "5")])
    resp = await confirm_order(client, company_id, order["id"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_purchase_order_validate_confirm_hook_receives_supplier_id_and_can_veto(
    client: AsyncClient,
) -> None:
    company_id = await create_company(client, "POHOOK")
    supplier_id = await create_supplier(client, company_id, "SUPHK")
    product_id = await create_product(client, company_id, "PSKU-HK", standard_cost="50")

    order = await create_draft_order(client, company_id, supplier_id, [order_line(product_id, "2")])

    seen: list[HookContext] = []

    class VendorBlockedError(ConflictError):
        pass

    async def veto_hook(_session: AsyncSession, ctx: HookContext) -> None:
        seen.append(ctx)
        raise VendorBlockedError("Vendor approval suspended by plugin")

    hooks.register(PURCHASE_ORDER_VALIDATE_CONFIRM, veto_hook)
    try:
        resp = await confirm_order(client, company_id, order["id"])
        assert resp.status_code == 409
        assert "Vendor approval suspended" in resp.json()["detail"]
        assert len(seen) == 1
        assert seen[0].supplier_id == supplier_id
        assert seen[0].customer_id == supplier_id
        assert seen[0].total == Decimal("100")

        # PO remained draft
        get_resp = await client.get(
            f"/api/v1/purchase-orders/{order['id']}", headers=company_headers(company_id)
        )
        assert get_resp.json()["status"] == "draft"
    finally:
        hooks.unregister(PURCHASE_ORDER_VALIDATE_CONFIRM, veto_hook)


@pytest.mark.asyncio
async def test_purchase_order_custom_fields_and_workflow_sod_lifecycle(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(client, "POWF")
    admin_headers = company_headers(company_id)
    supplier_id = await create_supplier(client, company_id, "SUPWF")
    product_id = await create_product(client, company_id, "PSKU-WF", standard_cost="100")

    # 1. Custom field definition for purchase_order
    cf_resp = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "purchase_order",
            "field_key": "cost_center",
            "label": "Cost Center",
            "field_type": "TEXT",
            "is_required": True,
        },
        headers=admin_headers,
    )
    assert cf_resp.status_code == 201, cf_resp.text

    # Missing required custom field -> 422
    bad_po = await client.post(
        "/api/v1/purchase-orders",
        json={"supplier_id": str(supplier_id), "lines": [order_line(product_id, "5")]},
        headers=admin_headers,
    )
    assert bad_po.status_code == 422

    order = await create_draft_order(
        client,
        company_id,
        supplier_id,
        [order_line(product_id, "5")],  # total 500
        custom_data={"cost_center": "MFG-01"},
    )
    assert order["custom_data"]["cost_center"] == "MFG-01"

    # 2. Define workflow for ("purchase_order", "confirm") & ("purchase_order", "cancel")
    wf_confirm = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "purchase_order",
            "transition": "confirm",
            "name": "PO Confirm Guard",
            "allow_self_approval": False,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Procurement Manager",
                    "min_amount": "100.000000",
                    "required_permission": "purchase.order.confirm",
                }
            ],
        },
        headers=admin_headers,
    )
    assert wf_confirm.status_code == 201, wf_confirm.text

    # Confirm blocked without approved workflow -> 409
    blocked = await confirm_order(client, company_id, order["id"])
    assert blocked.status_code == 409

    # Create a second approver user in the same company for SoD
    await create_role_without_permissions(db_session, "po_approver")
    for code in ("platform.workflow.read", "platform.workflow.approve", "purchase.order.confirm"):
        db_session.add(RolePermission(role_code="po_approver", permission_code=code))
    await db_session.commit()

    await create_user_with_role(
        db_session,
        email="po_mgr@example.com",
        password="mgrpassword123",
        company_id=company_id,
        role_code="po_approver",
    )
    mgr_login = await client.post(
        "/auth/login",
        json={"email": "po_mgr@example.com", "password": "mgrpassword123"},
    )
    assert mgr_login.status_code == 200
    mgr_headers = {"Authorization": f"Bearer {mgr_login.json()['access_token']}"}

    # Admin submits workflow instance
    submit_resp = await client.post(
        "/api/v1/workflows/instances",
        json={
            "entity_type": "purchase_order",
            "entity_id": order["id"],
            "transition": "confirm",
        },
        headers=admin_headers,
    )
    assert submit_resp.status_code == 201, submit_resp.text
    instance_id = submit_resp.json()["id"]

    # Admin tries to self-approve -> 403 SoD violation
    self_approve = await client.post(
        f"/api/v1/workflows/instances/{instance_id}/approve",
        json={"comment": "self check"},
        headers=admin_headers,
    )
    assert self_approve.status_code == 403

    # Manager approves -> status APPROVED
    mgr_approve = await client.post(
        f"/api/v1/workflows/instances/{instance_id}/approve",
        json={"comment": "approved by procurement manager"},
        headers=mgr_headers,
    )
    assert mgr_approve.status_code == 200
    assert mgr_approve.json()["status"] == "APPROVED"

    # Admin confirms PO -> 200 & workflow instance becomes CONSUMED
    ok_confirm = await confirm_order(client, company_id, order["id"])
    assert ok_confirm.status_code == 200, ok_confirm.text
    assert ok_confirm.json()["status"] == "confirmed"

    inst_after = await client.get(
        f"/api/v1/workflows/instances/{instance_id}", headers=admin_headers
    )
    assert inst_after.status_code == 200
    assert inst_after.json()["status"] == "CONSUMED"


@pytest.mark.asyncio
async def test_cancel_purchase_order_and_cross_company_isolation(client: AsyncClient) -> None:
    company_a = await create_company(client, "POCXA")
    company_b = await create_company(client, "POCXB")
    supplier_a = await create_supplier(client, company_a, "SUPCA")
    product_a = await create_product(client, company_a, "PSKU-CA", standard_cost="25")

    order_a = await create_draft_order(client, company_a, supplier_a, [order_line(product_a, "2")])
    oid = order_a["id"]

    # Cross-company read/patch/confirm/cancel/receive all 404
    headers_b = company_headers(company_b)
    assert (
        await client.get(f"/api/v1/purchase-orders/{oid}", headers=headers_b)
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/purchase-orders/{oid}", json={"custom_data": {}}, headers=headers_b
        )
    ).status_code == 404
    assert (await confirm_order(client, company_b, oid)).status_code == 404
    assert (await cancel_order(client, company_b, oid)).status_code == 404
    assert (
        await client.post(f"/api/v1/purchase-orders/{oid}/receive", headers=headers_b)
    ).status_code == 404
    assert (await client.get("/api/v1/purchase-orders", headers=headers_b)).json() == []

    # Cancel draft -> cancelled, second cancel -> 409
    c1 = await cancel_order(client, company_a, oid)
    assert c1.status_code == 200
    assert c1.json()["status"] == "cancelled"
    assert c1.json()["cancelled_at"] is not None

    c2 = await cancel_order(client, company_a, oid)
    assert c2.status_code == 409

    # Cancel from confirmed also works (before receive)
    order_a2 = await create_draft_order(client, company_a, supplier_a, [order_line(product_a, "1")])
    assert (await confirm_order(client, company_a, order_a2["id"])).status_code == 200
    c3 = await cancel_order(client, company_a, order_a2["id"])
    assert c3.status_code == 200
    assert c3.json()["status"] == "cancelled"


def test_purchase_export_and_workflow_target_errors() -> None:
    from app.modules.purchase.models import PurchaseOrderStatus

    assert {s.value for s in PurchaseOrderStatus} == {"draft", "confirmed", "cancelled", "received"}
