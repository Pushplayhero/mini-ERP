"""tests/platform/test_company_plugins_service.py — Service layer tests for company_plugins.

Validates:
1. `list_company_plugins` default-state fallback and customization detection (`is_customized`).
2. `get_company_plugin` single plugin retrieval and 404 on unknown plugin.
3. `update_company_plugin`:
   - Runtime dependency invariants: 422 (disabled prerequisite) and 409 (active dependent).
   - Config validation: rejecting non-empty config when config_schema is None (422); errors (422).
   - Partial updates: preserving existing values when omitted; default fallback on first upsert.
   - Decimal serialization: JSON normalization in stored config.
   - Permission checks: requires 'platform.plugin.read' / 'platform.plugin.manage'.
4. `lock_company_unchecked`:
   - Locks company row FOR UPDATE.
   - Raises NotFoundError on absent company.
   - Enforces fail-closed tenancy context match.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events, hooks
from app.core.authorization import (
    ActorContext,
    actor_context,
)
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    TenancyContextError,
)
from app.core.plugins import (
    PluginDependency,
    PluginManifest,
)
from app.core.tenancy import company_context
from app.modules.masterdata import service as masterdata_service
from app.platform.plugins import loader
from app.platform.plugins import service as plugins_service
from app.platform.plugins.schemas import CompanyPluginUpdate


class SampleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: Decimal = Decimal("100.000000")
    tag: str = "default"


@pytest.fixture(autouse=True)
def _cleanup_loader_and_registries() -> Any:
    saved_loaded = dict(loader._loaded_plugins)
    saved_hook_specs = dict(hooks._hook_specs)
    saved_handlers = {k: list(v) for k, v in hooks._handlers.items()}
    saved_event_schemas = dict(events._schemas)
    saved_event_handlers = {k: list(v) for k, v in events._handlers.items()}
    loader.reset_loaded_plugins()
    yield
    loader.reset_loaded_plugins()
    loader._loaded_plugins.update(saved_loaded)
    hooks._hook_specs.clear()
    hooks._hook_specs.update(saved_hook_specs)
    hooks._handlers.clear()
    hooks._handlers.update(saved_handlers)
    events._schemas.clear()
    events._schemas.update(saved_event_schemas)
    events._handlers.clear()
    events._handlers.update(saved_event_handlers)


@pytest.fixture
def test_manifests() -> tuple[PluginManifest, PluginManifest, PluginManifest]:
    p_base = PluginManifest(
        name="plugin_base",
        version="1.0.0",
        title="Base Plugin",
        description="Base prerequisite",
        default_enabled=True,
    )
    p_dep = PluginManifest(
        name="plugin_dep",
        version="1.0.0",
        title="Dependent Plugin",
        description="Depends on base",
        default_enabled=True,
        requires=(PluginDependency("plugin_base", "1.0.0"),),
    )
    p_config = PluginManifest(
        name="plugin_config",
        version="1.0.0",
        title="Configurable Plugin",
        description="Has config schema",
        default_enabled=False,
        config_schema=SampleConfig,
    )
    return p_base, p_dep, p_config


@pytest.fixture
async def sample_company(db_session: AsyncSession) -> uuid.UUID:
    from app.modules.masterdata.models import Company

    company = Company(
        code=f"C_{uuid.uuid4().hex[:6].upper()}",
        name="Plugin Test Co",
        functional_currency_code="TWD",
    )
    db_session.add(company)
    await db_session.commit()
    return company.id


@pytest.fixture
def admin_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.plugin.read", "platform.plugin.manage"}),
    )


@pytest.mark.asyncio
async def test_list_company_plugins_default_fallback(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    test_manifests: tuple[PluginManifest, PluginManifest, PluginManifest],
) -> None:
    loader.bootstrap_plugins(test_manifests, enable_entrypoints=False)

    with actor_context(admin_actor), company_context(sample_company):
        plugins = await plugins_service.list_company_plugins(db_session)
        assert len(plugins) == 3
        # In deterministic topological order (Kahn's min-heap tie-breaking)
        names = [p.plugin_name for p in plugins]
        assert names == ["plugin_base", "plugin_config", "plugin_dep"]

        p_base = next(p for p in plugins if p.plugin_name == "plugin_base")
        assert p_base.is_enabled is True
        assert p_base.is_customized is False

        p_cfg = next(p for p in plugins if p.plugin_name == "plugin_config")
        assert p_cfg.is_enabled is False
        assert p_cfg.is_customized is False
        assert p_cfg.config == {"limit": "100.000000", "tag": "default"}


@pytest.mark.asyncio
async def test_get_company_plugin_success_and_not_found(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    test_manifests: tuple[PluginManifest, PluginManifest, PluginManifest],
) -> None:
    loader.bootstrap_plugins(test_manifests, enable_entrypoints=False)

    with actor_context(admin_actor), company_context(sample_company):
        p = await plugins_service.get_company_plugin(db_session, "plugin_base")
        assert p.plugin_name == "plugin_base"
        assert p.is_enabled is True

        with pytest.raises(NotFoundError, match="Plugin"):
            await plugins_service.get_company_plugin(db_session, "nonexistent")


@pytest.mark.asyncio
async def test_update_company_plugin_dependency_guards(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    test_manifests: tuple[PluginManifest, PluginManifest, PluginManifest],
) -> None:
    loader.bootstrap_plugins(test_manifests, enable_entrypoints=False)

    with actor_context(admin_actor), company_context(sample_company):
        # 1. Disabling prerequisite 'plugin_base' while 'plugin_dep' is enabled -> 409
        dep_enabled_msg = "dependent plugin 'plugin_dep' is currently enabled"
        with pytest.raises(ConflictError, match=dep_enabled_msg):
            await plugins_service.update_company_plugin(
                db_session,
                "plugin_base",
                CompanyPluginUpdate(is_enabled=False),
            )

        # 2. First disable 'plugin_dep' -> succeeds
        updated_dep = await plugins_service.update_company_plugin(
            db_session,
            "plugin_dep",
            CompanyPluginUpdate(is_enabled=False),
        )
        assert updated_dep.is_enabled is False
        assert updated_dep.is_customized is True

        # 3. Now disabling 'plugin_base' -> succeeds
        updated_base = await plugins_service.update_company_plugin(
            db_session,
            "plugin_base",
            CompanyPluginUpdate(is_enabled=False),
        )
        assert updated_base.is_enabled is False

        # 4. Trying to enable 'plugin_dep' while prerequisite is disabled -> 422
        match_msg = "prerequisite plugin 'plugin_base' is disabled"
        with pytest.raises(DomainValidationError, match=match_msg):
            await plugins_service.update_company_plugin(
                db_session,
                "plugin_dep",
                CompanyPluginUpdate(is_enabled=True),
            )


@pytest.mark.asyncio
async def test_update_company_plugin_config_validation(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    admin_actor: ActorContext,
    test_manifests: tuple[PluginManifest, PluginManifest, PluginManifest],
) -> None:
    loader.bootstrap_plugins(test_manifests, enable_entrypoints=False)

    with actor_context(admin_actor), company_context(sample_company):
        # 1. Plugin without config_schema rejects non-empty config
        with pytest.raises(DomainValidationError, match="rejects non-empty configuration"):
            await plugins_service.update_company_plugin(
                db_session,
                "plugin_base",
                CompanyPluginUpdate(config={"some": "param"}),
            )

        # 2. Plugin with config_schema rejects schema-violating config
        with pytest.raises(DomainValidationError, match="Configuration validation failed"):
            await plugins_service.update_company_plugin(
                db_session,
                "plugin_config",
                CompanyPluginUpdate(config={"limit": "not-a-number"}),
            )

        # Extra fields forbidden
        with pytest.raises(DomainValidationError, match="Configuration validation failed"):
            await plugins_service.update_company_plugin(
                db_session,
                "plugin_config",
                CompanyPluginUpdate(config={"extra_field": 123}),
            )

        # 3. First-time upsert omitting config populates manifest.default_config_dict
        res_first = await plugins_service.update_company_plugin(
            db_session,
            "plugin_config",
            CompanyPluginUpdate(is_enabled=True),
        )
        assert res_first.is_enabled is True
        assert res_first.is_customized is True
        assert res_first.config == {"limit": "100.000000", "tag": "default"}

        # 4. Valid config succeeds, normalizes Decimal, and preserves is_enabled=True
        res = await plugins_service.update_company_plugin(
            db_session,
            "plugin_config",
            CompanyPluginUpdate(
                config={"limit": "250.500000", "tag": "custom"},
            ),
        )
        assert res.is_enabled is True
        assert res.is_customized is True
        assert res.config == {"limit": "250.500000", "tag": "custom"}

        # 5. Partial update (omitting config preserves previous custom config)
        res_partial = await plugins_service.update_company_plugin(
            db_session,
            "plugin_config",
            CompanyPluginUpdate(is_enabled=False),
        )
        assert res_partial.is_enabled is False
        assert res_partial.config == {"limit": "250.500000", "tag": "custom"}


@pytest.mark.asyncio
async def test_lock_company_unchecked(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
) -> None:
    # 1. Active company matches -> locks and returns company
    with company_context(sample_company):
        locked = await masterdata_service.lock_company_unchecked(db_session, sample_company)
        assert locked.id == sample_company

    # 2. Unknown company id with context -> raises TenancyContextError (mismatch)
    other_id = uuid.uuid4()
    with (
        company_context(sample_company),
        pytest.raises(TenancyContextError, match="does not match active company context"),
    ):
        await masterdata_service.lock_company_unchecked(db_session, other_id)

    # 3. Context matches nonexistent company -> raises NotFoundError
    with (
        company_context(other_id),
        pytest.raises(NotFoundError, match="Company"),
    ):
        await masterdata_service.lock_company_unchecked(db_session, other_id)


@pytest.mark.asyncio
async def test_service_permissions_enforced(
    db_session: AsyncSession,
    sample_company: uuid.UUID,
    test_manifests: tuple[PluginManifest, PluginManifest, PluginManifest],
) -> None:
    loader.bootstrap_plugins(test_manifests, enable_entrypoints=False)
    read_only_actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.plugin.read"}),
    )

    with company_context(sample_company):
        # Missing actor context -> ActionDeniedError
        with pytest.raises(ActionDeniedError):
            await plugins_service.list_company_plugins(db_session)

        # Read-only actor can list and get, but cannot update
        with actor_context(read_only_actor):
            await plugins_service.list_company_plugins(db_session)
            await plugins_service.get_company_plugin(db_session, "plugin_base")

            match_manage = "lacks permission 'platform.plugin.manage'"
            with pytest.raises(ActionDeniedError, match=match_manage):
                await plugins_service.update_company_plugin(
                    db_session,
                    "plugin_base",
                    CompanyPluginUpdate(is_enabled=False),
                )
