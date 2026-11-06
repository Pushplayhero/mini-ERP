"""platform.plugins FastAPI routes — ADR-010 / Week 10 Slice B.

Endpoints mounted under `/api/v1/plugins`:
- `GET /api/v1/plugins`: List all loaded plugins with effective enablement/config for active tenant.
- `GET /api/v1/plugins/{plugin_name}`: Get effective enablement/config for a single plugin.
- `PATCH /api/v1/plugins/{plugin_name}`: Update enablement and/or configuration for a single plugin.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import RequiresPermission
from app.core.db import get_db_session
from app.platform.plugins import service
from app.platform.plugins.schemas import CompanyPluginRead, CompanyPluginUpdate

router = APIRouter(prefix="/plugins", tags=["plugins"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "",
    response_model=list[CompanyPluginRead],
    dependencies=[Depends(RequiresPermission("platform.plugin.read"))],
)
async def list_plugins_endpoint(session: DbSession) -> list[CompanyPluginRead]:
    """List all loaded plugins in topological order with their active company status."""
    return await service.list_company_plugins(session)


@router.get(
    "/{plugin_name}",
    response_model=CompanyPluginRead,
    dependencies=[Depends(RequiresPermission("platform.plugin.read"))],
)
async def get_plugin_endpoint(
    plugin_name: str,
    session: DbSession,
) -> CompanyPluginRead:
    """Retrieve details and effective company status for a specific plugin."""
    return await service.get_company_plugin(session, plugin_name)


@router.patch(
    "/{plugin_name}",
    response_model=CompanyPluginRead,
    dependencies=[Depends(RequiresPermission("platform.plugin.manage"))],
)
async def update_plugin_endpoint(
    plugin_name: str,
    payload: CompanyPluginUpdate,
    session: DbSession,
) -> CompanyPluginRead:
    """Update or upsert active company's enablement or configuration for a plugin."""
    return await service.update_company_plugin(session, plugin_name, payload)
