"""tests/platform/test_customfields_business_integration.py

Comprehensive integration testing for tenant-scoped custom fields across all 8 business entities:
1. `customer` (`app.modules.masterdata`)
2. `product` (`app.modules.masterdata`)
3. `account` (`app.modules.masterdata`)
4. `accounting_period` (`app.modules.ledger`)
5. `journal_entry` (`app.modules.ledger`) - manual validation vs automated posting compatibility
6. `sales_order` (`app.modules.sales`)
7. `invoice` (`app.modules.receivables`)
8. `payment` (`app.modules.receivables`)
9. Cross-company tenant isolation
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import actor_context
from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from app.modules.ledger.schemas import (
    JournalEntryCreate,
    JournalLineCreate,
)
from tests.conftest import company_headers
from tests.inventory._helpers import create_adjustment
from tests.ledger._helpers import create_account
from tests.sales._helpers import (
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
    uom_id,
)


async def _define_field(
    client: AsyncClient,
    company_id: uuid.UUID,
    *,
    entity_type: str,
    field_key: str,
    label: str,
    field_type: str,
    is_required: bool = False,
    default_value: Any = None,
    validation_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": entity_type,
            "field_key": field_key,
            "label": label,
            "field_type": field_type,
            "is_required": is_required,
            "default_value": default_value,
            "validation_rules": validation_rules or {},
        },
        headers=company_headers(company_id),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_customer_custom_fields_e2e(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # Define custom fields for customer
    await _define_field(
        client,
        company_id,
        entity_type="customer",
        field_key="tier",
        label="Customer Tier",
        field_type="ENUM",
        is_required=True,
        default_value="SILVER",
        validation_rules={"choices": ["PLATINUM", "GOLD", "SILVER"]},
    )
    await _define_field(
        client,
        company_id,
        entity_type="customer",
        field_key="discount_rate",
        label="Discount Rate",
        field_type="DECIMAL",
        validation_rules={"min_value": "0.000000", "max_value": "100.000000"},
    )
    await _define_field(
        client,
        company_id,
        entity_type="customer",
        field_key="internal_note",
        label="Internal Note",
        field_type="TEXT",
        is_required=False,
    )

    # 1. Create with empty custom_data -> default 'SILVER' applied
    res1 = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST01",
            "name": "Customer 1",
            "currency_code": "TWD",
            "credit_limit": "5000",
            "custom_data": {},
        },
        headers=company_headers(company_id),
    )
    assert res1.status_code == 201, res1.text
    cust1 = res1.json()
    assert cust1["custom_data"]["tier"] == "SILVER"

    # 2. Unknown field -> 422
    res_bad = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST02",
            "name": "Customer 2",
            "currency_code": "TWD",
            "custom_data": {"unknown_field_xyz": "foo"},
        },
        headers=company_headers(company_id),
    )
    assert res_bad.status_code == 422
    assert "Unknown custom field" in res_bad.json()["detail"]

    # 3. Invalid enum choice -> 422
    res_bad_enum = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST03",
            "name": "Customer 3",
            "currency_code": "TWD",
            "custom_data": {"tier": "BRONZE"},
        },
        headers=company_headers(company_id),
    )
    assert res_bad_enum.status_code == 422
    assert "is not an allowed choice in" in res_bad_enum.json()["detail"]

    # 4. Decimal Banker's Rounding on custom field
    res2 = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST04",
            "name": "Customer 4",
            "currency_code": "TWD",
            "custom_data": {
                "tier": "GOLD",
                "discount_rate": "12.3456785",  # rounds to 12.345678
                "internal_note": "VIP account",
            },
        },
        headers=company_headers(company_id),
    )
    assert res2.status_code == 201, res2.text
    cust2 = res2.json()
    assert cust2["custom_data"]["tier"] == "GOLD"
    assert cust2["custom_data"]["discount_rate"] == "12.345678"
    assert cust2["custom_data"]["internal_note"] == "VIP account"

    # 5. Partial update merges existing custom_data
    cust2_id = cust2["id"]
    res_patch = await client.patch(
        f"/api/v1/customers/{cust2_id}",
        json={"custom_data": {"internal_note": "Updated VIP account"}},
        headers=company_headers(company_id),
    )
    assert res_patch.status_code == 200, res_patch.text
    updated = res_patch.json()
    assert updated["custom_data"]["tier"] == "GOLD"
    assert updated["custom_data"]["discount_rate"] == "12.345678"
    assert updated["custom_data"]["internal_note"] == "Updated VIP account"

    # 6. Clear optional field with null
    res_clear = await client.patch(
        f"/api/v1/customers/{cust2_id}",
        json={"custom_data": {"internal_note": None}},
        headers=company_headers(company_id),
    )
    assert res_clear.status_code == 200, res_clear.text
    cleared = res_clear.json()
    assert cleared["custom_data"]["internal_note"] is None
    assert cleared["custom_data"]["tier"] == "GOLD"

    # 7. Attempt to set required field to null -> 422
    res_clear_req = await client.patch(
        f"/api/v1/customers/{cust2_id}",
        json={"custom_data": {"tier": None}},
        headers=company_headers(company_id),
    )
    assert res_clear_req.status_code == 422
    assert "is required and cannot be null" in res_clear_req.json()["detail"]


@pytest.mark.asyncio
async def test_product_custom_fields_e2e(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    uom = await uom_id(client, company_id)

    # Define boolean & date custom fields
    await _define_field(
        client,
        company_id,
        entity_type="product",
        field_key="is_fragile",
        label="Is Fragile",
        field_type="BOOLEAN",
        default_value=False,
    )
    await _define_field(
        client,
        company_id,
        entity_type="product",
        field_key="expiration_date",
        label="Expiration Date",
        field_type="DATE",
        is_required=False,
    )

    # 1. Create with defaults
    res1 = await client.post(
        "/api/v1/products",
        json={
            "sku": "PROD01",
            "name": "Standard Product",
            "uom_id": str(uom),
            "list_price": "100.00",
        },
        headers=company_headers(company_id),
    )
    assert res1.status_code == 201, res1.text
    prod1 = res1.json()
    assert prod1["custom_data"]["is_fragile"] is False

    # 2. Create with date string
    res2 = await client.post(
        "/api/v1/products",
        json={
            "sku": "PROD02",
            "name": "Perishable Product",
            "uom_id": str(uom),
            "list_price": "250.00",
            "custom_data": {
                "is_fragile": True,
                "expiration_date": "2027-12-31",
            },
        },
        headers=company_headers(company_id),
    )
    assert res2.status_code == 201, res2.text
    prod2 = res2.json()
    assert prod2["custom_data"]["is_fragile"] is True
    assert prod2["custom_data"]["expiration_date"] == "2027-12-31"

    # 3. Update boolean field
    res_patch = await client.patch(
        f"/api/v1/products/{prod1['id']}",
        json={"custom_data": {"is_fragile": True}},
        headers=company_headers(company_id),
    )
    assert res_patch.status_code == 200, res_patch.text
    assert res_patch.json()["custom_data"]["is_fragile"] is True


@pytest.mark.asyncio
async def test_account_and_period_custom_fields_e2e(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Account custom field
    await _define_field(
        client,
        company_id,
        entity_type="account",
        field_key="department",
        label="Department Code",
        field_type="TEXT",
        is_required=True,
        default_value="CORP",
    )

    res_acc = await client.post(
        "/api/v1/accounts",
        json={
            "code": "1999",
            "name": "Special Clearing",
            "type": "asset",
        },
        headers=company_headers(company_id),
    )
    assert res_acc.status_code == 201, res_acc.text
    assert res_acc.json()["custom_data"]["department"] == "CORP"

    # 2. AccountingPeriod custom field
    await _define_field(
        client,
        company_id,
        entity_type="accounting_period",
        field_key="is_audited",
        label="Is Audited",
        field_type="BOOLEAN",
        default_value=False,
    )

    res_period = await client.post(
        "/api/v1/periods",
        json={"year": 2026, "month": 8},
        headers=company_headers(company_id),
    )
    assert res_period.status_code == 201, res_period.text
    assert res_period.json()["custom_data"]["is_audited"] is False


@pytest.mark.asyncio
async def test_journal_entry_custom_fields_and_system_postings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # Create period 2026-08
    await client.post(
        "/api/v1/periods",
        json={"year": 2026, "month": 8},
        headers=company_headers(company_id),
    )

    # Create two accounts (Asset 1000 and Equity 3000)
    acc1_res = await client.post(
        "/api/v1/accounts",
        json={"code": "1000", "name": "Cash", "type": "asset"},
        headers=company_headers(company_id),
    )
    acc1_id = acc1_res.json()["id"]

    acc2_res = await client.post(
        "/api/v1/accounts",
        json={"code": "3000", "name": "Common Stock", "type": "equity"},
        headers=company_headers(company_id),
    )
    acc2_id = acc2_res.json()["id"]

    # Define custom field on journal_entry: REQUIRED WITHOUT DEFAULT VALUE
    await _define_field(
        client,
        company_id,
        entity_type="journal_entry",
        field_key="approval_ticket",
        label="Approval Ticket",
        field_type="TEXT",
        is_required=True,
        default_value=None,  # strictly required
    )

    # 1. Manual entry missing required field -> 422
    manual_lines = [
        {
            "account_id": acc1_id,
            "currency_code": "TWD",
            "txn_debit": "1000.000000",
            "txn_credit": "0.000000",
            "debit": "1000.000000",
            "credit": "0.000000",
        },
        {
            "account_id": acc2_id,
            "currency_code": "TWD",
            "txn_debit": "0.000000",
            "txn_credit": "1000.000000",
            "debit": "0.000000",
            "credit": "1000.000000",
        },
    ]
    res_blocked = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-08-15",
            "lines": manual_lines,
            "custom_data": {},
        },
        headers=company_headers(company_id),
    )
    assert res_blocked.status_code == 422
    assert "Custom field 'approval_ticket' is required" in res_blocked.json()["detail"]

    # 2. Manual entry with approval_ticket -> 201
    res_ok = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-08-15",
            "lines": manual_lines,
            "custom_data": {"approval_ticket": "TICK-8888"},
        },
        headers=company_headers(company_id),
    )
    assert res_ok.status_code == 201, res_ok.text
    entry = res_ok.json()
    assert entry["custom_data"]["approval_ticket"] == "TICK-8888"

    # 3. System / rule-driven event posting:
    # Service call with source_type != None and custom_data={}
    # Must NOT fail even though approval_ticket has no default and is required for manual entries!
    admin_auth = company_headers(company_id)
    with (
        actor_context(manage_admin_actor(admin_auth)),
        company_context(company_id),
    ):
        system_entry = await ledger_service.post_journal_entry(
            db_session,
            JournalEntryCreate(
                entry_date=date(2026, 8, 16),
                source_type="sales.goods_shipped",
                source_id=uuid.uuid4(),
                lines=[
                    JournalLineCreate(
                        account_id=uuid.UUID(acc1_id),
                        currency_code="TWD",
                        txn_debit=Decimal("500"),
                        txn_credit=Decimal("0"),
                        debit=Decimal("500"),
                        credit=Decimal("0"),
                    ),
                    JournalLineCreate(
                        account_id=uuid.UUID(acc2_id),
                        currency_code="TWD",
                        txn_debit=Decimal("0"),
                        txn_credit=Decimal("500"),
                        debit=Decimal("0"),
                        credit=Decimal("500"),
                    ),
                ],
                custom_data={},
            ),
        )
        assert system_entry.source_type == "sales.goods_shipped"
        assert system_entry.custom_data == {}


def manage_admin_actor(headers: dict[str, str]) -> Any:
    from app.core.authorization import ActorContext
    from tests.conftest import ALL_ADMIN_PERMISSION_CODES

    return ActorContext(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        permission_codes=ALL_ADMIN_PERMISSION_CODES,
        is_system_actor=True,
    )


@pytest.mark.asyncio
async def test_sales_order_and_receivables_custom_fields_e2e(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # Define custom field on sales_order
    await _define_field(
        client,
        company_id,
        entity_type="sales_order",
        field_key="sales_channel",
        label="Sales Channel",
        field_type="ENUM",
        is_required=True,
        default_value="DIRECT",
        validation_rules={"choices": ["DIRECT", "PARTNER", "WEB"]},
    )

    # Define custom field on invoice
    await _define_field(
        client,
        company_id,
        entity_type="invoice",
        field_key="buyer_po",
        label="Buyer PO Reference",
        field_type="TEXT",
        is_required=True,
        default_value="PO-PENDING",
    )

    # Define custom field on payment
    await _define_field(
        client,
        company_id,
        entity_type="payment",
        field_key="remittance_bank",
        label="Remittance Bank",
        field_type="TEXT",
        is_required=False,
    )

    customer_id = await create_customer(client, company_id, "SO-CUST", credit_limit="10000")
    product_id = await create_product(client, company_id, "SO-PROD", list_price="500")

    # Open period for today so shipping & invoice posting succeed
    today = date.today()
    await client.post(
        "/api/v1/periods",
        json={"year": today.year, "month": today.month},
        headers=company_headers(company_id),
    )

    # Standard chart of accounts for O2C posting
    for acct_code, acct_name, acct_type in (
        ("1000", "Cash", "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("1300", "Inventory", "asset"),
        ("4000", "Revenue", "revenue"),
        ("5000", "COGS", "expense"),
    ):
        await create_account(client, company_id, acct_code, acct_name, acct_type)

    # Intake inventory for the product
    await create_adjustment(client, company_id, product_id, "10", "initial stock")

    # 1. Create draft order -> default sales_channel applied
    order = await create_draft_order(
        client,
        company_id,
        customer_id,
        [order_line(product_id, "2", unit_price="500")],
    )
    assert order["custom_data"]["sales_channel"] == "DIRECT"

    # 2. Update draft order with new custom field value
    res_order_patch = await client.patch(
        f"/api/v1/sales-orders/{order['id']}",
        json={"custom_data": {"sales_channel": "WEB"}},
        headers=company_headers(company_id),
    )
    assert res_order_patch.status_code == 200, res_order_patch.text
    assert res_order_patch.json()["custom_data"]["sales_channel"] == "WEB"

    # Confirm and Ship order
    await confirm_order(client, company_id, order["id"])
    res_ship = await client.post(
        f"/api/v1/sales-orders/{order['id']}/ship",
        headers=company_headers(company_id),
    )
    assert res_ship.status_code == 200, res_ship.text

    # 3. Create invoice -> default buyer_po applied
    res_inv = await client.post(
        "/api/v1/receivables/invoices",
        json={"order_id": order["id"]},
        headers=company_headers(company_id),
    )
    assert res_inv.status_code == 201, res_inv.text
    inv = res_inv.json()
    assert inv["custom_data"]["buyer_po"] == "PO-PENDING"

    # 4. Create payment with custom_data
    res_pay = await client.post(
        "/api/v1/receivables/payments",
        json={
            "customer_id": str(customer_id),
            "external_ref": f"REF_{uuid.uuid4().hex[:8]}",
            "amount": "1000.00",
            "custom_data": {"remittance_bank": "First Commercial Bank"},
        },
        headers=company_headers(company_id),
    )
    assert res_pay.status_code == 201, res_pay.text
    pay = res_pay.json()
    assert pay["custom_data"]["remittance_bank"] == "First Commercial Bank"


@pytest.mark.asyncio
async def test_custom_fields_cross_company_isolation(client: AsyncClient) -> None:
    code_a = f"CMPA_{uuid.uuid4().hex[:6].upper()}"
    code_b = f"CMPB_{uuid.uuid4().hex[:6].upper()}"
    cid_a = await create_company(client, code_a)
    cid_b = await create_company(client, code_b)

    # Define STRICT REQUIRED custom field ONLY on Company A
    await _define_field(
        client,
        cid_a,
        entity_type="customer",
        field_key="gov_vat_id",
        label="Government VAT ID",
        field_type="TEXT",
        is_required=True,
        default_value=None,  # strictly required
    )

    # Company B creates customer with custom_data={} -> SUCCEEDS (isolated!)
    res_b = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-B",
            "name": "Customer B",
            "currency_code": "TWD",
            "custom_data": {},
        },
        headers=company_headers(cid_b),
    )
    assert res_b.status_code == 201, res_b.text
    assert res_b.json()["custom_data"] == {}

    # Company A creates customer with custom_data={} -> 422 FAILS (enforces definition!)
    res_a_fail = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-A",
            "name": "Customer A",
            "currency_code": "TWD",
            "custom_data": {},
        },
        headers=company_headers(cid_a),
    )
    assert res_a_fail.status_code == 422
    assert "Custom field 'gov_vat_id' is required" in res_a_fail.json()["detail"]
