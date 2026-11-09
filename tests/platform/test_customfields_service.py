"""tests/platform/test_customfields_service.py — Service layer tests for custom fields.

Covers:
1. Definition CRUD operations with tenant locking.
2. Permission guards ('platform.customfield.read', 'platform.customfield.manage').
3. Validation rules and default value normalization for all 6 data types.
4. Decimal Banker's Rounding and numeric bounds.
5. Runtime validation engine:
   - Fail-closed on unknown fields (extra="forbid").
   - Required fields and default value population on creation.
   - Partial updates and nullability checks on update.
6. Multi-tenant isolation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context
from app.core.customfields import (
    CustomFieldEntityType,
    CustomFieldType,
    CustomFieldValidationError,
)
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    NotFoundError,
    TenancyContextError,
)
from app.core.tenancy import company_context
from app.platform.customfields import service as cf_service
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
    validate_and_normalize_field_value,
    validate_and_normalize_validation_rules,
)
from tests.platform.permissions._helpers import create_company


@pytest.fixture
async def sample_company(db_session: AsyncSession) -> uuid.UUID:
    return await create_company(db_session, "CFCO1")


@pytest.fixture
async def other_company(db_session: AsyncSession) -> uuid.UUID:
    return await create_company(db_session, "CFCO2")


@pytest.fixture
def admin_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.customfield.read", "platform.customfield.manage"}),
    )


@pytest.fixture
def readonly_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.customfield.read"}),
    )


@pytest.fixture
def unprivileged_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset(),
    )


# ---------------------------------------------------------------------------
# Definition CRUD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_custom_field_definition_and_uniqueness(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    unprivileged_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        # 1. Unprivileged actor denied
        with (
            actor_context(unprivileged_actor),
            pytest.raises(
                ActionDeniedError,
                match="lacks permission 'platform.customfield.manage'",
            ),
        ):
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="vip_tier",
                    label="VIP Tier",
                    field_type=CustomFieldType.TEXT,
                ),
            )

        # 2. Admin creates successfully
        with actor_context(admin_actor):
            res = await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="vip_tier",
                    label="VIP Tier",
                    field_type=CustomFieldType.TEXT,
                    is_required=True,
                    default_value="standard",
                    validation_rules={"min_length": 3, "max_length": 20},
                ),
            )
            assert res.company_id == sample_company
            assert res.entity_type == "customer"
            assert res.field_key == "vip_tier"
            assert res.label == "VIP Tier"
            assert res.field_type == "TEXT"
            assert res.is_required is True
            assert res.is_active is True
            assert res.default_value == "standard"
            assert res.validation_rules == {"min_length": 3, "max_length": 20}

            # 3. Duplicate definition for same (company_id, entity_type, field_key)
            # fails with ConflictError
            with pytest.raises(ConflictError, match="already defined"):
                await cf_service.create_custom_field_definition(
                    db_session,
                    CustomFieldDefinitionCreate(
                        entity_type=CustomFieldEntityType.CUSTOMER,
                        field_key="vip_tier",
                        label="Another VIP Tier",
                        field_type=CustomFieldType.TEXT,
                    ),
                )


@pytest.mark.asyncio
async def test_list_and_get_custom_field_definitions(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    readonly_actor: ActorContext,
    unprivileged_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        with actor_context(admin_actor):
            f1 = await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="loyalty_score",
                    label="Loyalty Score",
                    field_type=CustomFieldType.INTEGER,
                ),
            )
            f2 = await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.SALES_ORDER,
                    field_key="expedited_flag",
                    label="Expedited",
                    field_type=CustomFieldType.BOOLEAN,
                ),
            )

        # Unprivileged denied
        with (
            actor_context(unprivileged_actor),
            pytest.raises(ActionDeniedError, match="lacks permission 'platform.customfield.read'"),
        ):
            await cf_service.list_custom_field_definitions(db_session)

        # Readonly actor can list and get
        with actor_context(readonly_actor):
            all_defs = await cf_service.list_custom_field_definitions(db_session)
            assert len(all_defs) == 2

            # Filter by entity_type
            cust_defs = await cf_service.list_custom_field_definitions(
                db_session, entity_type="customer"
            )
            assert len(cust_defs) == 1
            assert cust_defs[0].id == f1.id

            # Get single
            got = await cf_service.get_custom_field_definition(db_session, f2.id)
            assert got.id == f2.id
            assert got.field_key == "expedited_flag"

            # Unknown ID raises NotFoundError
            with pytest.raises(NotFoundError, match="CustomFieldDefinition"):
                await cf_service.get_custom_field_definition(db_session, uuid.uuid4())


@pytest.mark.asyncio
async def test_update_and_deactivate_custom_field_definition(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    readonly_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        with actor_context(admin_actor):
            created = await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.PRODUCT,
                    field_key="fragility_level",
                    label="Fragility",
                    field_type=CustomFieldType.ENUM,
                    validation_rules={"choices": ["LOW", "MEDIUM", "HIGH"]},
                    default_value="LOW",
                ),
            )

        # Readonly actor denied update / deactivate
        with (
            actor_context(readonly_actor),
            pytest.raises(
                ActionDeniedError,
                match="lacks permission 'platform.customfield.manage'",
            ),
        ):
            await cf_service.update_custom_field_definition(
                db_session, created.id, CustomFieldDefinitionUpdate(label="New Label")
            )

        with (
            actor_context(readonly_actor),
            pytest.raises(
                ActionDeniedError,
                match="lacks permission 'platform.customfield.manage'",
            ),
        ):
            await cf_service.deactivate_custom_field_definition(db_session, created.id)

        # Admin updates successfully
        with actor_context(admin_actor):
            updated = await cf_service.update_custom_field_definition(
                db_session,
                created.id,
                CustomFieldDefinitionUpdate(
                    label="Fragility Rating",
                    is_required=True,
                    validation_rules={"choices": ["LOW", "MEDIUM", "HIGH", "EXTREME"]},
                    default_value="MEDIUM",
                ),
            )
            assert updated.label == "Fragility Rating"
            assert updated.is_required is True
            assert updated.default_value == "MEDIUM"
            assert updated.validation_rules == {"choices": ["LOW", "MEDIUM", "HIGH", "EXTREME"]}

            # Updating validation_rules incompatible with existing default_value fails closed
            with pytest.raises(CustomFieldValidationError, match="not an allowed choice"):
                await cf_service.update_custom_field_definition(
                    db_session,
                    created.id,
                    CustomFieldDefinitionUpdate(validation_rules={"choices": ["LOW", "HIGH"]}),
                )

            # Explicitly clearing default_value to None succeeds
            cleared_default = await cf_service.update_custom_field_definition(
                db_session,
                created.id,
                CustomFieldDefinitionUpdate(default_value=None),
            )
            assert cleared_default.default_value is None

            # Deactivate
            deactivated = await cf_service.deactivate_custom_field_definition(
                db_session, created.id
            )
            assert deactivated.is_active is False

            # Active-only list excludes deactivated
            active_list = await cf_service.list_custom_field_definitions(
                db_session, active_only=True
            )
            assert not any(d.id == created.id for d in active_list)


# ---------------------------------------------------------------------------
# Validation Rules & Type Normalization Tests
# ---------------------------------------------------------------------------


def test_validation_rules_text_enforcement() -> None:
    # Valid rules
    r = validate_and_normalize_validation_rules(
        CustomFieldType.TEXT, {"min_length": 2, "max_length": 10, "regex": r"^[A-Z]+$"}
    )
    assert r == {"min_length": 2, "max_length": 10, "regex": r"^[A-Z]+$"}

    # Unknown rule key
    with pytest.raises(CustomFieldValidationError, match="Unknown validation rule"):
        validate_and_normalize_validation_rules(CustomFieldType.TEXT, {"unsupported_key": 123})

    # min_length > max_length
    with pytest.raises(
        CustomFieldValidationError,
        match="max_length cannot be less than min_length",
    ):
        validate_and_normalize_validation_rules(
            CustomFieldType.TEXT, {"min_length": 10, "max_length": 5}
        )

    # Invalid regex
    with pytest.raises(CustomFieldValidationError, match="Invalid regex pattern"):
        validate_and_normalize_validation_rules(CustomFieldType.TEXT, {"regex": "[unclosed"})

    # Trailing newline rejected by fullmatch on regex
    with pytest.raises(CustomFieldValidationError, match="does not match regex pattern"):
        validate_and_normalize_field_value("code", CustomFieldType.TEXT, "ABC\n", r)


def test_validation_rules_integer_enforcement() -> None:
    r = validate_and_normalize_validation_rules(
        CustomFieldType.INTEGER, {"min_value": 0, "max_value": 100}
    )
    assert r == {"min_value": 0, "max_value": 100}

    # Reject boolean as integer
    with pytest.raises(CustomFieldValidationError, match="min_value must be an integer"):
        validate_and_normalize_validation_rules(CustomFieldType.INTEGER, {"min_value": True})

    # min_value > max_value
    with pytest.raises(CustomFieldValidationError, match="max_value cannot be less than min_value"):
        validate_and_normalize_validation_rules(
            CustomFieldType.INTEGER, {"min_value": 50, "max_value": 20}
        )

    # 64-bit integer bounds enforcement
    with pytest.raises(CustomFieldValidationError, match="64-bit integer bounds"):
        validate_and_normalize_validation_rules(CustomFieldType.INTEGER, {"max_value": 2**63})
    with pytest.raises(CustomFieldValidationError, match="64-bit integer bounds"):
        validate_and_normalize_field_value("big_int", CustomFieldType.INTEGER, -(2**63) - 1, {})


def test_validation_rules_decimal_bankers_rounding_and_bounds() -> None:
    # Quantizes to 6dp string Banker's Rounded
    # 10.1234565 rounds to 10.123456 (even), 10.1234575 rounds to 10.123458 (even)
    r = validate_and_normalize_validation_rules(
        CustomFieldType.DECIMAL,
        {"min_value": "10.1234565", "max_value": Decimal("100.50")},
    )
    assert r == {"min_value": "10.123456", "max_value": "100.500000"}

    # Rejects float (prevents float rounding inaccuracy)
    with pytest.raises(CustomFieldValidationError, match="rejects float"):
        validate_and_normalize_validation_rules(CustomFieldType.DECIMAL, {"min_value": 10.5})

    # Rejects NaN or Infinity
    with pytest.raises(CustomFieldValidationError, match="cannot be NaN"):
        validate_and_normalize_validation_rules(CustomFieldType.DECIMAL, {"min_value": "NaN"})

    # Out of bounds [0, 10^14)
    with pytest.raises(CustomFieldValidationError, match="out of bounds"):
        validate_and_normalize_validation_rules(CustomFieldType.DECIMAL, {"min_value": "-1"})

    with pytest.raises(CustomFieldValidationError, match="out of bounds"):
        validate_and_normalize_validation_rules(
            CustomFieldType.DECIMAL, {"max_value": "100000000000000"}
        )


def test_validation_rules_boolean_enforcement() -> None:
    # Boolean fields accept empty rules
    assert validate_and_normalize_validation_rules(CustomFieldType.BOOLEAN, {}) == {}

    # Reject extra rules for boolean
    with pytest.raises(CustomFieldValidationError, match="do not accept validation rules"):
        validate_and_normalize_validation_rules(CustomFieldType.BOOLEAN, {"some_rule": True})


def test_validation_rules_date_enforcement() -> None:
    r = validate_and_normalize_validation_rules(
        CustomFieldType.DATE, {"min_date": "2026-01-01", "max_date": "2026-12-31"}
    )
    assert r == {"min_date": "2026-01-01", "max_date": "2026-12-31"}

    # Invalid ISO format
    with pytest.raises(CustomFieldValidationError, match="not a valid ISO date"):
        validate_and_normalize_validation_rules(CustomFieldType.DATE, {"min_date": "2026/01/01"})

    # min_date > max_date
    with pytest.raises(CustomFieldValidationError, match="cannot be earlier than min_date"):
        validate_and_normalize_validation_rules(
            CustomFieldType.DATE, {"min_date": "2026-12-31", "max_date": "2026-01-01"}
        )


def test_validation_rules_enum_enforcement() -> None:
    r = validate_and_normalize_validation_rules(CustomFieldType.ENUM, {"choices": ["A", "B", "C"]})
    assert r == {"choices": ["A", "B", "C"]}

    # Missing choices
    with pytest.raises(CustomFieldValidationError, match="requires non-empty list of choices"):
        validate_and_normalize_validation_rules(CustomFieldType.ENUM, {})

    # Duplicate choices
    with pytest.raises(CustomFieldValidationError, match="duplicate entries"):
        validate_and_normalize_validation_rules(CustomFieldType.ENUM, {"choices": ["A", "B", "A"]})


# ---------------------------------------------------------------------------
# Runtime Validation Engine Tests (validate_and_normalize_custom_data_unchecked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_validation_fail_closed_on_unknown_fields(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        with actor_context(admin_actor):
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="known_field",
                    label="Known",
                    field_type=CustomFieldType.TEXT,
                ),
            )

        # Fail closed on unknown field
        with pytest.raises(
            CustomFieldValidationError,
            match="Unknown custom field 'unregistered_key'",
        ):
            await cf_service.validate_and_normalize_custom_data_unchecked(
                db_session,
                "customer",
                {"unregistered_key": "some_value"},
                is_create=True,
            )


@pytest.mark.asyncio
async def test_runtime_validation_create_semantics(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        with actor_context(admin_actor):
            # 1. Required field with default
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.SALES_ORDER,
                    field_key="priority_level",
                    label="Priority",
                    field_type=CustomFieldType.INTEGER,
                    is_required=True,
                    default_value=1,
                    validation_rules={"min_value": 1, "max_value": 5},
                ),
            )
            # 2. Required field WITHOUT default
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.SALES_ORDER,
                    field_key="delivery_zone",
                    label="Zone",
                    field_type=CustomFieldType.TEXT,
                    is_required=True,
                ),
            )
            # 3. Optional decimal field with Banker's Rounding
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.SALES_ORDER,
                    field_key="special_discount",
                    label="Discount",
                    field_type=CustomFieldType.DECIMAL,
                    is_required=False,
                ),
            )

        # Missing required field without default raises error
        with pytest.raises(
            CustomFieldValidationError,
            match="Custom field 'delivery_zone' is required",
        ):
            await cf_service.validate_and_normalize_custom_data_unchecked(
                db_session,
                "sales_order",
                {"special_discount": "50.1234565"},
                is_create=True,
            )

        # Successful create: auto-populates default priority_level and rounds special_discount
        normalized = await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session,
            "sales_order",
            {"delivery_zone": "NORTH", "special_discount": "50.1234565"},
            is_create=True,
        )
        assert normalized["priority_level"] == 1
        assert normalized["delivery_zone"] == "NORTH"
        assert normalized["special_discount"] == "50.123456"  # Banker's Rounded to even


@pytest.mark.asyncio
async def test_runtime_validation_update_semantics(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    with company_context(sample_company):
        with actor_context(admin_actor):
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.INVOICE,
                    field_key="carrier_id",
                    label="Carrier ID",
                    field_type=CustomFieldType.TEXT,
                    is_required=True,
                ),
            )
            await cf_service.create_custom_field_definition(
                db_session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.INVOICE,
                    field_key="tracking_notes",
                    label="Notes",
                    field_type=CustomFieldType.TEXT,
                    is_required=False,
                ),
            )

        existing_data: dict[str, Any] = {"carrier_id": "CARRIER-A", "tracking_notes": "Note 1"}

        # Setting required field to null fails
        with pytest.raises(CustomFieldValidationError, match="required and cannot be null"):
            await cf_service.validate_and_normalize_custom_data_unchecked(
                db_session,
                "invoice",
                {"carrier_id": None},
                is_create=False,
                existing_custom_data=existing_data,
            )

        # Partial update: modify tracking_notes while carrier_id is preserved
        updated = await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session,
            "invoice",
            {"tracking_notes": "Updated Note"},
            is_create=False,
            existing_custom_data=existing_data,
        )
        assert updated == {"carrier_id": "CARRIER-A", "tracking_notes": "Updated Note"}

        # Setting optional field to null succeeds
        cleared = await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session,
            "invoice",
            {"tracking_notes": None},
            is_create=False,
            existing_custom_data=existing_data,
        )
        assert cleared == {"carrier_id": "CARRIER-A", "tracking_notes": None}


@pytest.mark.asyncio
async def test_runtime_validation_missing_tenancy_context_fails_closed(
    db_session: AsyncSession,
) -> None:
    # No tenancy context bound
    with pytest.raises(TenancyContextError):
        await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session,
            "customer",
            {"field": "val"},
            is_create=True,
        )


@pytest.mark.asyncio
async def test_multi_tenant_isolation(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    other_company: uuid.UUID,
    admin_actor: ActorContext,
) -> None:
    # Company A defines custom field 'tenant_a_field'
    with company_context(sample_company), actor_context(admin_actor):
        await cf_service.create_custom_field_definition(
            db_session,
            CustomFieldDefinitionCreate(
                entity_type=CustomFieldEntityType.CUSTOMER,
                field_key="tenant_a_field",
                label="Company A Field",
                field_type=CustomFieldType.BOOLEAN,
            ),
        )

    # Company B defines custom field 'tenant_b_field'
    with company_context(other_company), actor_context(admin_actor):
        await cf_service.create_custom_field_definition(
            db_session,
            CustomFieldDefinitionCreate(
                entity_type=CustomFieldEntityType.CUSTOMER,
                field_key="tenant_b_field",
                label="Company B Field",
                field_type=CustomFieldType.BOOLEAN,
            ),
        )

    # In Company A context:
    with company_context(sample_company):
        # tenant_a_field is valid
        res_a = await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session, "customer", {"tenant_a_field": True}, is_create=True
        )
        assert res_a == {"tenant_a_field": True}

        # tenant_b_field is rejected as unknown
        with pytest.raises(
            CustomFieldValidationError,
            match="Unknown custom field 'tenant_b_field'",
        ):
            await cf_service.validate_and_normalize_custom_data_unchecked(
                db_session, "customer", {"tenant_b_field": True}, is_create=True
            )

    # In Company B context:
    with company_context(other_company):
        # tenant_a_field is rejected as unknown
        with pytest.raises(
            CustomFieldValidationError,
            match="Unknown custom field 'tenant_a_field'",
        ):
            await cf_service.validate_and_normalize_custom_data_unchecked(
                db_session, "customer", {"tenant_a_field": True}, is_create=True
            )

        # tenant_b_field is valid
        res_b = await cf_service.validate_and_normalize_custom_data_unchecked(
            db_session, "customer", {"tenant_b_field": True}, is_create=True
        )
        assert res_b == {"tenant_b_field": True}
