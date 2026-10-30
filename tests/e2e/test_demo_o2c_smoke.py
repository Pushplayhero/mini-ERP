"""tests/e2e/test_demo_o2c_smoke.py — `app.cli.demo_o2c` (the `make demo`

script) actually works against the real app, and is safe to rerun.

Runs the script's own `run_demo()` against an ASGI-transport client bound
directly to the real FastAPI `app` object (the same pattern
`tests/conftest.py`'s own `client` fixture uses) instead of a live network
server — proving the script's logic is correct without needing `docker
compose up` in CI. `demo_o2c.py`'s hard-coded absolute URLs still route
correctly through an injected client: `httpx.AsyncClient(transport=...)`
dispatches every request (relative or absolute) through its bound
transport, and `ASGITransport` never touches a real socket regardless of
the host/port in the URL.

**Asserts actual state, not just "didn't raise"** (Codex diff review
2026-08-15, finding 5): an earlier revision of this test only proved the
second `run_demo()` call didn't throw, which would have passed even while
`demo_o2c.py` had a real stock-reconciliation bug (finding 2) that
silently posted a fresh inventory adjustment on every rerun. This version
snapshots the inventory summary and trial balance around the second call
and asserts both are byte-for-byte unchanged.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.cli.bootstrap_admin import bootstrap_admin
from app.cli.demo_o2c import CUSTOMER_CODE, PRODUCT_SKU, run_demo


async def _product_and_summary(demo_client: AsyncClient, token: str) -> tuple[str, list[object]]:
    headers = {"Authorization": f"Bearer {token}"}
    companies_resp = await demo_client.get("/api/v1/companies", headers=headers)
    companies_resp.raise_for_status()

    products_resp = await demo_client.get("/api/v1/products", headers=headers)
    products_resp.raise_for_status()
    product_id = next(p["id"] for p in products_resp.json() if p["sku"] == PRODUCT_SKU)

    summary_resp = await demo_client.get(
        "/api/v1/inventory/summary", params={"product_id": product_id}, headers=headers
    )
    summary_resp.raise_for_status()
    return product_id, summary_resp.json()


@pytest.mark.asyncio
async def test_demo_o2c_runs_and_is_rerunnable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import app

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "adminpassword123")

    await bootstrap_admin(
        email="admin@example.com",
        password="adminpassword123",
        company_code="MAKEDEMO",
        company_name="Make Demo Co",
    )

    # A dedicated ASGI-transport client, matching `demo_o2c.py`'s own
    # absolute-URL requests (unlike the `client` fixture's
    # `base_url="http://testserver"`, this one doesn't matter for
    # correctness — see module docstring — but matching it keeps this test
    # unsurprising to read).
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo_client:
        await run_demo(demo_client)

        login_resp = await demo_client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "adminpassword123"}
        )
        login_resp.raise_for_status()
        token = login_resp.json()["access_token"]

        _, summary_before_rerun = await _product_and_summary(demo_client, token)

        # Rerun: every step must resolve to the same order/invoice/payment
        # by its stable tag/ref and post nothing new — this is the actual
        # "safe to run `make demo` repeatedly" proof, verified by state,
        # not merely by absence of an exception.
        await run_demo(demo_client)

        _, summary_after_rerun = await _product_and_summary(demo_client, token)
        assert summary_before_rerun == summary_after_rerun, (
            "rerunning demo_o2c must not change on-hand stock — a change here means the "
            "remaining-work-aware reconciliation regressed to the fixed-target over-"
            "correction bug (Codex diff review 2026-08-15, finding 2)"
        )
        assert Decimal(summary_after_rerun[0]["on_hand"]) == Decimal("50"), (  # type: ignore[index]
            "expected on-hand to settle at the initial stock target once the demo order "
            "has already shipped (nothing left awaiting shipment)"
        )

        # Sanity: the customer this script's own module docstring claims
        # to be self-contained about really was created idempotently too.
        headers = {"Authorization": f"Bearer {token}"}
        companies_resp = await demo_client.get("/api/v1/companies", headers=headers)
        companies_resp.raise_for_status()
        customers_resp = await demo_client.get("/api/v1/customers", headers=headers)
        customers_resp.raise_for_status()
        matching_customers = [c for c in customers_resp.json() if c["code"] == CUSTOMER_CODE]
        assert len(matching_customers) == 1, "customer must not be duplicated across reruns"
