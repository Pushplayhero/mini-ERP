"""tests/cli/test_manage_custom_fields.py — Coverage for manage_custom_fields CLI (ADR-011).

Covers:
1. `_resolve_company_id`:
   - via company_code and company_id
   - matching vs mismatched
   - error cases (missing, nonexistent, whitespace)
2. `define_custom_field`, `list_custom_fields`, `get_custom_field`:
   - entity filtering and active_only filtering
   - default value and validation rules
3. `update_custom_field`:
   - label, rules, default_value, clear_default_value
4. `deactivate_custom_field`:
   - soft deactivation
5. CLI `_main` entry point:
   - standard exit codes (0 for success, 1 for domain/conflict/not found errors)
   - subcommands: list, get, define, update, deactivate
6. `CUSTOM_FIELD_ADMIN_ACTOR` least privilege enforcement:
   - attempts to call non-customfield services fail closed
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.sales._helpers import create_company

from app.cli import manage_custom_fields
from app.core.authorization import actor_context
from app.core.customfields import CustomFieldEntityType, CustomFieldType
from app.core.exceptions import (
    ActionDeniedError,
    DomainValidationError,
    NotFoundError,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import CompanyCreate
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
)


@pytest.mark.asyncio
async def test_resolve_company_id(client: AsyncClient, db_session: AsyncSession) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Resolve by code
    resolved_id = await manage_custom_fields._resolve_company_id(db_session, company_code=code)
    assert resolved_id == company_id

    # 2. Resolve by id
    resolved_by_id = await manage_custom_fields._resolve_company_id(
        db_session, company_id=company_id
    )
    assert resolved_by_id == company_id

    # 3. Resolve by matching code and id
    both_id = await manage_custom_fields._resolve_company_id(
        db_session, company_code=code, company_id=company_id
    )
    assert both_id == company_id

    # 4. Neither provided -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Either company_code or company_id"):
        await manage_custom_fields._resolve_company_id(db_session)

    # 5. Non-existent code -> NotFoundError
    with pytest.raises(NotFoundError, match="Company"):
        await manage_custom_fields._resolve_company_id(db_session, company_code="NONEXISTENT_XYZ")

    # 6. Mismatched code and id -> DomainValidationError
    with pytest.raises(DomainValidationError, match="does not match provided code"):
        await manage_custom_fields._resolve_company_id(
            db_session, company_code="MISMATCH_CODE", company_id=company_id
        )

    # 7. Empty/whitespace company_code -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Company code must not be empty"):
        await manage_custom_fields._resolve_company_id(db_session, company_code="   ")


@pytest.mark.asyncio
async def test_manage_custom_fields_crud_functions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Define customer field
    cf1 = await manage_custom_fields.define_custom_field(
        CustomFieldDefinitionCreate(
            entity_type=CustomFieldEntityType.CUSTOMER,
            field_key="tax_branch",
            label="Tax Branch Code",
            field_type=CustomFieldType.TEXT,
            is_required=True,
            default_value="HQ",
            validation_rules={"min_length": 2, "max_length": 10},
        ),
        company_code=code,
        session=db_session,
    )
    assert cf1.field_key == "tax_branch"
    assert cf1.label == "Tax Branch Code"
    assert cf1.is_required is True
    assert cf1.default_value == "HQ"

    # 2. Define product field
    cf2 = await manage_custom_fields.define_custom_field(
        CustomFieldDefinitionCreate(
            entity_type=CustomFieldEntityType.PRODUCT,
            field_key="shelf_life_days",
            label="Shelf Life (Days)",
            field_type=CustomFieldType.INTEGER,
            is_required=False,
            default_value=30,
            validation_rules={"min_value": 1, "max_value": 3650},
        ),
        company_id=company_id,
        session=db_session,
    )
    assert cf2.field_key == "shelf_life_days"

    # 3. List fields with filters
    all_fields = await manage_custom_fields.list_custom_fields(
        company_code=code, session=db_session
    )
    assert len(all_fields) == 2

    customer_fields = await manage_custom_fields.list_custom_fields(
        company_id=company_id, entity_type="customer", session=db_session
    )
    assert len(customer_fields) == 1
    assert customer_fields[0].field_key == "tax_branch"

    product_fields = await manage_custom_fields.list_custom_fields(
        company_code=code, entity_type=CustomFieldEntityType.PRODUCT, session=db_session
    )
    assert len(product_fields) == 1
    assert product_fields[0].field_key == "shelf_life_days"

    # 4. Get single field
    got = await manage_custom_fields.get_custom_field(cf1.id, company_code=code, session=db_session)
    assert got.id == cf1.id
    assert got.field_key == "tax_branch"

    # 5. Update field: update label and clear default value
    updated = await manage_custom_fields.update_custom_field(
        cf1.id,
        CustomFieldDefinitionUpdate(
            label="Tax Branch Office",
            is_required=False,
            default_value=None,
        ),
        company_code=code,
        session=db_session,
    )
    assert updated.label == "Tax Branch Office"
    assert updated.is_required is False
    assert updated.default_value is None

    # 6. Deactivate field
    deactivated = await manage_custom_fields.deactivate_custom_field(
        cf1.id, company_code=code, session=db_session
    )
    assert deactivated.is_active is False

    # List active_only
    active_fields = await manage_custom_fields.list_custom_fields(
        company_code=code, active_only=True, session=db_session
    )
    assert len(active_fields) == 1
    assert active_fields[0].field_key == "shelf_life_days"

    # Invalid entity_type -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Unsupported custom field entity_type"):
        await manage_custom_fields.list_custom_fields(
            company_code=code, entity_type="invalid_entity_xyz", session=db_session
        )


@pytest.mark.asyncio
async def test_manage_custom_fields_main_cli(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. define
    rc = await manage_custom_fields._main(
        [
            "define",
            "--company-code",
            code,
            "--entity-type",
            "customer",
            "--key",
            "rating",
            "--label",
            "Customer Rating",
            "--type",
            "ENUM",
            "--rules",
            '{"choices": ["VIP", "STANDARD", "LEAD"]}',
            "--default-value",
            '"STANDARD"',
        ],
        dispose=False,
    )
    assert rc == 0

    # 2. list
    rc = await manage_custom_fields._main(
        ["list", "--company-code", code, "--entity-type", "customer"],
        dispose=False,
    )
    assert rc == 0

    # Retrieve defined field ID via list helper
    fields = await manage_custom_fields.list_custom_fields(company_code=code)
    rating_field = next(f for f in fields if f.field_key == "rating")

    # 3. get
    rc = await manage_custom_fields._main(
        ["get", "--company-code", code, "--field-id", str(rating_field.id)],
        dispose=False,
    )
    assert rc == 0

    # 4. update
    rc = await manage_custom_fields._main(
        [
            "update",
            "--company-code",
            code,
            "--field-id",
            str(rating_field.id),
            "--label",
            "Client Tier",
            "--required",
        ],
        dispose=False,
    )
    assert rc == 0

    # 5. deactivate
    rc = await manage_custom_fields._main(
        ["deactivate", "--company-id", str(company_id), "--field-id", str(rating_field.id)],
        dispose=False,
    )
    assert rc == 0

    # 6. Error cases: invalid json
    rc_bad_json = await manage_custom_fields._main(
        [
            "define",
            "--company-code",
            code,
            "--entity-type",
            "customer",
            "--key",
            "bad_field",
            "--label",
            "Bad Field",
            "--type",
            "TEXT",
            "--rules",
            "not-valid-json",
        ],
        dispose=False,
    )
    assert rc_bad_json != 0

    # 7. Error cases: nonexistent company
    rc_no_co = await manage_custom_fields._main(
        ["list", "--company-code", "NONEXISTENT_CO_XYZ"],
        dispose=False,
    )
    assert rc_no_co != 0

    # 8. Error cases: empty --rules string and argparse SystemExit
    rc_empty_rules = await manage_custom_fields._main(
        [
            "define",
            "--company-code",
            code,
            "--entity-type",
            "customer",
            "--key",
            "bad_empty_rules",
            "--label",
            "Bad Empty Rules",
            "--type",
            "TEXT",
            "--rules",
            "",
        ],
        dispose=False,
    )
    assert rc_empty_rules != 0

    rc_argparse_err = await manage_custom_fields._main(
        ["unknown-subcommand"],
        dispose=False,
    )
    assert rc_argparse_err != 0


@pytest.mark.asyncio
async def test_custom_field_admin_actor_least_privilege(db_session: AsyncSession) -> None:
    """CUSTOM_FIELD_ADMIN_ACTOR is strictly bounded to platform.customfield permissions."""
    with (
        actor_context(manage_custom_fields.CUSTOM_FIELD_ADMIN_ACTOR),
        pytest.raises(ActionDeniedError, match="lacks permission 'masterdata.company.create'"),
    ):
        await masterdata_service.create_company(
            db_session,
            CompanyCreate(code="UNAUTH", name="Unauth Co", functional_currency_code="TWD"),
        )
