"""tests/payables/test_bills.py — Vendor bills, GR/IR clearing, control accounts (ADR-015)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.permissions.models import RolePermission
from tests.conftest import company_headers
from tests.payables._helpers import (
    allocation,
    create_payment,
    get_trial_balance_by_code,
    issue_bill,
    setup_received_order,
    void_bill,
)
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)
from tests.purchase._helpers import create_draft_order, order_line


@pytest.mark.asyncio
async def test_issue_vendor_bill_clears_grir_to_accounts_payable(client: AsyncClient) -> None:
    ctx = await setup_received_order(
        client, "BILL1", standard_cost="40", qty="3", payment_terms_days=45
    )
    resp = await issue_bill(
        client,
        ctx.company_id,
        ctx.order["id"],
        supplier_invoice_no="  SUP-INV-001  ",
    )
    assert resp.status_code == 201, resp.text
    bill = resp.json()
    assert bill["bill_no"].startswith("BILL-")
    assert bill["status"] == "open"
    assert bill["supplier_invoice_no"] == "SUP-INV-001"
    assert Decimal(bill["total"]) == Decimal("120.000000")
    assert Decimal(bill["settled_amount"]) == Decimal("0")

    bill_dt = datetime.fromisoformat(bill["bill_date"])
    due_dt = datetime.fromisoformat(bill["due_date"])
    assert (due_dt - bill_dt).days == 45

    tb = await get_trial_balance_by_code(client, ctx.company_id)
    assert tb["2110"]["total_debit"] == "120.000000"
    assert tb["2110"]["total_credit"] == "120.000000"
    assert tb["2000"]["total_credit"] == "120.000000"
    assert tb["2000"]["total_debit"] == "0.000000"


@pytest.mark.asyncio
async def test_bill_rejects_non_received_po_and_invalid_dates(client: AsyncClient) -> None:
    ctx = await setup_received_order(client, "BILL2", standard_cost="50", qty="2")
    draft = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(ctx.product_id, "1")]
    )
    assert (await issue_bill(client, ctx.company_id, draft["id"])).status_code == 422

    received_dt = datetime.fromisoformat(ctx.order["received_at"]).astimezone(timezone.utc)  # noqa: UP017
    before_receipt = (received_dt.date() - timedelta(days=1)).isoformat()
    r_before = await issue_bill(client, ctx.company_id, ctx.order["id"], bill_date=before_receipt)
    assert r_before.status_code == 422

    today_utc = datetime.now(timezone.utc).date()  # noqa: UP017
    r_due = await issue_bill(
        client,
        ctx.company_id,
        ctx.order["id"],
        bill_date=today_utc.isoformat(),
        due_date=(today_utc - timedelta(days=1)).isoformat(),
    )
    assert r_due.status_code == 422


@pytest.mark.asyncio
async def test_double_bill_and_supplier_invoice_no_uniqueness_and_void_reuse(
    client: AsyncClient,
) -> None:
    ctx = await setup_received_order(client, "BILL3", standard_cost="30", qty="2")
    # Blank supplier_invoice_no normalizes to None -> distinct NULLs allowed
    first = await issue_bill(client, ctx.company_id, ctx.order["id"], supplier_invoice_no="   ")
    assert first.status_code == 201, first.text
    assert first.json()["supplier_invoice_no"] is None

    # Duplicate live bill for same PO -> 409
    dup_po = await issue_bill(
        client, ctx.company_id, ctx.order["id"], supplier_invoice_no="INV-DUP-TEST"
    )
    assert dup_po.status_code == 409

    # Void first bill, then re-bill with explicit supplier_invoice_no
    v1 = await void_bill(client, ctx.company_id, first.json()["id"])
    assert v1.status_code == 200
    assert v1.json()["status"] == "voided"

    rebilled = await issue_bill(
        client, ctx.company_id, ctx.order["id"], supplier_invoice_no="EXT-001"
    )
    assert rebilled.status_code == 201, rebilled.text

    # Second PO with same supplier_invoice_no is rejected 409 while first is live
    po2 = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(ctx.product_id, "1")]
    )
    assert (
        await client.post(
            f"/api/v1/purchase-orders/{po2['id']}/confirm",
            headers=company_headers(ctx.company_id),
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/purchase-orders/{po2['id']}/receive",
            headers=company_headers(ctx.company_id),
        )
    ).status_code == 200
    dup_ref = await issue_bill(client, ctx.company_id, po2["id"], supplier_invoice_no="EXT-001")
    assert dup_ref.status_code == 409

    # Void rebilled -> EXT-001 can now be reused on po2
    assert (await void_bill(client, ctx.company_id, rebilled.json()["id"])).status_code == 200
    reused = await issue_bill(client, ctx.company_id, po2["id"], supplier_invoice_no="EXT-001")
    assert reused.status_code == 201, reused.text


@pytest.mark.asyncio
async def test_void_bill_contra_entry_and_reject_when_settled(client: AsyncClient) -> None:
    ctx = await setup_received_order(client, "BILL4", standard_cost="25", qty="4")
    bill = (await issue_bill(client, ctx.company_id, ctx.order["id"])).json()

    # Partial inline payment settles 40
    pay = await create_payment(
        client,
        ctx.company_id,
        ctx.supplier_id,
        "40",
        "BILL4-PAY",
        allocations=[allocation(bill["id"], "40")],
    )
    assert pay.status_code == 201, pay.text
    assert (await void_bill(client, ctx.company_id, bill["id"])).status_code == 409

    # New order + unallocated bill -> void reverses 2110 / 2000 cleanly
    po2 = await create_draft_order(
        client, ctx.company_id, ctx.supplier_id, [order_line(ctx.product_id, "2")]
    )
    assert (
        await client.post(
            f"/api/v1/purchase-orders/{po2['id']}/confirm",
            headers=company_headers(ctx.company_id),
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/purchase-orders/{po2['id']}/receive",
            headers=company_headers(ctx.company_id),
        )
    ).status_code == 200
    b2 = (await issue_bill(client, ctx.company_id, po2["id"])).json()
    vb2 = await void_bill(client, ctx.company_id, b2["id"])
    assert vb2.status_code == 200
    assert vb2.json()["status"] == "voided"
    assert (await void_bill(client, ctx.company_id, b2["id"])).status_code == 409


@pytest.mark.asyncio
async def test_control_accounts_2000_and_2110_reject_manual_entries_and_reversals(
    client: AsyncClient,
) -> None:
    ctx = await setup_received_order(client, "CTRLAP", standard_cost="60", qty="1")
    accounts_resp = await client.get("/api/v1/accounts", headers=company_headers(ctx.company_id))
    by_code = {a["code"]: a for a in accounts_resp.json()}
    cash_id = by_code["1000"]["id"]
    today_iso = datetime.now(timezone.utc).date().isoformat()  # noqa: UP017

    for ctrl_code in ("2000", "2110"):
        bad_je = await client.post(
            "/api/v1/journal-entries",
            json={
                "entry_date": today_iso,
                "lines": [
                    {
                        "account_id": cash_id,
                        "currency_code": "TWD",
                        "txn_debit": "10",
                        "txn_credit": "0",
                        "debit": "10",
                        "credit": "0",
                        "exchange_rate": "1",
                    },
                    {
                        "account_id": by_code[ctrl_code]["id"],
                        "currency_code": "TWD",
                        "txn_debit": "0",
                        "txn_credit": "10",
                        "debit": "0",
                        "credit": "10",
                        "exchange_rate": "1",
                    },
                ],
            },
            headers=company_headers(ctx.company_id),
        )
        assert bad_je.status_code == 422

    # Manual reversal of event-posted GR/IR (2110) or AP bill (2000) is forbidden (422)
    assert (await issue_bill(client, ctx.company_id, ctx.order["id"])).status_code == 201
    jes = (
        await client.get("/api/v1/journal-entries", headers=company_headers(ctx.company_id))
    ).json()
    assert len(jes) == 2
    for je in jes:
        rev = await client.post(
            f"/api/v1/journal-entries/{je['id']}/reverse",
            headers=company_headers(ctx.company_id),
        )
        assert rev.status_code == 422

    # Workflow target resolver must also reject cleanly (422, not 500 MultipleResultsFound)
    # even on VendorBill clearing entries that touch BOTH 2000 and 2110.
    wf_rev = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "journal_entry",
            "transition": "reverse",
            "name": "JE Reverse Gate",
            "allow_self_approval": False,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Controller",
                    "min_amount": "1.000000",
                    "required_permission": "ledger.journal_entry.reverse",
                }
            ],
        },
        headers=company_headers(ctx.company_id),
    )
    assert wf_rev.status_code == 201, wf_rev.text
    for je in jes:
        wf_inst = await client.post(
            "/api/v1/workflows/instances",
            json={
                "entity_type": "journal_entry",
                "entity_id": je["id"],
                "transition": "reverse",
            },
            headers=company_headers(ctx.company_id),
        )
        assert wf_inst.status_code == 422


@pytest.mark.asyncio
async def test_vendor_bill_custom_fields_and_workflow_sod_and_rbac(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ctx = await setup_received_order(client, "BILLWF", standard_cost="80", qty="2")
    headers = company_headers(ctx.company_id)

    cf_resp = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "vendor_bill",
            "field_key": "tax_rate_pct",
            "label": "Tax Rate",
            "field_type": "DECIMAL",
            "is_required": True,
        },
        headers=headers,
    )
    assert cf_resp.status_code == 201, cf_resp.text
    assert (await issue_bill(client, ctx.company_id, ctx.order["id"])).status_code == 422

    bill_resp = await issue_bill(
        client,
        ctx.company_id,
        ctx.order["id"],
        custom_data={"tax_rate_pct": "5.1234565"},
    )
    assert bill_resp.status_code == 201, bill_resp.text
    assert bill_resp.json()["custom_data"]["tax_rate_pct"] == "5.123456"
    bill_id = bill_resp.json()["id"]

    wf_resp = await client.post(
        "/api/v1/workflows/definitions",
        json={
            "entity_type": "vendor_bill",
            "transition": "void",
            "name": "AP Bill Void Gate",
            "allow_self_approval": False,
            "steps": [
                {
                    "step_order": 1,
                    "step_name": "Finance Void Approval",
                    "min_amount": "100.000000",
                    "required_permission": "platform.workflow.approve",
                }
            ],
        },
        headers=headers,
    )
    assert wf_resp.status_code == 201, wf_resp.text
    assert (await void_bill(client, ctx.company_id, bill_id)).status_code == 409

    inst = (
        await client.post(
            "/api/v1/workflows/instances",
            json={"entity_type": "vendor_bill", "entity_id": bill_id, "transition": "void"},
            headers=headers,
        )
    ).json()
    assert (
        await client.post(f"/api/v1/workflows/instances/{inst['id']}/approve", headers=headers)
    ).status_code == 403

    role_code = f"ap_approver_{uuid.uuid4().hex[:6]}"
    await create_role_without_permissions(db_session, role_code)
    db_session.add(RolePermission(role_code=role_code, permission_code="platform.workflow.approve"))
    await db_session.flush()
    email = f"ap_mgr_{uuid.uuid4().hex[:6]}@example.com"
    await create_user_with_role(
        db_session, email=email, password="pw", company_id=ctx.company_id, role_code=role_code
    )
    await db_session.commit()

    mgr_token = (await client.post("/auth/login", json={"email": email, "password": "pw"})).json()[
        "access_token"
    ]
    mgr_headers = {"Authorization": f"Bearer {mgr_token}"}

    # Approver lacks payables.bill.read/void -> 403 on bills API, can approve workflow step
    assert (await client.get("/api/v1/payables/bills", headers=mgr_headers)).status_code == 403
    assert (
        await client.post(f"/api/v1/workflows/instances/{inst['id']}/approve", headers=mgr_headers)
    ).status_code == 200

    voided = await void_bill(client, ctx.company_id, bill_id)
    assert voided.status_code == 200
    assert voided.json()["status"] == "voided"
