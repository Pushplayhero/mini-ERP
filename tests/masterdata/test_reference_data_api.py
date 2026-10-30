"""Coverage for global reference-data endpoints (UoM / currencies) and CLI management.

ADR-009 Decision 11 (WEEK9 Slice E):
- All write operations for global reference data (UoM, UoM conversions, currencies,
  exchange rates) are moved to CLI tool `app.cli.manage_reference_data` under
  `REFERENCE_DATA_ADMIN_ACTOR`.
- HTTP POST endpoints are removed (405 Method Not Allowed).
- `GET /api/v1/uom` and `GET /api/v1/currencies` remain Public (no auth required).
- `GET /api/v1/uom-conversions` and `GET /api/v1/exchange-rates` require authentication
  and dedicated read permissions (`masterdata.uom_conversion.read`,
  `masterdata.exchange_rate.read`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import manage_reference_data
from app.core.authorization import actor_context
from app.core.exceptions import ActionDeniedError, DomainValidationError
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import CompanyCreate
from tests._auth_helpers import setup_company_with_admin
from tests.platform.permissions._helpers import (
    create_role_without_permissions,
    create_user_with_role,
)


@pytest.mark.asyncio
async def test_list_seeded_currencies_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/currencies")
    assert response.status_code == 200
    codes = {c["code"] for c in response.json()}
    assert {"TWD", "USD"}.issubset(codes)


@pytest.mark.asyncio
async def test_list_seeded_uom_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/uom")
    assert response.status_code == 200
    codes = {u["code"] for u in response.json()}
    assert {"EA", "KG"}.issubset(codes)


@pytest.mark.asyncio
async def test_reference_data_http_posts_are_removed_405(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="REFPOST")
    removed_endpoints = [
        ("/api/v1/uom", {"code": "BOX", "name": "Box"}),
        (
            "/api/v1/uom-conversions",
            {
                "from_uom_id": str(uuid.uuid4()),
                "to_uom_id": str(uuid.uuid4()),
                "factor": "1.000000",
            },
        ),
        ("/api/v1/currencies", {"code": "EUR", "name": "Euro"}),
        (
            "/api/v1/exchange-rates",
            {
                "from_currency_code": "USD",
                "to_currency_code": "TWD",
                "rate": "31.5000000000",
                "rate_date": "2026-08-13",
            },
        ),
    ]

    for path, payload in removed_endpoints:
        resp = await client.post(path, json=payload, headers=auth.headers)
        assert resp.status_code == 405, f"Expected 405 for POST {path}, got {resp.status_code}"


@pytest.mark.asyncio
async def test_manage_reference_data_cli_and_get_queries(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    auth = await setup_company_with_admin(client, code="REFCLI")

    # 1. Programmatic UoM creation
    uom_box = await manage_reference_data.create_uom(code="BOX", name="Box", session=db_session)
    assert uom_box.code == "BOX"

    # 2. CLI _main invocation for UoM
    code_pack = f"PK{uuid.uuid4().hex[:4].upper()}"
    rc = await manage_reference_data._main(
        ["create-uom", "--code", code_pack, "--name", "Pack"], dispose=False
    )
    assert rc == 0

    uom_list_resp = await client.get("/api/v1/uom")
    assert uom_list_resp.status_code == 200
    codes = {u["code"] for u in uom_list_resp.json()}
    assert {"BOX", code_pack}.issubset(codes)

    # 3. Currency creation
    eur_code = f"E{uuid.uuid4().hex[:2].upper()}"
    cur_eur = await manage_reference_data.create_currency(
        code=eur_code, name="Euro", decimal_places=2, session=db_session
    )
    assert cur_eur.code == eur_code

    jpy_code = f"J{uuid.uuid4().hex[:2].upper()}"
    rc = await manage_reference_data._main(
        ["create-currency", "--code", jpy_code, "--name", "Japanese Yen", "--decimal-places", "0"],
        dispose=False,
    )
    assert rc == 0

    cur_list_resp = await client.get("/api/v1/currencies")
    assert cur_list_resp.status_code == 200
    cur_codes = {c["code"] for c in cur_list_resp.json()}
    assert {eur_code, jpy_code}.issubset(cur_codes)

    # 4. UoM Conversion creation (programmatic + CLI _main with Banker's Rounding)
    uoms = (await client.get("/api/v1/uom")).json()
    ea_id = uuid.UUID(next(u["id"] for u in uoms if u["code"] == "EA"))
    kg_id = uuid.UUID(next(u["id"] for u in uoms if u["code"] == "KG"))
    box_id = uuid.UUID(next(u["id"] for u in uoms if u["code"] == "BOX"))

    conv = await manage_reference_data.create_uom_conversion(
        from_uom_id=kg_id,
        to_uom_id=ea_id,
        factor=Decimal("2.5000005"),  # Round-half-even -> 2.500000
        session=db_session,
    )
    assert conv.factor == Decimal("2.500000")

    rc = await manage_reference_data._main(
        [
            "create-uom-conversion",
            "--from-uom-id",
            str(box_id),
            "--to-uom-id",
            str(ea_id),
            "--factor",
            "12.0000015",  # Round-half-even -> 12.000002
        ],
        dispose=False,
    )
    assert rc == 0

    convs_resp = await client.get("/api/v1/uom-conversions", headers=auth.headers)
    assert convs_resp.status_code == 200
    assert any(
        c["from_uom_id"] == str(box_id)
        and c["to_uom_id"] == str(ea_id)
        and c["factor"] == "12.000002"
        for c in convs_resp.json()
    )

    # 5. Exchange rate creation via CLI _main
    rc = await manage_reference_data._main(
        [
            "create-exchange-rate",
            "--from-currency",
            "USD",
            "--to-currency",
            "TWD",
            "--rate",
            "31.5000000000",
            "--rate-date",
            "2026-08-13",
        ],
        dispose=False,
    )
    assert rc == 0

    # 6. Verify protected GET reads with admin auth
    rates_resp = await client.get("/api/v1/exchange-rates", headers=auth.headers)
    assert rates_resp.status_code == 200
    assert any(
        r["from_currency_code"] == "USD"
        and r["to_currency_code"] == "TWD"
        and r["rate"] == "31.5000000000"
        for r in rates_resp.json()
    )


@pytest.mark.asyncio
async def test_manage_reference_data_cli_rejects_invalid_inputs(
    db_session: AsyncSession,
) -> None:
    # 1. Invalid / non-finite / non-positive decimals fail cleanly in CLI parser (exit code != 0)
    for bad_rate in ("not-a-decimal", "NaN", "Infinity", "0", "-5.0"):
        rc = await manage_reference_data._main(
            [
                "create-exchange-rate",
                "--from-currency",
                "USD",
                "--to-currency",
                "TWD",
                "--rate",
                bad_rate,
                "--rate-date",
                "2026-08-14",
            ],
            dispose=False,
        )
        assert rc != 0

    # 2. Programmatic calls reject empty codes and non-positive factors
    with pytest.raises(DomainValidationError, match="UoM code must not be empty"):
        await manage_reference_data.create_uom(code="   ", name="Blank", session=db_session)

    with pytest.raises(DomainValidationError, match="must be finite and greater than zero"):
        await manage_reference_data.create_uom_conversion(
            from_uom_id=uuid.uuid4(),
            to_uom_id=uuid.uuid4(),
            factor=Decimal("NaN"),
            session=db_session,
        )


@pytest.mark.asyncio
async def test_protected_reference_data_reads_authorization(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    auth = await setup_company_with_admin(client, code="REFPROT")

    # 1. Unauthenticated requests fail closed with 401
    resp_conv_noauth = await client.get("/api/v1/uom-conversions")
    assert resp_conv_noauth.status_code == 401

    resp_rate_noauth = await client.get("/api/v1/exchange-rates")
    assert resp_rate_noauth.status_code == 401

    # 2. Authenticated user without read permission gets 403 ActionDeniedError
    role_code = "ref_unprivileged"
    await create_role_without_permissions(db_session, role_code)
    unprivileged_email = f"unprivileged_{uuid.uuid4().hex[:6]}@example.com"
    await create_user_with_role(
        db_session,
        email=unprivileged_email,
        password="password123",
        company_id=auth.company_id,
        role_code=role_code,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": unprivileged_email, "password": "password123"},
    )
    assert login_resp.status_code == 200
    unprivileged_headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    resp_conv_noperm = await client.get("/api/v1/uom-conversions", headers=unprivileged_headers)
    assert resp_conv_noperm.status_code == 403

    resp_rate_noperm = await client.get("/api/v1/exchange-rates", headers=unprivileged_headers)
    assert resp_rate_noperm.status_code == 403

    # 3. Authenticated user with permission gets 200
    resp_conv_ok = await client.get("/api/v1/uom-conversions", headers=auth.headers)
    assert resp_conv_ok.status_code == 200

    resp_rate_ok = await client.get("/api/v1/exchange-rates", headers=auth.headers)
    assert resp_rate_ok.status_code == 200


@pytest.mark.asyncio
async def test_reference_data_admin_actor_least_privilege(db_session: AsyncSession) -> None:
    """REFERENCE_DATA_ADMIN_ACTOR is strictly bounded and cannot create companies."""
    with (
        actor_context(manage_reference_data.REFERENCE_DATA_ADMIN_ACTOR),
        pytest.raises(ActionDeniedError, match="lacks permission 'masterdata.company.create'"),
    ):
        await masterdata_service.create_company(
            db_session,
            CompanyCreate(code="ILLEGAL", name="Illegal Co", functional_currency_code="TWD"),
        )
