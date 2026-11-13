"""tests/platform/test_integration_batch.py — Batch import and export test suite.

ADR-013 / Week 13 Slice C.

Covers:
1. Formula injection neutralization:
   - Neutralizing strings starting with =, +, -, @, \\t, \\r
   - Preserving valid negative decimal amounts
2. Batch import for Customer, Product, and Account (CSV & JSON).
3. Dry-run execution verifying zero side effects on the database.
4. Validation failure rollback & error summary reporting.
5. Size and row count limit guards.
6. Batch export for registered entities with CSV (UTF-8 / BOM) and JSON.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context
from app.core.integration import (
    BatchDataFormat,
    BatchEntityType,
    BatchImportValidationError,
)
from app.core.tenancy import company_context
from app.modules.masterdata.models import Customer, Product, Uom
from app.platform.integration import batch
from app.platform.integration.batch import neutralize_formula
from tests.platform.permissions._helpers import create_company


def test_formula_injection_neutralization() -> None:
    # Dangerous formula prefixes are prepended with an apostrophe
    assert neutralize_formula("=SUM(A1:A10)") == "'=SUM(A1:A10)"
    assert neutralize_formula("+123456cmd") == "'+123456cmd"
    assert neutralize_formula("@HYPERLINK('http://evil.com')") == "'@HYPERLINK('http://evil.com')"
    assert neutralize_formula("\tTabInjection") == "'\tTabInjection"
    assert neutralize_formula("-cmd|' /C calc'!A0") == "'-cmd|' /C calc'!A0"

    # Valid numeric values are safe and NOT modified
    assert neutralize_formula("-10.50") == "-10.50"
    assert neutralize_formula("-100") == "-100"
    assert neutralize_formula(Decimal("-50.25")) == Decimal("-50.25")
    assert neutralize_formula("Standard Company Name") == "Standard Company Name"


@pytest.mark.asyncio
async def test_batch_import_customer_csv_and_dry_run(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "BATCH_CUST_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import", "platform.integration.read"}),
    )

    csv_data = (
        "code,name,credit_limit,currency_code,payment_terms_days\n"
        "CUST01,Alpha Corp,50000.00,TWD,30\n"
        "CUST02,Beta LLC,25000.00,TWD,60\n"
    )

    with company_context(co_id), actor_context(actor):
        # 1. Dry run
        dry_job, dry_errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            csv_data,
            data_format=BatchDataFormat.CSV,
            dry_run=True,
        )
        assert dry_job.status == "DRY_RUN"
        assert dry_job.dry_run is True
        assert dry_job.total_rows == 2
        assert dry_job.failed_rows == 0
        assert not dry_errors

        # Check DB: no customers persisted
        custs = list(
            (await db_session.scalars(select(Customer).where(Customer.company_id == co_id))).all()
        )
        assert len(custs) == 0

        # 2. Live import
        job, errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            csv_data,
            data_format=BatchDataFormat.CSV,
            dry_run=False,
        )
        assert job.status == "COMPLETED"
        assert job.dry_run is False
        assert job.total_rows == 2
        assert job.succeeded_rows == 2
        assert job.failed_rows == 0
        assert not errors

        # Check DB: customers persisted
        persisted = list(
            (
                await db_session.scalars(
                    select(Customer).where(Customer.company_id == co_id).order_by(Customer.code)
                )
            ).all()
        )
        assert len(persisted) == 2
        assert persisted[0].code == "CUST01"
        assert persisted[0].credit_limit == Decimal("50000.000000")
        assert persisted[1].code == "CUST02"


@pytest.mark.asyncio
async def test_batch_import_customer_validation_failure(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "BATCH_ERR_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import"}),
    )

    # Row 2 has unsupported currency USD in Phase 1
    csv_data = (
        "code,name,credit_limit,currency_code\n"
        "CUST10,Valid Customer,1000.00,TWD\n"
        "CUST11,Bad Customer,2000.00,USD\n"
    )

    with company_context(co_id), actor_context(actor):
        job, errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            csv_data,
            data_format=BatchDataFormat.CSV,
            dry_run=False,
        )
        assert job.status == "FAILED"
        assert job.failed_rows == 1
        assert len(errors) == 1
        assert errors[0]["row"] == 2

        # Rollback check: CUST10 must NOT be in DB (all-or-nothing atomicity)
        custs = list(
            (await db_session.scalars(select(Customer).where(Customer.company_id == co_id))).all()
        )
        assert len(custs) == 0


@pytest.mark.asyncio
async def test_batch_import_product_with_uom_code(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "BATCH_PROD_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import"}),
    )

    # Create reference UoM
    uom = Uom(code="PCS", name="Pieces")
    db_session.add(uom)
    await db_session.commit()

    csv_data = (
        "sku,name,uom_code,list_price,standard_cost\n"
        "SKU-001,Widget A,PCS,100.00,60.00\n"
        "SKU-002,Widget B,PCS,250.00,150.00\n"
    )

    with company_context(co_id), actor_context(actor):
        job, errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.PRODUCT,
            csv_data,
            data_format=BatchDataFormat.CSV,
        )
        assert job.status == "COMPLETED"
        assert job.succeeded_rows == 2

        prods = list(
            (
                await db_session.scalars(
                    select(Product).where(Product.company_id == co_id).order_by(Product.sku)
                )
            ).all()
        )
        assert len(prods) == 2
        assert prods[0].sku == "SKU-001"
        assert prods[0].uom_id == uom.id
        assert prods[0].list_price == Decimal("100.000000")


@pytest.mark.asyncio
async def test_batch_export_customer_csv_and_json(
    db_session: AsyncSession,
) -> None:
    co_id = await create_company(db_session, "BATCH_EXP_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.read"}),
    )

    c1 = Customer(
        company_id=co_id,
        code="EXP01",
        name="=HYPERLINK() Dangerous Customer",
        credit_limit=Decimal("12000.000000"),
        currency_code="TWD",
        payment_terms_days=30,
    )
    db_session.add(c1)
    await db_session.commit()

    with company_context(co_id), actor_context(actor):
        # 1. Export CSV with BOM
        csv_out, job_csv = await batch.export_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            data_format=BatchDataFormat.CSV,
            include_bom=True,
        )
        assert job_csv.status == "COMPLETED"
        assert job_csv.total_rows == 1
        assert csv_out.startswith("\ufeff")
        # Formula neutralized in CSV output:
        assert "'=HYPERLINK() Dangerous Customer" in csv_out

        # 2. Export JSON
        json_out, job_json = await batch.export_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            data_format=BatchDataFormat.JSON,
        )
        assert job_json.status == "COMPLETED"
        assert '"code": "EXP01"' in json_out


@pytest.mark.asyncio
async def test_batch_import_size_limit_rejection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    co_id = await create_company(db_session, "BATCH_LIMIT_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import"}),
    )

    from app.core import settings

    orig_settings = settings.get_settings()
    monkeypatch.setattr(orig_settings, "batch_import_max_bytes", 50)

    large_content = "code,name\n" + ("X" * 100)
    with (
        company_context(co_id),
        actor_context(actor),
        pytest.raises(BatchImportValidationError, match="exceeds max limit"),
    ):
        await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            large_content,
        )


def test_formula_injection_neutralization_hardened() -> None:
    """Verify invisible chars, Unicode spaces, delimiters, and full-width/non-finite numbers."""
    bypasses = [
        "\u200b=1+1",
        "\ufeff=1+1",
        "\u00a0=SUM(1+1)",
        "\u3000=1+1",
        "\u2000=1+1",
        "\u202f=1+1",
        "\u205f=1+1",
        "\x85=1+1",
        "\u2028=1+1",
        "\u200e=1+1",
        '"\u3000=1+1',
        "+\uff11\uff12\uff13",
        "-\uff11\uff12\uff13",
        "|calc",
        ";=1+1",
        "+Infinity",
        "-Infinity",
        "+NaN",
        "+1_000",
        "+1e5",
        "-1e5",
    ]
    for sample in bypasses:
        neutralized = neutralize_formula(sample)
        assert neutralized.startswith("'"), f"Failed to neutralize {sample!r} -> {neutralized!r}"

    assert neutralize_formula("+100") == "+100"
    assert neutralize_formula("-10.50") == "-10.50"
    assert neutralize_formula("0.00") == "0.00"
    assert neutralize_formula("台北\u3000有限公司") == "台北\u3000有限公司"


@pytest.mark.asyncio
async def test_batch_import_preserves_literal_text_and_validates_custom_data(
    db_session: AsyncSession,
) -> None:
    from app.core import customfields as cf_port
    from app.platform.customfields.models import CustomFieldDefinition
    from app.platform.customfields.service import validate_and_normalize_custom_data_unchecked

    co_id = await create_company(db_session, "BATCH_CF_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import", "platform.integration.read"}),
    )

    cf_def = CustomFieldDefinition(
        company_id=co_id,
        entity_type="customer",
        field_key="vip_tier",
        label="VIP Tier",
        field_type="ENUM",
        is_required=True,
        is_active=True,
        validation_rules={"choices": ["GOLD", "SILVER"]},
    )
    db_session.add(cf_def)
    await db_session.commit()

    orig_validator = cf_port.get_custom_data_validator()
    cf_port.set_custom_data_validator(validate_and_normalize_custom_data_unchecked)
    try:
        with company_context(co_id), actor_context(actor):
            # 1. Missing required custom field must fail validation cleanly
            bad_csv = "code,name,currency_code\nC_FAIL,@Corp + Partner,TWD\n"
            bad_job, bad_errors = await batch.import_batch_data(
                db_session,
                BatchEntityType.CUSTOMER,
                bad_csv,
                data_format=BatchDataFormat.CSV,
            )
            assert bad_job.status == "FAILED"
            assert bad_job.failed_rows == 1
            assert "vip_tier" in bad_errors[0]["error"]

            # 2. Valid custom_data.vip_tier succeeds and keeps '@Corp + Partner' literal (no `'`)
            good_csv = (
                "code,name,currency_code,custom_data.vip_tier\nC_OK,@Corp + Partner,TWD,GOLD\n"
            )
            good_job, good_errors = await batch.import_batch_data(
                db_session,
                BatchEntityType.CUSTOMER,
                good_csv,
                data_format=BatchDataFormat.CSV,
            )
            assert good_job.status == "COMPLETED"
            assert good_errors == []

            cust = await db_session.scalar(
                select(Customer).where(
                    Customer.company_id == co_id,
                    Customer.code == "C_OK",
                )
            )
            assert cust is not None
            assert cust.name == "@Corp + Partner"
            assert cust.custom_data == {"vip_tier": "GOLD"}
    finally:
        if orig_validator is not None:
            cf_port.set_custom_data_validator(orig_validator)


@pytest.mark.asyncio
async def test_batch_import_savepoint_isolation_malformed_rows_and_rbac(
    db_session: AsyncSession,
) -> None:
    from app.core.exceptions import ActionDeniedError

    co_id = await create_company(db_session, "BATCH_ERR_CO")
    unpriv_actor = ActorContext(user_id=uuid.uuid4(), permission_codes=frozenset())
    with company_context(co_id), actor_context(unpriv_actor):
        with pytest.raises(ActionDeniedError):
            await batch.import_batch_data(
                db_session,
                BatchEntityType.CUSTOMER,
                "code,name\nC1,Co\n",
            )
        with pytest.raises(ActionDeniedError):
            await batch.export_batch_data(db_session, BatchEntityType.CUSTOMER)

    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.import", "platform.integration.read"}),
    )
    with company_context(co_id), actor_context(actor):
        # 1. Duplicate code fails row 2 in its own savepoint without breaking row 3;
        # because any row failed, whole batch rolls back inserted rows cleanly.
        dup_csv = (
            "code,name,currency_code\n"
            "C_DUP,First,TWD\n"
            "C_DUP,Duplicate,TWD\n"
            "C_VALID,Third Valid,TWD\n"
        )
        dup_job, dup_errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            dup_csv,
            data_format=BatchDataFormat.CSV,
        )
        assert dup_job.status == "FAILED"
        assert dup_job.total_rows == 3
        assert dup_job.succeeded_rows == 0
        assert dup_job.failed_rows == 1
        assert len(dup_errors) == 1
        assert dup_errors[0]["row"] == 2
        assert (
            list(
                (
                    await db_session.scalars(select(Customer).where(Customer.company_id == co_id))
                ).all()
            )
            == []
        )

        # 2. Extra CSV column (`None` key) reports structured row error instead of crashing
        extra_col_csv = "code,name,currency_code\nC_X,Alpha,TWD,EXTRA_VAL\n"
        extra_job, extra_errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            extra_col_csv,
            data_format=BatchDataFormat.CSV,
        )
        assert extra_job.status == "FAILED"
        assert "extra columns" in extra_errors[0]["error"]

        # 3. Non-dict JSON array item reports structured row error while row 2 still validates
        non_dict_job, non_dict_errors = await batch.import_batch_data(
            db_session,
            BatchEntityType.CUSTOMER,
            '[123, {"code": "C_OK_JSON", "name": "Valid"}]',
            data_format=BatchDataFormat.JSON,
        )
        assert non_dict_job.status == "FAILED"
        assert non_dict_job.total_rows == 2
        assert non_dict_job.succeeded_rows == 0
        assert non_dict_job.failed_rows == 1
        assert len(non_dict_errors) == 1
        assert non_dict_errors[0]["error"] == "Row must be an object"

        # 4. Oversized CSV field (> 131,072 bytes) raises BatchImportValidationError cleanly
        oversized_field_csv = "code,name\nC_HUGE," + ("X" * 150_000) + "\n"
        with pytest.raises(BatchImportValidationError, match="Malformed CSV"):
            await batch.import_batch_data(
                db_session,
                BatchEntityType.CUSTOMER,
                oversized_field_csv,
                data_format=BatchDataFormat.CSV,
            )


@pytest.mark.asyncio
async def test_batch_export_uses_company_context_and_omits_bom_by_default(
    db_session: AsyncSession,
) -> None:
    from typing import Any

    from app.core import integration as int_port
    from app.core.tenancy import require_current_company_id

    co_id = await create_company(db_session, "BATCH_CTX_CO")
    actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.integration.read"}),
    )

    observed_company_id: list[uuid.UUID] = []

    async def _provider(
        session: AsyncSession,
        company_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        _ = session
        observed_company_id.append(require_current_company_id())
        assert company_id == co_id
        return [{"order_no": "SO-100", "note": "=CMD()"}]

    orig_provider = int_port.get_export_provider(BatchEntityType.SALES_ORDER)
    int_port.register_export_provider(BatchEntityType.SALES_ORDER, _provider)
    try:
        with company_context(co_id), actor_context(actor):
            csv_out, job = await batch.export_batch_data(
                db_session,
                BatchEntityType.SALES_ORDER,
                data_format=BatchDataFormat.CSV,
            )
            assert job.status == "COMPLETED"
            assert observed_company_id == [co_id]
            assert not csv_out.startswith("\ufeff")
            assert "'=CMD()" in csv_out
    finally:
        if orig_provider is not None:
            int_port.register_export_provider(BatchEntityType.SALES_ORDER, orig_provider)
        else:
            int_port._export_providers.pop(BatchEntityType.SALES_ORDER.value, None)
