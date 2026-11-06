"""tests/platform/test_plugin_runtime_gate.py — PluginRuntimeGate implementation tests.

Validates:
1. Gated handler execution when plugin is enabled (default or configured).
2. Gated handler bypass when plugin is disabled for active company.
3. Configuration injection into plugin_config_context and access via get_current_plugin_config.
4. Tenant isolation across distinct companies.
5. Unstamped (core) handlers run unconditionally with zero plugin DB overhead.
6. Fail-closed behaviour: missing tenant context, unloaded plugin, corrupted DB config.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import events, hooks
from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import TenancyContextError
from app.core.hooks import HookContext
from app.core.plugins import (
    EventBinding,
    HookBinding,
    PluginLoadError,
    PluginManifest,
    get_current_plugin_config,
    get_plugin_runtime_gate,
    set_plugin_runtime_gate,
)
from app.core.tenancy import company_context
from app.platform.plugins import loader
from app.platform.plugins import service as plugins_service
from app.platform.plugins.models import CompanyPlugin
from app.platform.plugins.schemas import CompanyPluginUpdate


class GateTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_amount: Decimal = Decimal("500.000000")
    label: str = "init"


class GateEventPayload(BaseModel):
    company_id: uuid.UUID
    order_no: str


@pytest.fixture(autouse=True)
def _cleanup_loader_and_registries() -> Any:
    saved_gate = get_plugin_runtime_gate()
    saved_loaded = dict(loader._loaded_plugins)
    saved_hook_specs = dict(hooks._hook_specs)
    saved_handlers = {k: list(v) for k, v in hooks._handlers.items()}
    saved_event_schemas = dict(events._schemas)
    saved_event_handlers = {k: list(v) for k, v in events._handlers.items()}
    loader.reset_loaded_plugins()
    set_plugin_runtime_gate(plugins_service.execute_with_plugin_gate_unchecked)
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
    set_plugin_runtime_gate(saved_gate)


@pytest.fixture
async def two_companies(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    from app.modules.masterdata.models import Company

    c1 = Company(
        code=f"C1_{uuid.uuid4().hex[:5].upper()}",
        name="Company One",
        functional_currency_code="TWD",
    )
    c2 = Company(
        code=f"C2_{uuid.uuid4().hex[:5].upper()}",
        name="Company Two",
        functional_currency_code="TWD",
    )
    db_session.add_all([c1, c2])
    await db_session.commit()
    return c1.id, c2.id


@pytest.mark.asyncio
async def test_gate_allows_enabled_and_bypasses_disabled(
    db_session: AsyncSession,
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    c1_id, c2_id = two_companies
    invoked: list[str] = []

    async def my_handler(_session: AsyncSession, _ctx: HookContext) -> None:
        cfg = get_current_plugin_config(GateTestConfig)
        invoked.append(f"hook:{cfg.label}:{cfg.max_amount}")

    async def my_event_handler(_session: AsyncSession, payload: BaseModel) -> None:
        cfg = get_current_plugin_config(GateTestConfig)
        order_no = getattr(payload, "order_no", "")
        invoked.append(f"event:{order_no}:{cfg.label}:{cfg.max_amount}")

    hooks.define_hook_point("sales.gate.test", "1.0.0")
    events.register_event("sales.gate.event", GateEventPayload)

    manifest = PluginManifest(
        name="test_gated_plugin",
        version="1.0.0",
        title="Gated Plugin",
        description="test",
        default_enabled=True,
        config_schema=GateTestConfig,
        hooks=(HookBinding("sales.gate.test", "1.0.0", my_handler),),
        events=(EventBinding("sales.gate.event", my_event_handler),),
    )
    loader.bootstrap_plugins([manifest], enable_entrypoints=False)

    ctx_c1 = HookContext(
        company_id=c1_id,
        order_id=uuid.uuid4(),
        order_no="SO-1",
        customer_id=uuid.uuid4(),
        total=Decimal("100"),
    )
    ctx_c2 = HookContext(
        company_id=c2_id,
        order_id=uuid.uuid4(),
        order_no="SO-2",
        customer_id=uuid.uuid4(),
        total=Decimal("200"),
    )

    # 1. Company 1 runs with default enabled and default config (both hook & event)
    with company_context(c1_id):
        await hooks.run("sales.gate.test", db_session, ctx_c1)
        await events.redispatch(
            db_session,
            "sales.gate.event",
            {"company_id": str(c1_id), "order_no": "SO-1"},
        )
        assert invoked == [
            "hook:init:500.000000",
            "event:SO-1:init:500.000000",
        ]

    invoked.clear()

    # 2. Disable plugin for Company 1, customize config for Company 2
    admin_actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"platform.plugin.manage"}),
    )
    with actor_context(admin_actor), company_context(c1_id):
        await plugins_service.update_company_plugin(
            db_session,
            "test_gated_plugin",
            CompanyPluginUpdate(is_enabled=False),
        )

    with actor_context(admin_actor), company_context(c2_id):
        await plugins_service.update_company_plugin(
            db_session,
            "test_gated_plugin",
            CompanyPluginUpdate(
                is_enabled=True,
                config={"max_amount": "999.000000", "label": "custom_c2"},
            ),
        )

    # 3. Execution for Company 1 -> Bypassed for both hook and event!
    with company_context(c1_id):
        await hooks.run("sales.gate.test", db_session, ctx_c1)
        await events.redispatch(
            db_session,
            "sales.gate.event",
            {"company_id": str(c1_id), "order_no": "SO-1"},
        )
        assert invoked == []

    # 4. Execution for Company 2 -> Runs with Company 2's custom config for both hook and event!
    with company_context(c2_id):
        await hooks.run("sales.gate.test", db_session, ctx_c2)
        await events.redispatch(
            db_session,
            "sales.gate.event",
            {"company_id": str(c2_id), "order_no": "SO-2"},
        )
        assert invoked == [
            "hook:custom_c2:999.000000",
            "event:SO-2:custom_c2:999.000000",
        ]


@pytest.mark.asyncio
async def test_unstamped_core_handler_runs_unconditionally(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    c1_id, _ = two_companies
    ran: list[bool] = []
    executed_sql: list[str] = []

    # Plain handler without __plugin_name__
    async def core_handler(_session: AsyncSession, _ctx: HookContext) -> None:
        ran.append(True)

    hooks.define_hook_point("core.test.point", "1.0.0")
    hooks.register("core.test.point", core_handler)

    ctx = HookContext(
        company_id=c1_id,
        order_id=uuid.uuid4(),
        order_no="SO-C",
        customer_id=uuid.uuid4(),
        total=Decimal("50"),
    )

    def _on_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        executed_sql.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _on_cursor_execute)
    try:
        with company_context(c1_id):
            await hooks.run("core.test.point", db_session, ctx)
            assert ran == [True]
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _on_cursor_execute)

    # Zero queries to company_plugins for unstamped core handlers
    assert [s for s in executed_sql if "company_plugins" in s] == []


@pytest.mark.asyncio
async def test_gate_fail_closed_error_conditions(
    db_session: AsyncSession,
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    c1_id, _ = two_companies

    async def dummy_invoke() -> None:
        pass

    # 1. Missing tenant context -> raises TenancyContextError
    with pytest.raises(TenancyContextError, match="No company context is set"):
        await plugins_service.execute_with_plugin_gate_unchecked(
            db_session,
            "some_plugin",
            dummy_invoke,
        )

    # 2. Unloaded plugin -> raises PluginLoadError
    with (
        company_context(c1_id),
        pytest.raises(PluginLoadError, match="not loaded"),
    ):
        await plugins_service.execute_with_plugin_gate_unchecked(
            db_session,
            "unloaded_plugin",
            dummy_invoke,
        )

    # 3. Corrupted DB configuration failing schema validation -> raises PluginLoadError
    corrupt_manifest = PluginManifest(
        name="corrupt_cfg_plugin",
        version="1.0.0",
        title="Corrupt Config Test",
        description="test",
        default_enabled=True,
        config_schema=GateTestConfig,
    )
    loader.bootstrap_plugins([corrupt_manifest], enable_entrypoints=False)

    corrupt_row = CompanyPlugin(
        id=uuid.uuid4(),
        company_id=c1_id,
        plugin_name="corrupt_cfg_plugin",
        is_enabled=True,
        config={"max_amount": "corrupted_not_a_decimal"},
    )
    db_session.add(corrupt_row)
    await db_session.commit()

    with (
        company_context(c1_id),
        pytest.raises(PluginLoadError, match="failed schema validation"),
    ):
        await plugins_service.execute_with_plugin_gate_unchecked(
            db_session,
            "corrupt_cfg_plugin",
            dummy_invoke,
        )
