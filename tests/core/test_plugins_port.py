"""tests/core/test_plugins_port.py — Core plugin contracts and runtime gate primitives (ADR-010).

Validates:
1. SemVer parsing and Caret compatibility rules (0.x vs 1.x).
2. PluginManifest validation (names, titles, dependencies, duplicate bindings, config_schema).
3. Safe handler stamping (__plugin_name__ attribute uniqueness and preservation).
4. Inductive Invariant B (unconditional default model_validate({}) viability).
5. plugin_config_context ContextVar mechanics and type enforcement.
6. PluginRuntimeGate port wiring and bypass logic.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events, hooks
from app.core.hooks import HookContext
from app.core.plugins import (
    EventBinding,
    HookBinding,
    PluginDependency,
    PluginLoadError,
    PluginManifest,
    get_current_plugin_config,
    get_plugin_runtime_gate,
    is_semver_compatible,
    parse_semver,
    plugin_config_context,
    set_plugin_runtime_gate,
)


def test_parse_semver_valid() -> None:
    assert parse_semver("0.1.0") == (0, 1, 0)
    assert parse_semver("1.2.3") == (1, 2, 3)
    assert parse_semver("10.200.3000") == (10, 200, 3000)


@pytest.mark.parametrize(
    "invalid", ["1.0", "1.0.0.0", "v1.0.0", "1.0.0-beta", "01.0.0", "1.a.0", ""]
)
def test_parse_semver_invalid_raises(invalid: str) -> None:
    with pytest.raises(PluginLoadError, match="not a valid strict SemVer"):
        parse_semver(invalid)


@pytest.mark.parametrize(
    "actual,required,expected",
    [
        ("1.0.0", "1.0.0", True),
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.0", True),
        ("1.0.0", "1.0.1", False),
        ("1.0.0", "1.1.0", False),
        ("2.0.0", "1.0.0", False),
        ("1.9.9", "2.0.0", False),
        # 0.x rules
        ("0.1.0", "0.1.0", True),
        ("0.1.2", "0.1.0", True),
        ("0.1.0", "0.1.1", False),
        ("0.2.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("1.0.0", "0.1.0", False),
    ],
)
def test_is_semver_compatible(actual: str, required: str, expected: bool) -> None:
    assert is_semver_compatible(actual, required) is expected


class DummyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tolerance: Decimal = Decimal("0.000000")


class ConfigMissingForbid(BaseModel):
    tolerance: Decimal = Decimal("0.000000")


class ConfigRequiringNonDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_field: str


def test_manifest_validation_valid() -> None:
    async def sample_hook(_session: AsyncSession, _ctx: HookContext) -> None:
        pass

    m = PluginManifest(
        name="test_plugin",
        version="1.0.0",
        title="Test Plugin",
        description="A test plugin",
        default_enabled=True,
        requires=(PluginDependency("dep_a", "1.0.0"),),
        config_schema=DummyConfig,
        hooks=(HookBinding("sales.order.validate_confirm", "1.0.0", sample_hook),),
    )
    assert m.name == "test_plugin"
    assert m.default_config_dict == {"tolerance": "0.000000"}
    assert getattr(sample_hook, "__plugin_name__", None) == "test_plugin"


def test_manifest_invalid_name_raises() -> None:
    with pytest.raises(PluginLoadError, match="must match pattern"):
        PluginManifest(
            name="Invalid-Name",
            version="1.0.0",
            title="Invalid",
            description="",
        )


def test_manifest_blank_title_raises() -> None:
    with pytest.raises(PluginLoadError, match="title cannot be blank"):
        PluginManifest(
            name="valid_name",
            version="1.0.0",
            title="   ",
            description="",
        )


def test_manifest_self_dependency_raises() -> None:
    with pytest.raises(PluginLoadError, match="cannot depend on itself"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Self Dep",
            description="",
            requires=(PluginDependency("my_plugin", "1.0.0"),),
        )


def test_manifest_duplicate_dependency_raises() -> None:
    with pytest.raises(PluginLoadError, match="duplicate dependency"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Dup Dep",
            description="",
            requires=(
                PluginDependency("other", "1.0.0"),
                PluginDependency("other", "1.1.0"),
            ),
        )


def test_manifest_duplicate_hook_binding_raises() -> None:
    async def h(_s: AsyncSession, _c: HookContext) -> None:
        pass

    with pytest.raises(PluginLoadError, match="duplicate hook binding"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Dup Hook",
            description="",
            hooks=(
                HookBinding("h1", "1.0.0", h),
                HookBinding("h1", "1.0.0", h),
            ),
        )


def test_manifest_duplicate_event_binding_raises() -> None:
    async def eh(_s: AsyncSession, _p: BaseModel) -> None:
        pass

    with pytest.raises(PluginLoadError, match="duplicate event binding"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Dup Event",
            description="",
            events=(
                EventBinding("e1", eh),
                EventBinding("e1", eh),
            ),
        )


def test_manifest_reusing_same_handler_across_hooks_and_events_raises() -> None:
    async def shared(_s: AsyncSession, _arg: Any) -> None:
        pass

    with pytest.raises(
        PluginLoadError,
        match="cannot reuse the same handler .* across both hooks and events",
    ):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Hook and Event Reuse",
            description="",
            hooks=(HookBinding("h1", "1.0.0", shared),),
            events=(EventBinding("e1", shared),),
        )


def test_manifest_config_schema_must_have_extra_forbid() -> None:
    with pytest.raises(PluginLoadError, match="must configure model_config extra='forbid'"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="No Extra Forbid",
            description="",
            config_schema=ConfigMissingForbid,
        )


def test_manifest_config_schema_invariant_b_requires_empty_dict_viability() -> None:
    with pytest.raises(PluginLoadError, match="failed default model_validate"):
        PluginManifest(
            name="my_plugin",
            version="1.0.0",
            title="Bad Config",
            description="",
            config_schema=ConfigRequiringNonDefault,
        )


def test_manifest_safe_handler_stamping_rejects_sharing_without_mutation() -> None:
    async def shared_handler(_s: AsyncSession, _c: HookContext) -> None:
        pass

    # First manifest claims the handler
    PluginManifest(
        name="plugin_one",
        version="1.0.0",
        title="Plugin One",
        description="",
        hooks=(HookBinding("h1", "1.0.0", shared_handler),),
    )
    assert getattr(shared_handler, "__plugin_name__", None) == "plugin_one"

    # Second manifest tries to claim the same handler -> must fail and preserve plugin_one
    with pytest.raises(PluginLoadError, match="already bound to plugin 'plugin_one'"):
        PluginManifest(
            name="plugin_two",
            version="1.0.0",
            title="Plugin Two",
            description="",
            hooks=(HookBinding("h1", "1.0.0", shared_handler),),
        )

    assert getattr(shared_handler, "__plugin_name__", None) == "plugin_one"


def test_plugin_config_context_fallback_and_scoping() -> None:
    # 1. Without context bound, fallback creates default instance
    cfg = get_current_plugin_config(DummyConfig)
    assert cfg.tolerance == Decimal("0.000000")

    # 2. Within context manager, bound instance is returned
    custom = DummyConfig(tolerance=Decimal("50.000000"))
    with plugin_config_context(custom):
        active = get_current_plugin_config(DummyConfig)
        assert active.tolerance == Decimal("50.000000")

    # 3. Exiting context manager restores previous state
    assert get_current_plugin_config(DummyConfig).tolerance == Decimal("0.000000")


def test_plugin_config_context_type_mismatch_raises() -> None:
    class OtherConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = "default"

    with (
        plugin_config_context(OtherConfig()),
        pytest.raises(PluginLoadError, match="Expected plugin config schema DummyConfig"),
    ):
        get_current_plugin_config(DummyConfig)


@pytest.mark.asyncio
async def test_hooks_run_intercepts_stamped_handler_with_runtime_gate(
    db_session: AsyncSession,
) -> None:
    async def sample_handler(_s: AsyncSession, _c: HookContext) -> None:
        pass

    PluginManifest(
        name="gated_plugin",
        version="1.0.0",
        title="Gated",
        description="",
        hooks=(HookBinding("test_point", "1.0.0", sample_handler),),
    )

    saved_specs = dict(hooks._hook_specs)
    saved_handlers = {k: list(v) for k, v in hooks._handlers.items()}
    hooks.define_hook_point("test_point", "1.0.0")
    hooks.register("test_point", sample_handler)

    intercepted: list[str] = []

    async def fake_gate(
        session: AsyncSession,
        plugin_name: str,
        invoke: Any,
    ) -> None:
        intercepted.append(plugin_name)
        await invoke()

    saved_gate = get_plugin_runtime_gate()
    set_plugin_runtime_gate(fake_gate)
    try:
        ctx = HookContext(
            company_id=uuid.uuid4(),
            order_id=uuid.uuid4(),
            order_no="SO-001",
            customer_id=uuid.uuid4(),
            total=Decimal("100"),
        )
        await hooks.run("test_point", db_session, ctx)
        assert intercepted == ["gated_plugin"]
    finally:
        set_plugin_runtime_gate(saved_gate)
        hooks._hook_specs.clear()
        hooks._hook_specs.update(saved_specs)
        hooks._handlers.clear()
        hooks._handlers.update(saved_handlers)


@pytest.mark.asyncio
async def test_events_dispatch_intercepts_stamped_handler_with_runtime_gate(
    db_session: AsyncSession,
) -> None:
    class _TestEventPayload(BaseModel):
        company_id: uuid.UUID

    async def sample_event_handler(_s: AsyncSession, _p: BaseModel) -> None:
        pass

    PluginManifest(
        name="gated_event_plugin",
        version="1.0.0",
        title="Gated Event",
        description="",
        events=(EventBinding("test_event_point", sample_event_handler),),
    )

    saved_schemas = dict(events._schemas)
    saved_handlers = {k: list(v) for k, v in events._handlers.items()}
    events.register_event("test_event_point", _TestEventPayload)
    events.subscribe("test_event_point", sample_event_handler)

    intercepted: list[str] = []

    async def fake_gate(
        session: AsyncSession,
        plugin_name: str,
        invoke: Any,
    ) -> None:
        intercepted.append(plugin_name)
        await invoke()

    saved_gate = get_plugin_runtime_gate()
    set_plugin_runtime_gate(fake_gate)
    try:
        await events.redispatch(
            db_session,
            "test_event_point",
            {"company_id": str(uuid.uuid4())},
        )
        assert intercepted == ["gated_event_plugin"]
    finally:
        set_plugin_runtime_gate(saved_gate)
        events._schemas.clear()
        events._schemas.update(saved_schemas)
        events._handlers.clear()
        events._handlers.update(saved_handlers)
