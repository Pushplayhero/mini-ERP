"""platform.customfields FastAPI routes — ADR-011 / Week 11 Slice B.

Endpoints mounted under `/api/v1/custom-fields`:
- `GET /api/v1/custom-fields`: List custom field definitions for the active tenant.
- `POST /api/v1/custom-fields`: Create a new custom field definition.
- `GET /api/v1/custom-fields/{field_id}`: Retrieve a single definition by ID.
- `PATCH /api/v1/custom-fields/{field_id}`: Update mutable attributes of a definition.
- `DELETE /api/v1/custom-fields/{field_id}`: Soft deactivate a definition.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.customfields import CustomFieldEntityType
from app.core.db import get_db_session
from app.platform.customfields import service
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionRead,
    CustomFieldDefinitionUpdate,
)

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "",
    response_model=list[CustomFieldDefinitionRead],
    dependencies=[Depends(RequiresPermission("platform.customfield.read"))],
)
async def list_custom_fields_endpoint(
    session: DbSession,
    entity_type: Annotated[
        CustomFieldEntityType | None, Query(description="Filter by business entity target")
    ] = None,
    active_only: Annotated[bool, Query(description="Filter only active definitions")] = False,
) -> list[CustomFieldDefinitionRead]:
    """List custom field definitions for the active tenant."""
    return await service.list_custom_field_definitions(
        session, entity_type=entity_type, active_only=active_only
    )


@router.post(
    "",
    response_model=CustomFieldDefinitionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("platform.customfield.manage"))],
)
async def create_custom_field_endpoint(
    payload: CustomFieldDefinitionCreate,
    session: DbSession,
) -> CustomFieldDefinitionRead:
    """Create a new custom field definition for the active tenant."""
    return await service.create_custom_field_definition(session, payload)


@router.get(
    "/{field_id}",
    response_model=CustomFieldDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.customfield.read"))],
)
async def get_custom_field_endpoint(
    field_id: uuid.UUID,
    session: DbSession,
) -> CustomFieldDefinitionRead:
    """Retrieve details for a specific custom field definition."""
    return await service.get_custom_field_definition(session, field_id)


@router.patch(
    "/{field_id}",
    response_model=CustomFieldDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.customfield.manage"))],
)
async def update_custom_field_endpoint(
    field_id: uuid.UUID,
    payload: CustomFieldDefinitionUpdate,
    session: DbSession,
) -> CustomFieldDefinitionRead:
    """Update mutable attributes of an existing custom field definition."""
    return await service.update_custom_field_definition(session, field_id, payload)


@router.delete(
    "/{field_id}",
    response_model=CustomFieldDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.customfield.manage"))],
)
async def deactivate_custom_field_endpoint(
    field_id: uuid.UUID,
    session: DbSession,
) -> CustomFieldDefinitionRead:
    """Soft deactivate a custom field definition for the active tenant."""
    return await service.deactivate_custom_field_definition(session, field_id)
