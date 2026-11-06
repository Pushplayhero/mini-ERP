"""platform.plugins service layer — ADR-010 / Week 10 Slice B.

Implements:
1. `list_company_plugins`: Retrieve all loaded plugins with active tenant's effective state.
2. `get_company_plugin`: Retrieve single plugin's effective state for active tenant.
3. `update_company_plugin`: Update/upsert tenant plugin state with locking & invariant checks.
4. `execute_with_plugin_gate_unchecked`: Tenant runtime gate (PluginRuntimeGate implementation).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import require_current_actor, require_permission
from app.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from app.core.plugins import PluginLoadError, PluginManifest, plugin_config_context
from app.core.tenancy import require_current_company_id
from app.modules.masterdata import service as masterdata_service
from app.platform.plugins import loader
from app.platform.plugins.models import CompanyPlugin
from app.platform.plugins.schemas import (
    CompanyPluginRead,
    CompanyPluginUpdate,
    PluginDependencyRead,
)


def _to_read_dto(manifest: PluginManifest, row: CompanyPlugin | None) -> CompanyPluginRead:
    is_enabled = row.is_enabled if row else manifest.default_enabled
    config = row.config if row else manifest.default_config_dict
    return CompanyPluginRead(
        plugin_name=manifest.name,
        version=manifest.version,
        title=manifest.title,
        description=manifest.description,
        default_enabled=manifest.default_enabled,
        requires=[
            PluginDependencyRead(plugin_name=dep.plugin_name, min_version=dep.min_version)
            for dep in manifest.requires
        ],
        is_enabled=is_enabled,
        config=config,
        is_customized=row is not None,
    )


async def list_company_plugins(session: AsyncSession) -> list[CompanyPluginRead]:
    """Return all loaded plugins in topological order with active tenant's effective state."""
    require_permission("platform.plugin.read")
    require_current_company_id()

    # Query all customized rows for current company (auto-filtered by TenantScopedMixin)
    res = await session.execute(select(CompanyPlugin))
    rows_by_name = {row.plugin_name: row for row in res.scalars().all()}

    return [_to_read_dto(m, rows_by_name.get(m.name)) for m in loader.get_loaded_plugins()]


async def get_company_plugin(session: AsyncSession, plugin_name: str) -> CompanyPluginRead:
    """Return a single plugin's effective state for the active tenant."""
    require_permission("platform.plugin.read")
    require_current_company_id()

    manifest = loader.get_loaded_plugin(plugin_name)
    if manifest is None:
        raise NotFoundError("Plugin", plugin_name)

    res = await session.execute(
        select(CompanyPlugin).where(CompanyPlugin.plugin_name == plugin_name)
    )
    row = res.scalars().first()
    return _to_read_dto(manifest, row)


def _normalize_and_validate_config(
    manifest: PluginManifest,
    plugin_name: str,
    raw_config: dict[str, Any],
) -> dict[str, Any]:
    if manifest.config_schema is None:
        if raw_config:
            raise DomainValidationError(
                f"Plugin {plugin_name!r} does not declare a configuration schema and "
                "rejects non-empty configuration"
            )
        return {}
    try:
        validated_model = manifest.config_schema.model_validate(raw_config)
        return validated_model.model_dump(mode="json")
    except ValidationError as exc:
        raise DomainValidationError(
            f"Configuration validation failed for plugin {plugin_name!r}: {exc}"
        ) from exc


def _check_dependency_invariants(
    manifest: PluginManifest,
    target_is_enabled: bool,
    existing_rows: dict[str, CompanyPlugin],
) -> None:
    all_loaded = loader.get_loaded_plugins()
    proposed_states: dict[str, bool] = {}
    for m in all_loaded:
        if m.name == manifest.name:
            proposed_states[m.name] = target_is_enabled
        else:
            r = existing_rows.get(m.name)
            proposed_states[m.name] = r.is_enabled if r else m.default_enabled

    # Enable check: if turning enabled, all prerequisites must be enabled
    if target_is_enabled:
        for dep in manifest.requires:
            if not proposed_states.get(dep.plugin_name, False):
                raise DomainValidationError(
                    f"Cannot enable plugin {manifest.name!r}: prerequisite plugin "
                    f"{dep.plugin_name!r} is disabled for this company"
                )

    # Disable check: if turning disabled, no active plugin may depend on this
    if not target_is_enabled:
        for other in all_loaded:
            if other.name != manifest.name and proposed_states.get(other.name, False):
                for dep in other.requires:
                    if dep.plugin_name == manifest.name:
                        raise ConflictError(
                            f"Cannot disable plugin {manifest.name!r}: dependent plugin "
                            f"{other.name!r} is currently enabled for this company"
                        )


async def update_company_plugin(
    session: AsyncSession,
    plugin_name: str,
    data: CompanyPluginUpdate,
) -> CompanyPluginRead:
    """Update or upsert tenant plugin enablement and configuration.

    1. Checks 'platform.plugin.manage' permission.
    2. Validates plugin exists in loader.
    3. Serializes via `masterdata_service.lock_company_unchecked(session, company_id)`.
    4. Loads fresh tenant plugin rows with `populate_existing=True`.
    5. Merges omitted fields with existing row (or manifest defaults).
    6. Validates config against manifest.config_schema (translating ValidationError to 422).
    7. Verifies runtime dependency invariants across all loaded plugins (422 / 409).
    8. Upserts `company_plugins` row, sets audit fields, commits, and returns read DTO.
    """
    require_permission("platform.plugin.manage")
    company_id = require_current_company_id()
    actor = require_current_actor()

    manifest = loader.get_loaded_plugin(plugin_name)
    if manifest is None:
        raise NotFoundError("Plugin", plugin_name)

    # 1. Lock tenant root row FOR UPDATE to serialize all plugin mutations for this company
    await masterdata_service.lock_company_unchecked(session, company_id)

    # 2. Load fresh snapshot of existing rows with populate_existing=True
    res = await session.execute(select(CompanyPlugin).execution_options(populate_existing=True))
    existing_rows = {r.plugin_name: r for r in res.scalars().all()}
    target_row = existing_rows.get(plugin_name)

    # 3. Determine proposed target values
    if data.is_enabled is not None:
        target_is_enabled = data.is_enabled
    elif target_row is not None:
        target_is_enabled = target_row.is_enabled
    else:
        target_is_enabled = manifest.default_enabled

    if data.config is not None:
        proposed_raw_config: dict[str, Any] = data.config
    elif target_row is not None:
        proposed_raw_config = target_row.config
    else:
        proposed_raw_config = manifest.default_config_dict

    # 4. Validate proposed configuration
    normalized_config = _normalize_and_validate_config(manifest, plugin_name, proposed_raw_config)

    # 5. Check runtime dependency invariants across all loaded plugins
    _check_dependency_invariants(manifest, target_is_enabled, existing_rows)

    # 6. Upsert row
    if target_row is not None:
        target_row.is_enabled = target_is_enabled
        target_row.config = normalized_config
        target_row.updated_by = actor.user_id
    else:
        target_row = CompanyPlugin(
            id=uuid.uuid4(),
            company_id=company_id,
            plugin_name=plugin_name,
            is_enabled=target_is_enabled,
            config=normalized_config,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        session.add(target_row)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Plugin {plugin_name!r} state conflict on commit") from exc

    return _to_read_dto(manifest, target_row)


async def execute_with_plugin_gate_unchecked(
    session: AsyncSession,
    plugin_name: str,
    invoke: Callable[[], Awaitable[None]],
) -> None:
    """Runtime execution gate evaluating tenant enablement and binding configuration."""
    require_current_company_id()

    manifest = loader.get_loaded_plugin(plugin_name)
    if manifest is None:
        raise PluginLoadError(
            f"Execution gate invoked for plugin {plugin_name!r} which is not loaded"
        )

    res = await session.execute(
        select(CompanyPlugin).where(CompanyPlugin.plugin_name == plugin_name)
    )
    row = res.scalars().first()

    is_enabled = row.is_enabled if row else manifest.default_enabled
    if not is_enabled:
        return

    raw_config = row.config if row else manifest.default_config_dict

    config_model: BaseModel | None = None
    if manifest.config_schema is not None:
        try:
            config_model = manifest.config_schema.model_validate(raw_config)
        except ValidationError as exc:
            raise PluginLoadError(
                f"Stored configuration for plugin {plugin_name!r} failed schema validation: {exc}"
            ) from exc

    with plugin_config_context(config_model):
        await invoke()
