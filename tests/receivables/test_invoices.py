"""tests/receivables/test_invoices.py — invoice issue/void (ADR-008 Decision 1/4).

Happy path (AR/Revenue posted, trial balance moves), issue preconditions
(non-shipped 422, invoice_date-before-shipment 422), the double-invoice
race (`uq_invoices_order_live` -> 409), void -> contra entry -> re-issue,
void-while-settled rejection, and the two control-account protections
(R5/R11: manual entry to 1100 rejected, manual reversal of a
control-account entry rejected) that make the aging tie-out property
actually true.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import company_context
from app.modules.ledger import service as ledger_service
from tests.ledger._helpers import create_account
from tests.receivables._helpers import (
    get_trial_balance_by_code,
    issue_invoice,
    setup_shipped_order,
    void_invoice,
)


@pytest.mark.asyncio
async def test_issue_invoice_posts_ar_and_revenue_and_moves_trial_balance(
    client: AsyncClient,
) -> None:
    ctx = await setup_shipped_order(client, "RCV1", list_price="100", qty="2")
    company_id = ctx.company_id
    order = ctx.order

    resp = await issue_invoice(client, company_id, order["id"])
    assert resp.status_code == 201, resp.text
    invoice = resp.json()
    assert invoice["status"] == "open"
    assert invoice["total"] == "200.000000"
    assert invoice["settled_amount"] == "0.000000"
    assert invoice["order_id"] == order["id"]

    tb = await get_trial_balance_by_code(client, company_id)
    assert tb["1100"]["total_debit"] == "200.000000"
    assert tb["4000"]["total_credit"] == "200.000000"


@pytest.mark.asyncio
async def test_issue_invoice_from_non_shipped_order_is_422(client: AsyncClient) -> None:
    from tests.sales._helpers import create_draft_order, create_product, order_line

    ctx = await setup_shipped_order(client, "RCV2X", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    product_id = await create_product(client, company_id, "RCV2X-SKU2", list_price="50")
    draft = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="50")]
    )
    resp = await issue_invoice(client, company_id, draft["id"])
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_issue_invoice_backdated_before_shipment_is_422(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "RCV3")
    company_id = ctx.company_id
    order = ctx.order

    too_early = (date.today() - timedelta(days=365)).isoformat()
    resp = await issue_invoice(client, company_id, order["id"], invoice_date=too_early)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_double_invoice_race_second_call_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "RCV4")
    company_id = ctx.company_id
    order = ctx.order

    first = await issue_invoice(client, company_id, order["id"])
    assert first.status_code == 201, first.text
    second = await issue_invoice(client, company_id, order["id"])
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_void_invoice_posts_contra_entry_and_permits_reissue(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ctx = await setup_shipped_order(client, "RCV5", list_price="100", qty="1")
    company_id = ctx.company_id
    order = ctx.order

    issued = await issue_invoice(client, company_id, order["id"])
    assert issued.status_code == 201, issued.text
    invoice = issued.json()

    voided = await void_invoice(client, company_id, invoice["id"])
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "voided"

    tb = await get_trial_balance_by_code(client, company_id)
    # Issue posted Dr 1100 / Cr 4000 (100); void posted Dr 4000 / Cr 1100
    # (100) — both sides now show equal debit and credit, net zero.
    assert tb["1100"]["total_debit"] == tb["1100"]["total_credit"] == "100.000000"
    assert tb["4000"]["total_debit"] == tb["4000"]["total_credit"] == "100.000000"

    reissued = await issue_invoice(client, company_id, order["id"])
    assert reissued.status_code == 201, reissued.text
    assert reissued.json()["id"] != invoice["id"]


@pytest.mark.asyncio
async def test_void_invoice_with_nonzero_settled_amount_is_409(client: AsyncClient) -> None:
    from tests.receivables._helpers import allocation, create_payment

    ctx = await setup_shipped_order(client, "RCV6", list_price="100", qty="1")
    company_id = ctx.company_id
    customer_id = ctx.customer_id
    order = ctx.order

    issued = await issue_invoice(client, company_id, order["id"])
    invoice = issued.json()

    pay_resp = await create_payment(
        client,
        company_id,
        customer_id,
        "100",
        "RCV6-REF",
        allocations=[allocation(invoice["id"], "100")],
    )
    assert pay_resp.status_code == 201, pay_resp.text

    voided = await void_invoice(client, company_id, invoice["id"])
    assert voided.status_code == 409, voided.text


@pytest.mark.asyncio
async def test_void_already_voided_invoice_is_409(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "RCV7")
    company_id = ctx.company_id
    order = ctx.order
    invoice = (await issue_invoice(client, company_id, order["id"])).json()

    first = await void_invoice(client, company_id, invoice["id"])
    assert first.status_code == 200, first.text
    second = await void_invoice(client, company_id, invoice["id"])
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# Control-account protection (ADR-008 R5/R11) — what makes the aging tie-out
# property in test_aging.py actually true, not aspirational.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_journal_entry_to_control_account_is_rejected(client: AsyncClient) -> None:
    ctx = await setup_shipped_order(client, "RCV8", list_price="10", qty="1")
    company_id = ctx.company_id

    ar_account_id = None
    accounts_resp = await client.get("/api/v1/accounts", headers={"X-Company-Id": str(company_id)})
    for acct in accounts_resp.json():
        if acct["code"] == "1100":
            ar_account_id = acct["id"]
    cash_account_id = await create_account(client, company_id, "1000B", "Cash 2")

    resp = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": date.today().isoformat(),
            "lines": [
                {
                    "account_id": ar_account_id,
                    "currency_code": "TWD",
                    "debit": "10",
                    "credit": "0",
                    "txn_debit": "10",
                    "txn_credit": "0",
                    "exchange_rate": "1",
                },
                {
                    "account_id": str(cash_account_id),
                    "currency_code": "TWD",
                    "debit": "0",
                    "credit": "10",
                    "txn_debit": "0",
                    "txn_credit": "10",
                    "exchange_rate": "1",
                },
            ],
        },
        headers={"X-Company-Id": str(company_id)},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_public_journal_entry_api_cannot_set_source_fields(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A client-supplied `source_type`/`source_id` in the request body is

    silently ignored (extra field), never applied — the entry always ends
    up `source_type IS NULL` regardless of what the client sends (ADR-008
    R11).
    """
    ctx = await setup_shipped_order(client, "RCV9", list_price="10", qty="1")
    company_id = ctx.company_id
    debit_account_id = await create_account(client, company_id, "9000", "Misc Debit", "expense")
    credit_account_id = await create_account(client, company_id, "9001", "Misc Credit", "asset")

    resp = await client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": date.today().isoformat(),
            "source_type": "receivables.invoice_issued",
            "source_id": str(uuid.uuid4()),
            "lines": [
                {
                    "account_id": str(debit_account_id),
                    "currency_code": "TWD",
                    "debit": "5",
                    "credit": "0",
                    "txn_debit": "5",
                    "txn_credit": "0",
                    "exchange_rate": "1",
                },
                {
                    "account_id": str(credit_account_id),
                    "currency_code": "TWD",
                    "debit": "0",
                    "credit": "5",
                    "txn_debit": "0",
                    "txn_credit": "5",
                    "exchange_rate": "1",
                },
            ],
        },
        headers={"X-Company-Id": str(company_id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_type"] is None
    assert resp.json()["source_id"] is None


@pytest.mark.asyncio
async def test_manual_reversal_of_control_account_entry_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ctx = await setup_shipped_order(client, "RCV10", list_price="10", qty="1")
    company_id = ctx.company_id
    order = ctx.order

    issued = await issue_invoice(client, company_id, order["id"])
    assert issued.status_code == 201, issued.text

    with company_context(company_id):
        entries = await ledger_service.list_journal_entries(db_session)
    matching = [e for e in entries if e.source_type == "receivables.invoice_issued"]
    assert len(matching) == 1
    entry_id = matching[0].id

    resp = await client.post(
        f"/api/v1/journal-entries/{entry_id}/reverse",
        headers={"X-Company-Id": str(company_id)},
    )
    assert resp.status_code == 422, resp.text
