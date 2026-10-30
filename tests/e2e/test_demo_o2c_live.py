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
