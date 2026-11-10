"""platform.workflow FastAPI routes — ADR-012 / Week 12 Slice B.

Endpoints mounted under `/api/v1/workflows`:
- `GET /api/v1/workflows/definitions`: List workflow definitions.
- `POST /api/v1/workflows/definitions`: Create a new workflow definition.
- `GET /api/v1/workflows/definitions/{definition_id}`: Retrieve a definition by ID.
- `PATCH /api/v1/workflows/definitions/{definition_id}`: Update mutable attributes.
- `DELETE /api/v1/workflows/definitions/{definition_id}`: Soft deactivate a definition.
- `GET /api/v1/workflows/instances`: List workflow instances.
- `POST /api/v1/workflows/instances`: Submit an entity transition into approval.
- `GET /api/v1/workflows/instances/{instance_id}`: Retrieve an instance by ID.
- `POST /api/v1/workflows/instances/{instance_id}/approve`: Approve the current step.
- `POST /api/v1/workflows/instances/{instance_id}/reject`: Reject the current step.
- `POST /api/v1/workflows/instances/{instance_id}/cancel`: Cancel a pending/approved instance.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
)
from app.platform.workflow import service
from app.platform.workflow.schemas import (
    WorkflowDecisionInput,
    WorkflowDefinitionCreate,
    WorkflowDefinitionRead,
    WorkflowDefinitionUpdate,
    WorkflowInstanceRead,
    WorkflowInstanceSubmit,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


# ---------------------------------------------------------------------------
# Definition Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/definitions",
    response_model=list[WorkflowDefinitionRead],
    dependencies=[Depends(RequiresPermission("platform.workflow.read"))],
)
async def list_definitions_endpoint(
    session: DbSession,
    entity_type: Annotated[
        WorkflowEntityType | None,
        Query(description="Filter by entity type"),
    ] = None,
    transition: Annotated[
        WorkflowTransition | None,
        Query(description="Filter by transition"),
    ] = None,
    active_only: Annotated[
        bool,
        Query(description="Filter active definitions only"),
    ] = False,
) -> list[WorkflowDefinitionRead]:
    """List workflow definitions for the active tenant."""
    return await service.list_workflow_definitions(
        session,
        entity_type=entity_type,
        transition=transition,
        active_only=active_only,
    )


@router.post(
    "/definitions",
    response_model=WorkflowDefinitionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("platform.workflow.manage"))],
)
async def create_definition_endpoint(
    payload: WorkflowDefinitionCreate,
    session: DbSession,
) -> WorkflowDefinitionRead:
    """Create a new workflow definition for the active tenant."""
    return await service.create_workflow_definition(session, payload)


@router.get(
    "/definitions/{definition_id}",
    response_model=WorkflowDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.read"))],
)
async def get_definition_endpoint(
    definition_id: uuid.UUID,
    session: DbSession,
) -> WorkflowDefinitionRead:
    """Retrieve a single workflow definition by ID."""
    return await service.get_workflow_definition(session, definition_id)


@router.patch(
    "/definitions/{definition_id}",
    response_model=WorkflowDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.manage"))],
)
async def update_definition_endpoint(
    definition_id: uuid.UUID,
    payload: WorkflowDefinitionUpdate,
    session: DbSession,
) -> WorkflowDefinitionRead:
    """Update mutable attributes of an existing workflow definition."""
    return await service.update_workflow_definition(session, definition_id, payload)


@router.delete(
    "/definitions/{definition_id}",
    response_model=WorkflowDefinitionRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.manage"))],
)
async def deactivate_definition_endpoint(
    definition_id: uuid.UUID,
    session: DbSession,
) -> WorkflowDefinitionRead:
    """Soft deactivate a workflow definition for the active tenant."""
    return await service.deactivate_workflow_definition(session, definition_id)


# ---------------------------------------------------------------------------
# Instance Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/instances",
    response_model=list[WorkflowInstanceRead],
    dependencies=[Depends(RequiresPermission("platform.workflow.read"))],
)
async def list_instances_endpoint(
    session: DbSession,
    entity_type: Annotated[
        WorkflowEntityType | None,
        Query(description="Filter by entity type"),
    ] = None,
    entity_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter by entity ID"),
    ] = None,
    transition: Annotated[
        WorkflowTransition | None,
        Query(description="Filter by transition"),
    ] = None,
    status: Annotated[
        WorkflowInstanceStatus | None,
        Query(description="Filter by instance status"),
    ] = None,
) -> list[WorkflowInstanceRead]:
    """List workflow instances for the active tenant."""
    return await service.list_workflow_instances(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        transition=transition,
        status=status,
    )


@router.post(
    "/instances",
    response_model=WorkflowInstanceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RequiresPermission("platform.workflow.approve"))],
)
async def submit_instance_endpoint(
    payload: WorkflowInstanceSubmit,
    session: DbSession,
) -> WorkflowInstanceRead:
    """Submit an entity transition into workflow approval."""
    return await service.submit_workflow_instance(session, payload)


@router.get(
    "/instances/{instance_id}",
    response_model=WorkflowInstanceRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.read"))],
)
async def get_instance_endpoint(
    instance_id: uuid.UUID,
    session: DbSession,
) -> WorkflowInstanceRead:
    """Retrieve a single workflow instance with audit logs."""
    return await service.get_workflow_instance(session, instance_id)


@router.post(
    "/instances/{instance_id}/approve",
    response_model=WorkflowInstanceRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.approve"))],
)
async def approve_instance_endpoint(
    instance_id: uuid.UUID,
    session: DbSession,
    payload: WorkflowDecisionInput | None = None,
) -> WorkflowInstanceRead:
    """Approve the current step of a pending workflow instance."""
    return await service.approve_workflow_instance(session, instance_id, payload)


@router.post(
    "/instances/{instance_id}/reject",
    response_model=WorkflowInstanceRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.approve"))],
)
async def reject_instance_endpoint(
    instance_id: uuid.UUID,
    session: DbSession,
    payload: WorkflowDecisionInput | None = None,
) -> WorkflowInstanceRead:
    """Reject the current step of a pending workflow instance."""
    return await service.reject_workflow_instance(session, instance_id, payload)


@router.post(
    "/instances/{instance_id}/cancel",
    response_model=WorkflowInstanceRead,
    dependencies=[Depends(RequiresPermission("platform.workflow.approve"))],
)
async def cancel_instance_endpoint(
    instance_id: uuid.UUID,
    session: DbSession,
) -> WorkflowInstanceRead:
    """Cancel a pending or unconsumed approved workflow instance."""
    return await service.cancel_workflow_instance(session, instance_id)
