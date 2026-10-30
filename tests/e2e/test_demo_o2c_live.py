"""tests/e2e/test_demo_o2c_live.py — Live / Subprocess verification for
bootstrap_admin and demo_o2c.

Runs a real Uvicorn process in a separate child process and executes
`app.cli.demo_o2c` against the live TCP port to verify:
1. `bootstrap_admin` creates the initial administrator user and company.
2. `demo_o2c` logs in via HTTP, traverses the full O2C flow, and produces a balanced trial balance.
3. Rerunning `demo_o2c` against the live server is strictly idempotent.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cli.bootstrap_admin import bootstrap_admin
from app.cli.demo_o2c import PRODUCT_SKU

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_EMAIL = "admin@example.com"
TEST_PASSWORD = "change-this-password"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(
    base_url: str,
    server_process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = 15.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with httpx.AsyncClient(base_url=base_url, timeout=1.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            if server_process.poll() is not None:
                raise RuntimeError("Uvicorn server exited prematurely")
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f"Server at {base_url} did not become healthy within {timeout_seconds}s")


def _run_demo(base_url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DEMO_BASE_URL"] = base_url
    environment["BOOTSTRAP_ADMIN_EMAIL"] = TEST_EMAIL
    environment["BOOTSTRAP_ADMIN_PASSWORD"] = TEST_PASSWORD

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.demo_o2c",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


async def _snapshot(base_url: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        login = await client.post(
            "/auth/login",
            json={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD,
            },
        )
        login.raise_for_status()
        token = str(login.json()["access_token"])
        headers = {"Authorization": f"Bearer {token}"}

        trial_balance_response = await client.get(
            "/api/v1/reports/trial-balance",
            headers=headers,
        )
        trial_balance_response.raise_for_status()

        products_response = await client.get(
            "/api/v1/products",
            headers=headers,
        )
        products_response.raise_for_status()

        product_id = next(
            product["id"] for product in products_response.json() if product["sku"] == PRODUCT_SKU
        )

        stock_response = await client.get(
            "/api/v1/inventory/summary",
            params={"product_id": product_id},
            headers=headers,
        )
        stock_response.raise_for_status()

        return (
            trial_balance_response.json(),
            stock_response.json(),
        )


@pytest.mark.asyncio
async def test_demo_o2c_live_subprocess_lifecycle_and_idempotence(
    db_engine: AsyncEngine,
) -> None:
    await bootstrap_admin(
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
        company_code="LIVEDEMO",
        company_name="Live Demo Enterprise",
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    server_environment = os.environ.copy()
    server_environment["APP_ENVIRONMENT"] = "development"

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=server_environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        await _wait_for_health(base_url, server)

        first_run = _run_demo(base_url)
        assert first_run.returncode == 0, (
            f"demo_o2c failed:\nstdout:\n{first_run.stdout}\n" f"stderr:\n{first_run.stderr}"
        )
        assert "Trial balance:" in first_run.stdout

        trial_balance_first, stock_first = await _snapshot(base_url)

        total_debit = sum(Decimal(str(row["total_debit"])) for row in trial_balance_first)
        total_credit = sum(Decimal(str(row["total_credit"])) for row in trial_balance_first)
        assert total_debit == total_credit

        second_run = _run_demo(base_url)
        assert second_run.returncode == 0, (
            f"second demo_o2c failed:\nstdout:\n{second_run.stdout}\n"
            f"stderr:\n{second_run.stderr}"
        )

        trial_balance_second, stock_second = await _snapshot(base_url)

        assert trial_balance_first == trial_balance_second
        assert stock_first == stock_second

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


@pytest.mark.asyncio
async def test_readme_try_it_walkthrough_and_slice_e_live(
    db_engine: AsyncEngine,
) -> None:
    """WEEK9 Slice F: Live-server re-run of the README 'Try it' O2C walkthrough
    plus Slice E CLI provisioning and HTTP 405 enforcement.
    """
    await bootstrap_admin(
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
        company_code="MAIN",
        company_name="Main Company",
    )

    # Verify Slice E CLI company creation + reference data CLI work against the live DB
    cli_company_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.manage_companies",
            "--code",
            "SUB-LIVE",
            "--name",
            "Live Subsidiary",
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli_company_res.returncode == 0, f"manage_companies failed: {cli_company_res.stderr}"
    assert "code=SUB-LIVE" in cli_company_res.stdout
    sub_live_id = next(
        part.split("=", 1)[1] for part in cli_company_res.stdout.split() if part.startswith("id=")
    )

    box_code = f"BX{uuid.uuid4().hex[:6].upper()}"
    cli_ref_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.manage_reference_data",
            "create-uom",
            "--code",
            box_code,
            "--name",
            "Live Box",
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli_ref_res.returncode == 0, f"manage_reference_data failed: {cli_ref_res.stderr}"
    assert f"code={box_code}" in cli_ref_res.stdout

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_environment = os.environ.copy()
    server_environment["APP_ENVIRONMENT"] = "development"

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=server_environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        await _wait_for_health(base_url, server)

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            # 1. Login to MAIN company (TEST_EMAIL is entitled only to MAIN, not SUB-LIVE)
            login_resp = await client.post(
                "/auth/login",
                json={
                    "email": TEST_EMAIL,
                    "password": TEST_PASSWORD,
                },
            )
            login_resp.raise_for_status()
            token = str(login_resp.json()["access_token"])
            headers = {"Authorization": f"Bearer {token}"}

            # Decision 12: GET /api/v1/companies returns only entitled companies
            companies_resp = await client.get("/api/v1/companies", headers=headers)
            assert companies_resp.status_code == 200
            assert [c["code"] for c in companies_resp.json()] == ["MAIN"]

            unentitled_resp = await client.get(f"/api/v1/companies/{sub_live_id}", headers=headers)
            assert unentitled_resp.status_code == 404

            # 2. Verify Slice E removed POST endpoints return 405 on the live server
            for removed_path in (
                "/api/v1/companies",
                "/api/v1/currencies",
                "/api/v1/uom",
                "/api/v1/uom-conversions",
                "/api/v1/exchange-rates",
            ):
                r405 = await client.post(removed_path, json={}, headers=headers)
                assert (
                    r405.status_code == 405
                ), f"Expected 405 for {removed_path}, got {r405.status_code}"

            # 3. Walk the exact README 'Try it' O2C flow
            uom_resp = await client.get("/api/v1/uom", headers=headers)
            uom_resp.raise_for_status()
            ea_id = next(u["id"] for u in uom_resp.json() if u["code"] == "EA")

            local_today = date.today()
            utc_today = datetime.now(UTC).date()
            for year, month in {
                (local_today.year, local_today.month),
                (utc_today.year, utc_today.month),
            }:
                period_resp = await client.post(
                    "/api/v1/periods",
                    json={"year": year, "month": month},
                    headers=headers,
                )
                assert period_resp.status_code == 201

            for code, name, acct_type in (
                ("1000", "Cash", "asset"),
                ("1100", "Accounts Receivable", "asset"),
                ("1300", "Inventory", "asset"),
                ("4000", "Revenue", "revenue"),
                ("5000", "COGS", "expense"),
            ):
                acct_resp = await client.post(
                    "/api/v1/accounts",
                    json={"code": code, "name": name, "type": acct_type},
                    headers=headers,
                )
                assert acct_resp.status_code == 201

            cust_resp = await client.post(
                "/api/v1/customers",
                json={
                    "code": "CUST-001",
                    "name": "Test Customer",
                    "currency_code": "TWD",
                    "credit_limit": "50000",
                },
                headers=headers,
            )
            assert cust_resp.status_code == 201
            customer_id = cust_resp.json()["id"]

            prod_resp = await client.post(
                "/api/v1/products",
                json={
                    "sku": "WIDGET-1",
                    "name": "Widget",
                    "uom_id": ea_id,
                    "list_price": "100",
                    "standard_cost": "30",
                },
                headers=headers,
            )
            assert prod_resp.status_code == 201
            product_id = prod_resp.json()["id"]

            adj_resp = await client.post(
                "/api/v1/inventory/adjustments",
                json={"product_id": product_id, "qty_delta": "10", "reason": "initial stock"},
                headers=headers,
            )
            assert adj_resp.status_code == 201

            order_resp = await client.post(
                "/api/v1/sales-orders",
                json={
                    "customer_id": customer_id,
                    "lines": [{"product_id": product_id, "qty": "2", "unit_price": "100"}],
                },
                headers=headers,
            )
            assert order_resp.status_code == 201
            order_id = order_resp.json()["id"]

            confirm_resp = await client.post(
                f"/api/v1/sales-orders/{order_id}/confirm", headers=headers
            )
            assert confirm_resp.status_code == 200

            ship_resp = await client.post(f"/api/v1/sales-orders/{order_id}/ship", headers=headers)
            assert ship_resp.status_code == 200
            assert ship_resp.json()["status"] == "shipped"
            assert ship_resp.json()["total"] == "200.000000"

            inv_resp = await client.post(
                "/api/v1/receivables/invoices",
                json={"order_id": order_id},
                headers=headers,
            )
            assert inv_resp.status_code == 201
            invoice_id = inv_resp.json()["id"]

            pay_resp = await client.post(
                "/api/v1/receivables/payments",
                json={
                    "customer_id": customer_id,
                    "external_ref": "PAY-001",
                    "amount": "200",
                },
                headers=headers,
            )
            assert pay_resp.status_code == 201
            payment_id = pay_resp.json()["id"]

            alloc_resp = await client.post(
                f"/api/v1/receivables/payments/{payment_id}/allocations",
                json={
                    "request_ref": "ALLOC-001",
                    "allocations": [{"invoice_id": invoice_id, "amount": "200"}],
                },
                headers=headers,
            )
            assert alloc_resp.status_code == 200

            tb_resp = await client.get("/api/v1/reports/trial-balance", headers=headers)
            assert tb_resp.status_code == 200
            tb = tb_resp.json()
            by_code = {row["account_code"]: (row["total_debit"], row["total_credit"]) for row in tb}
            assert by_code == {
                "1000": ("200.000000", "0.000000"),
                "1100": ("200.000000", "200.000000"),
                "1300": ("0.000000", "60.000000"),
                "4000": ("0.000000", "200.000000"),
                "5000": ("60.000000", "0.000000"),
            }
            total_debit = sum(Decimal(str(row["total_debit"])) for row in tb)
            total_credit = sum(Decimal(str(row["total_credit"])) for row in tb)
            assert total_debit == Decimal("460.000000")
            assert total_credit == Decimal("460.000000")

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
