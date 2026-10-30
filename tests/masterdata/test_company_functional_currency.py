"""Diff-review regression: `CompanyCreate.functional_currency_code` must be
restricted to Phase 1's only supported functional currency (ADR-005 R1).

WEEK9 Slice E (ADR-009 Decision 12):
Company creation is moved from HTTP `POST /companies` to CLI tool
`app.cli.manage_companies` under `COMPANY_ADMIN_ACTOR`. `POST /companies` is
removed (returns 405 Method Not Allowed).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import manage_companies
from app.core.authorization import actor_context
from app.core.exceptions import ActionDeniedError, DomainValidationError
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import UomCreate
from tests._auth_helpers import setup_company_with_admin


@pytest.mark.asyncio
async def test_post_companies_http_endpoint_is_removed_405(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="ANCHP")
    response = await client.post(
        "/api/v1/companies",
        json={"code": "USDCO", "name": "USD Co.", "functional_currency_code": "USD"},
        headers=auth.headers,
    )
    assert response.status_code == 405, response.text


@pytest.mark.asyncio
async def test_create_company_with_non_twd_functional_currency_is_rejected(
    db_session: AsyncSession,
) -> None:
    # 1. Programmatic creation raises validation error
    with pytest.raises(ValidationError, match="functional_currency_code must be 'TWD'"):
        await manage_companies.create_company(
            code="USDCO",
            name="USD Co.",
            functional_currency_code="USD",
            session=db_session,
        )

    # 2. CLI _main exits with non-zero status
    rc = await manage_companies._main(
        ["--code", "USDCLI", "--name", "USD CLI Co.", "--functional-currency-code", "USD"],
        dispose=False,
    )
    assert rc == 1

    # 3. Empty/whitespace company code is rejected
    with pytest.raises(DomainValidationError, match="Company code must not be empty"):
        await manage_companies.create_company(
            code="   ",
            name="Blank Co.",
            functional_currency_code="TWD",
            session=db_session,
        )


@pytest.mark.asyncio
async def test_create_company_with_twd_functional_currency_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    code = f"TWD_{uuid.uuid4().hex[:4].upper()}"
    company = await manage_companies.create_company(
        code=code,
        name="TWD Co.",
        functional_currency_code="TWD",
        session=db_session,
    )
    assert company.code == code
    assert company.functional_currency_code == "TWD"

    # Also verify CLI _main (with optional create-company subcommand & --functional-currency alias)
    cli_code = f"CLI_{uuid.uuid4().hex[:4].upper()}"
    rc = await manage_companies._main(
        [
            "create-company",
            "--code",
            cli_code,
            "--name",
            "CLI Co.",
            "--functional-currency",
            "TWD",
        ],
        dispose=False,
    )
    assert rc == 0


@pytest.mark.asyncio
async def test_company_admin_actor_least_privilege(db_session: AsyncSession) -> None:
    """COMPANY_ADMIN_ACTOR is strictly bounded and cannot create reference data (UoM)."""
    with (
        actor_context(manage_companies.COMPANY_ADMIN_ACTOR),
        pytest.raises(ActionDeniedError, match="lacks permission 'masterdata.uom.create'"),
    ):
        await masterdata_service.create_uom(
            db_session,
            UomCreate(code="ILL", name="Illegal UoM"),
        )
