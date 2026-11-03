"""tests/platform/test_plugin_loader.py — Plugin loader & topological resolution (ADR-010).

Validates:
1. SemVer Caret compatibility checking against defined hook points.
2. Domain event type registration checks.
3. Kahn's topological sort with lexicographical min-heap tie-breaking.
4. Cycle detection (circular dependencies fail closed).
5. Invariant A (upward-closed default_enabled).
6. Operator overrides (disabled_plugins and enable_plugin_entrypoints).
7. Entry point discovery mechanics.
8. bootstrap_plugins idempotency and registration wiring.
"""

from __future__ import annotations

import uuid
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events, hooks
from app.core.hooks import HookContext
from app.core.plugins import (
    EventBinding,
    HookBinding,
    PluginDependency,
    PluginLoadError,
    PluginManifest,
)
from app.platform.plugins import loader


class _DummyPayload(BaseModel):
    company_id: uuid.UUID


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


def _make_manifest(
    name: str,
    *,
    version: str = "1.0.0",
    default_enabled: bool = False,
    requires: tuple[PluginDependency, ...] = (),
    hook_name: str | None = None,
    event_type: str | None = None,
) -> PluginManifest:
    async def h(_s: AsyncSession, _c: HookContext) -> None:
        pass

    async def eh(_s: AsyncSession, _p: BaseModel) -> None:
        pass

    hook_bindings: tuple[HookBinding, ...] = ()
    if hook_name:
        hook_bindings = (HookBinding(hook_name, "1.0.0", h),)

    event_bindings: tuple[EventBinding, ...] = ()
    if event_type:
        event_bindings = (EventBinding(event_type, eh),)

    return PluginManifest(
        name=name,
        version=version,
        title=f"Plugin {name}",
        description="test",
        default_enabled=default_enabled,
        requires=requires,
        hooks=hook_bindings,
        events=event_bindings,
    )


def test_topological_sort_deterministic_tie_breaking() -> None:
    # A, B, C have no dependencies -> must order lexicographically: a, b, c
    ma = _make_manifest("plugin_a")
    mb = _make_manifest("plugin_b")
    mc = _make_manifest("plugin_c")

    ordered = loader.resolve_and_validate_plugins([mc, ma, mb])
    assert [m.name for m in ordered] == ["plugin_a", "plugin_b", "plugin_c"]


def test_topological_sort_respects_prerequisites() -> None:
    # plugin_b requires plugin_a; plugin_c requires plugin_b
    ma = _make_manifest("plugin_a")
    mb = _make_manifest("plugin_b", requires=(PluginDependency("plugin_a", "1.0.0"),))
    mc = _make_manifest("plugin_c", requires=(PluginDependency("plugin_b", "1.0.0"),))

    ordered = loader.resolve_and_validate_plugins([mc, mb, ma])
    assert [m.name for m in ordered] == ["plugin_a", "plugin_b", "plugin_c"]


def test_topological_sort_detects_dependency_cycle() -> None:
    # a -> b -> a
    ma = _make_manifest("plugin_a", requires=(PluginDependency("plugin_b", "1.0.0"),))
    mb = _make_manifest("plugin_b", requires=(PluginDependency("plugin_a", "1.0.0"),))

    with pytest.raises(PluginLoadError, match="Dependency cycle detected"):
        loader.resolve_and_validate_plugins([ma, mb])


def test_hook_point_semver_compatibility_enforced() -> None:
    m = _make_manifest("plugin_h", hook_name="sales.order.validate_confirm")
    hook_specs = {"sales.order.validate_confirm": "2.0.0"}

    with pytest.raises(PluginLoadError, match="incompatible version"):
        loader.resolve_and_validate_plugins([m], hook_specs=hook_specs)


def test_undefined_hook_point_fails_closed() -> None:
    m = _make_manifest("plugin_h", hook_name="undefined.hook.point")
    with pytest.raises(PluginLoadError, match="binds to undefined hook point"):
        loader.resolve_and_validate_plugins([m], hook_specs={})


def test_unregistered_event_type_fails_closed() -> None:
    m = _make_manifest("plugin_e", event_type="unregistered.event")
    with pytest.raises(PluginLoadError, match="subscribes to unregistered event type"):
        loader.resolve_and_validate_plugins([m], event_types=frozenset())


def test_inductive_invariant_a_default_enabled_must_be_upward_closed() -> None:
    # mb is default_enabled=True, but ma is default_enabled=False -> violates Invariant A
    ma = _make_manifest("plugin_a", default_enabled=False)
    mb = _make_manifest(
        "plugin_b",
        default_enabled=True,
        requires=(PluginDependency("plugin_a", "1.0.0"),),
    )

    with pytest.raises(PluginLoadError, match="violates Inductive Invariant A"):
        loader.resolve_and_validate_plugins([ma, mb])


def test_disabled_plugins_operator_override() -> None:
    ma = _make_manifest("plugin_a")
    mb = _make_manifest("plugin_b")

    ordered = loader.resolve_and_validate_plugins(
        [ma, mb],
        disabled_plugins=["plugin_a"],
    )
    assert [m.name for m in ordered] == ["plugin_b"]


def test_disabled_plugin_breaks_dependents_fail_closed() -> None:
    ma = _make_manifest("plugin_a")
    mb = _make_manifest("plugin_b", requires=(PluginDependency("plugin_a", "1.0.0"),))

    with pytest.raises(PluginLoadError, match="disabled by configuration"):
        loader.resolve_and_validate_plugins(
            [ma, mb],
            disabled_plugins=["plugin_a"],
        )


def test_entrypoint_discovery_loading() -> None:
    mock_ep = MagicMock()
    mock_ep.name = "external_p"
    mock_ep.value = "some.pkg:PLUGIN_MANIFEST"
    sample = _make_manifest("external_p")
    mock_ep.load.return_value = sample

    with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
        discovered = loader.discover_entrypoint_manifests()
        assert len(discovered) == 1
        assert discovered[0].name == "external_p"


def test_bootstrap_plugins_wiring_and_idempotency() -> None:
    hooks.define_hook_point("hp_test", "1.0.0")
    events.register_event("ev_test", _DummyPayload)

    async def my_hook(_s: AsyncSession, _c: HookContext) -> None:
        pass

    async def my_event(_s: AsyncSession, _p: BaseModel) -> None:
        pass

    m = PluginManifest(
        name="boot_plugin",
        version="1.0.0",
        title="Boot",
        description="",
        hooks=(HookBinding("hp_test", "1.0.0", my_hook),),
        events=(EventBinding("ev_test", my_event),),
    )

    # First bootstrap
    ordered1 = loader.bootstrap_plugins([m], enable_entrypoints=False)
    assert len(ordered1) == 1
    assert my_hook in hooks._handlers["hp_test"]
    assert my_event in events._handlers["ev_test"]
    assert len(hooks._handlers["hp_test"]) == 1

    # Second bootstrap (idempotent: does not duplicate handlers)
    ordered2 = loader.bootstrap_plugins([m], enable_entrypoints=False)
    assert len(ordered2) == 1
    assert len(hooks._handlers["hp_test"]) == 1
    assert len(events._handlers["ev_test"]) == 1

    # Reset unregisters handlers cleanly
    loader.reset_loaded_plugins()
    assert my_hook not in hooks._handlers.get("hp_test", [])
    assert my_event not in events._handlers.get("ev_test", [])

    # Pre-registering a handler before bootstrap still does not duplicate
    hooks.register("hp_test", my_hook)
    ordered3 = loader.bootstrap_plugins([m], enable_entrypoints=False)
    assert len(ordered3) == 1
    assert len(hooks._handlers["hp_test"]) == 1


def test_intra_plugin_handler_reuse_and_tampering_checks() -> None:
    async def multi_event_handler(_s: AsyncSession, _p: BaseModel) -> None:
        pass

    # 1. Single plugin binding one handler to two distinct event types is valid
    m_multi = PluginManifest(
        name="multi_listener",
        version="1.0.0",
        title="Multi Listener",
        description="Subscribes one handler to two events",
        events=(
            EventBinding("ev_one", multi_event_handler),
            EventBinding("ev_two", multi_event_handler),
        ),
    )
    resolved = loader.resolve_and_validate_plugins(
        [m_multi],
        event_types=frozenset({"ev_one", "ev_two"}),
    )
    assert [m.name for m in resolved] == ["multi_listener"]

    # 2. Tampering with __plugin_name__ after manifest creation fails closed in loader
    cast(Any, multi_event_handler).__plugin_name__ = "tampered_name"
    with pytest.raises(PluginLoadError, match="mismatched or missing __plugin_name__"):
        loader.resolve_and_validate_plugins(
            [m_multi],
            event_types=frozenset({"ev_one", "ev_two"}),
        )
