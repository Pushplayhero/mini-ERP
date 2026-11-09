"""platform.customfields service layer — ADR-011 / Week 11 Slice B.

Implements:
1. Definition CRUD with company root serialization (`lock_company_unchecked`).
2. Runtime validation engine (`validate_and_normalize_custom_data_unchecked`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import require_current_actor, require_permission
from app.core.customfields import CustomFieldEntityType, CustomFieldValidationError
from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import require_current_company_id
from app.modules.masterdata import service as masterdata_service
from app.platform.customfields.models import CustomFieldDefinition
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionRead,
    CustomFieldDefinitionUpdate,
    validate_and_normalize_field_value,
    validate_and_normalize_validation_rules,
    validate_default_value,
)


def _normalize_entity_type(entity_type: CustomFieldEntityType | str) -> str:
    try:
        return CustomFieldEntityType(entity_type).value
    except ValueError as exc:
        raise CustomFieldValidationError(
            f"Unsupported custom field entity_type: {entity_type!r}"
        ) from exc


async def list_custom_field_definitions(
    session: AsyncSession,
    *,
    entity_type: CustomFieldEntityType | str | None = None,
    active_only: bool = False,
) -> list[CustomFieldDefinitionRead]:
    """List custom field definitions for the active tenant."""
    require_permission("platform.customfield.read")
    require_current_company_id()

    stmt = select(CustomFieldDefinition)
    if entity_type is not None:
        normalized_entity = _normalize_entity_type(entity_type)
        stmt = stmt.where(CustomFieldDefinition.entity_type == normalized_entity)
    if active_only:
        stmt = stmt.where(CustomFieldDefinition.is_active.is_(True))

    stmt = stmt.order_by(CustomFieldDefinition.entity_type, CustomFieldDefinition.field_key)
    res = await session.execute(stmt)
    rows = res.scalars().all()
    return [CustomFieldDefinitionRead.model_validate(r) for r in rows]


async def get_custom_field_definition(
    session: AsyncSession,
    field_id: uuid.UUID,
) -> CustomFieldDefinitionRead:
    """Retrieve a single custom field definition by ID for the active tenant."""
    require_permission("platform.customfield.read")
    require_current_company_id()

    stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.id == field_id)
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("CustomFieldDefinition", str(field_id))
    return CustomFieldDefinitionRead.model_validate(row)


async def create_custom_field_definition(
    session: AsyncSession,
    data: CustomFieldDefinitionCreate,
) -> CustomFieldDefinitionRead:
    """Create a new custom field definition for the active tenant.

    Serialized by locking the tenant root company row to eliminate TOCTOU races.
    """
    require_permission("platform.customfield.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    # Lock tenant root row FOR UPDATE
    await masterdata_service.lock_company_unchecked(session, company_id)

    # Check for duplicate (company_id, entity_type, field_key)
    stmt = select(CustomFieldDefinition).where(
        CustomFieldDefinition.entity_type == data.entity_type.value,
        CustomFieldDefinition.field_key == data.field_key,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        raise ConflictError(
            f"Custom field {data.field_key!r} already defined for entity {data.entity_type.value!r}"
        )

    definition = CustomFieldDefinition(
        id=uuid.uuid4(),
        company_id=company_id,
        entity_type=data.entity_type.value,
        field_key=data.field_key,
        label=data.label,
        field_type=data.field_type.value,
        is_required=data.is_required,
        is_active=True,
        default_value=data.default_value,
        validation_rules=data.validation_rules,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    session.add(definition)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Conflict creating custom field {data.field_key!r} "
            f"for entity {data.entity_type.value!r}"
        ) from exc

    await session.refresh(definition)
    return CustomFieldDefinitionRead.model_validate(definition)


async def update_custom_field_definition(
    session: AsyncSession,
    field_id: uuid.UUID,
    data: CustomFieldDefinitionUpdate,
) -> CustomFieldDefinitionRead:
    """Update mutable attributes of a custom field definition for the active tenant."""
    require_permission("platform.customfield.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(session, company_id)

    stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.id == field_id)
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("CustomFieldDefinition", str(field_id))

    # Validate new rules and default_value BEFORE mutating the ORM row
    rules_to_use = row.validation_rules
    if "validation_rules" in data.model_fields_set and data.validation_rules is not None:
        rules_to_use = validate_and_normalize_validation_rules(
            row.field_type, data.validation_rules
        )

    new_default = row.default_value
    if "default_value" in data.model_fields_set:
        new_default = validate_default_value(
            row.field_key, row.field_type, data.default_value, rules_to_use
        )
    elif "validation_rules" in data.model_fields_set and row.default_value is not None:
        # Re-validate existing default_value with new rules
        new_default = validate_default_value(
            row.field_key, row.field_type, row.default_value, rules_to_use
        )

    if "label" in data.model_fields_set and data.label is not None:
        row.label = data.label

    if "is_required" in data.model_fields_set and data.is_required is not None:
        row.is_required = data.is_required

    if "is_active" in data.model_fields_set and data.is_active is not None:
        row.is_active = data.is_active

    if "validation_rules" in data.model_fields_set:
        row.validation_rules = rules_to_use

    if "default_value" in data.model_fields_set or (
        "validation_rules" in data.model_fields_set and row.default_value is not None
    ):
        row.default_value = new_default

    row.updated_by = actor.user_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Conflict updating custom field {str(field_id)!r}") from exc

    await session.refresh(row)
    return CustomFieldDefinitionRead.model_validate(row)


async def deactivate_custom_field_definition(
    session: AsyncSession,
    field_id: uuid.UUID,
) -> CustomFieldDefinitionRead:
    """Soft deactivate a custom field definition (is_active = False)."""
    require_permission("platform.customfield.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    await masterdata_service.lock_company_unchecked(session, company_id)

    stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.id == field_id)
    res = await session.execute(stmt)
    row = res.scalars().first()
    if row is None:
        raise NotFoundError("CustomFieldDefinition", str(field_id))

    row.is_active = False
    row.updated_by = actor.user_id

    await session.commit()
    await session.refresh(row)
    return CustomFieldDefinitionRead.model_validate(row)


# ---------------------------------------------------------------------------
# Runtime Custom Fields Validation Engine (implements CustomDataValidator)
# ---------------------------------------------------------------------------


async def validate_and_normalize_custom_data_unchecked(
    session: AsyncSession,
    entity_type: str,
    raw_custom_data: dict[str, Any],
    *,
    is_create: bool = False,
    existing_custom_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute runtime schema validation and Banker's Rounding normalization.

    1. Fail-closed: enforces active tenant context.
    2. Fetches all active CustomFieldDefinition rows for entity_type.
    3. Rejects unknown keys ('extra="forbid"').
    4. Enforces required fields, applies default values on creation, and normalizes types.
    """
    require_current_company_id()
    normalized_entity = _normalize_entity_type(entity_type)

    if not isinstance(raw_custom_data, dict):
        raise CustomFieldValidationError("custom_data must be a JSON object (dictionary)")

    stmt = select(CustomFieldDefinition).where(
        CustomFieldDefinition.entity_type == normalized_entity,
        CustomFieldDefinition.is_active.is_(True),
    )
    res = await session.execute(stmt)
    active_defs = res.scalars().all()
    defs_map = {d.field_key: d for d in active_defs}

    # 1. Fail-closed on unknown fields
    for key in raw_custom_data:
        if key not in defs_map:
            raise CustomFieldValidationError(
                f"Unknown custom field {key!r} for entity {normalized_entity!r}"
            )

    # 2. Base payload: copy of existing data if partial update, else empty
    if is_create or existing_custom_data is None:
        result: dict[str, Any] = {}
    else:
        result = dict(existing_custom_data)

    # 3. Process fields provided in raw_custom_data
    for key, val in raw_custom_data.items():
        definition = defs_map[key]
        if val is None:
            if definition.is_required:
                raise CustomFieldValidationError(
                    f"Custom field {key!r} is required and cannot be null"
                )
            result[key] = None
        else:
            normalized_val = validate_and_normalize_field_value(
                key, definition.field_type, val, definition.validation_rules
            )
            result[key] = normalized_val

    # 4. On creation, apply default values for omitted fields and verify required fields
    if is_create:
        for definition in active_defs:
            key = definition.field_key
            if key not in raw_custom_data:
                if definition.default_value is not None:
                    result[key] = definition.default_value
                elif definition.is_required:
                    raise CustomFieldValidationError(
                        f"Custom field {key!r} is required for entity {normalized_entity!r}"
                    )

    return result
