"""tests/cli/test_manage_integrations.py — CLI tests for manage_integrations & run_outbox_worker.

ADR-013 / Week 13 Slice C.
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.platform.permissions._helpers import create_company

from app.cli import manage_integrations, run_outbox_worker


@pytest.mark.asyncio
async def test_manage_integrations_webhooks_cli(
    db_session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await create_company(db_session, "CLI_INT_CO")

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        # 1. webhooks add
        rc = await manage_integrations._amain(
            [
                "webhooks",
                "add",
                "--company-code",
                "CLI_INT_CO",
                "--name",
                "CLI Hook",
                "--url",
                "https://api.clihook.com/callback",
                "--events",
                "sales.order_confirmed",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Endpoint created successfully:" in captured
        assert "Signing Secret (SAVE NOW):" in captured

        # 2. webhooks list
        rc_list = await manage_integrations._amain(
            ["webhooks", "list", "--company-code", "CLI_INT_CO"]
        )
        assert rc_list == 0
        captured_list = capsys.readouterr().out
        assert "CLI Hook" in captured_list
        assert "https://api.clihook.com/callback" in captured_list

        # 3. webhooks delete (soft deactivation)
        ep_id_line = next(line for line in captured_list.splitlines() if "ID:" in line)
        ep_id_str = ep_id_line.split("|")[0].replace("ID:", "").strip()
        rc_del = await manage_integrations._amain(
            ["webhooks", "delete", "--company-code", "CLI_INT_CO", "--endpoint-id", ep_id_str]
        )
        assert rc_del == 0
        assert f"Deactivated endpoint: {ep_id_str}" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_manage_integrations_batch_cli(
    db_session: AsyncSession,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await create_company(db_session, "CLI_BATCH_CO")

    # 1. Prepare CSV import file
    csv_file = tmp_path / "customers.csv"
    csv_file.write_text(
        "code,name,credit_limit,currency_code,payment_terms_days\n"
        "CLI_C1,CLI Customer 1,5000.00,TWD,30\n",
        encoding="utf-8",
    )

    rc_imp = await manage_integrations._amain(
        [
            "batch",
            "import",
            "--company-code",
            "CLI_BATCH_CO",
            "--entity",
            "customer",
            "--file",
            str(csv_file),
        ]
    )
    assert rc_imp == 0
    cap_imp = capsys.readouterr().out
    assert "Status: COMPLETED" in cap_imp
    assert "Succeeded: 1" in cap_imp

    # 2. Export to file
    out_file = tmp_path / "exported_customers.csv"
    rc_exp = await manage_integrations._amain(
        [
            "batch",
            "export",
            "--company-code",
            "CLI_BATCH_CO",
            "--entity",
            "customer",
            "--output",
            str(out_file),
        ]
    )
    assert rc_exp == 0
    cap_exp = capsys.readouterr().out
    assert "Status: COMPLETED" in cap_exp
    assert out_file.exists()
    assert "CLI_C1" in out_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_outbox_worker_cli_once(
    db_session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = await run_outbox_worker._amain(["--once"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Worker completed:" in captured
