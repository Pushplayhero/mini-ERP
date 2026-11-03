"""Deterministic plugin loader and dependency resolution — ADR-010 Decisions 1 & 2.

Handles:
1. Discovery from built-in lists and installed Python packages via
   `importlib.metadata.entry_points(group="mini_erp.plugins")`.
2. SemVer Caret-compatibility verification against registered core hook points and
   inter-plugin dependencies (`manifest.requires`).
3. Domain event subscription verification against `app.core.events.registered_event_types()`.
4. Startup Inductive Invariant A: Upward-closed `default_enabled` (if B is default_enabled,
   all its prerequisites must be default_enabled).
5. Kahn topological sort with lexicographical min-heap tie-breaking on `manifest.name`,
   guaranteeing 100% deterministic, cycle-free loading order across all platforms.
6. Safe, idempotent registration of plugin handlers into `app.core.hooks` and
   `app.core.events`.
"""

from __future__ import annotations

import heapq
import importlib.metadata
import logging
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import ModuleType
from typing import Any

from app.core import events, hooks
from app.core.plugins import (
    PluginLoadError,
    PluginManifest,
    is_semver_compatible,
)

logger = logging.getLogger(__name__)

ENTRYPOINT_GROUP = "mini_erp.plugins"

_loaded_plugins: dict[str, PluginManifest] = {}


def get_loaded_plugins() -> tuple[PluginManifest, ...]:
    """Return all currently loaded plugins in deterministic topological order."""
    return tuple(_loaded_plugins.values())


def get_loaded_plugin(name: str) -> PluginManifest | None:
    """Return the loaded plugin manifest with `name`, or None."""
    return _loaded_plugins.get(name)


def reset_loaded_plugins() -> None:
    """Unregister all loaded plugin handlers and clear the loaded plugins registry."""
    for old_manifest in _loaded_plugins.values():
        for hb in old_manifest.hooks:
            hooks.unregister(hb.hook_name, hb.handler)
        for eb in old_manifest.events:
            events.unregister(eb.event_type, eb.handler)
    _loaded_plugins.clear()


def discover_entrypoint_manifests(*, group: str = ENTRYPOINT_GROUP) -> list[PluginManifest]:
    """Discover plugin manifests from installed Python package entry points.

    Sorted by entry point name for determinism.
    """
    manifests: list[PluginManifest] = []
    try:
        eps = importlib.metadata.entry_points(group=group)
    except Exception as exc:
        raise PluginLoadError(f"Failed to scan entry points for group {group!r}: {exc}") from exc

    sorted_eps = sorted(eps, key=lambda ep: ep.name)

    for ep in sorted_eps:
        try:
            target = ep.load()
        except Exception as exc:
            raise PluginLoadError(
                f"Failed to load plugin entry point {ep.name!r} ({ep.value}): {exc}"
            ) from exc

        manifest: PluginManifest | None = None
        if isinstance(target, PluginManifest):
            manifest = target
        elif isinstance(target, ModuleType) and hasattr(target, "PLUGIN_MANIFEST"):
            attr = target.PLUGIN_MANIFEST
            if isinstance(attr, PluginManifest):
                manifest = attr
            else:
                raise PluginLoadError(
                    f"Entry point {ep.name!r} module has PLUGIN_MANIFEST of invalid type "
                    f"{type(attr)}"
                )
        elif callable(target):
            try:
                result = target()
            except Exception as exc:
                raise PluginLoadError(
                    f"Entry point {ep.name!r} callable raised an exception: {exc}"
                ) from exc
            if isinstance(result, PluginManifest):
                manifest = result
            else:
                raise PluginLoadError(
                    f"Entry point {ep.name!r} callable returned invalid type {type(result)}"
                )
        else:
            raise PluginLoadError(
                f"Entry point {ep.name!r} loaded unsupported object of type {type(target)}"
            )

        manifests.append(manifest)

    return manifests


def _topological_sort_manifests(
    active_manifests: Sequence[PluginManifest],
    active_by_name: Mapping[str, PluginManifest],
) -> list[PluginManifest]:
    """Kahn's algorithm with min-heap lexicographical tie-breaking."""
    dependents: dict[str, list[str]] = {m.name: [] for m in active_manifests}
    in_degree: dict[str, int] = {m.name: 0 for m in active_manifests}

    for m in active_manifests:
        for dep in m.requires:
            dependents[dep.plugin_name].append(m.name)
            in_degree[m.name] += 1

    heap: list[str] = [name for name, deg in in_degree.items() if deg == 0]
    heapq.heapify(heap)

    ordered: list[PluginManifest] = []

    while heap:
        node_name = heapq.heappop(heap)
        ordered.append(active_by_name[node_name])

        for nxt in dependents[node_name]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                heapq.heappush(heap, nxt)

    if len(ordered) < len(active_manifests):
        cycle_candidates = [name for name, deg in in_degree.items() if deg > 0]
        raise PluginLoadError(
            f"Dependency cycle detected among plugins: {sorted(cycle_candidates)}"
        )

    return ordered


def resolve_and_validate_plugins(
    manifests: Sequence[PluginManifest],
    *,
    hook_specs: Mapping[str, str] | None = None,
    event_types: AbstractSet[str] | None = None,
    disabled_plugins: Iterable[str] = (),
) -> list[PluginManifest]:
    """Validate, filter, and topologically sort plugin manifests.

    Raises `PluginLoadError` fail-closed if:
    - Multiple manifests share the same name.
    - Two manifests share the exact same handler function object.
    - Any hook point is undefined or required_version is incompatible.
    - Any event_type is not registered in `app.core.events`.
    - Any dependency in `manifest.requires` is missing, disabled, or SemVer incompatible.
    - Inductive Invariant A is violated: a default-enabled plugin depends on a disabled
      or non-default-enabled plugin.
    - A dependency cycle exists.
    """
    if hook_specs is None:
        hook_specs = hooks.defined_hook_points()
    if event_types is None:
        event_types = events.registered_event_types()

    disabled_set = set(disabled_plugins)

    # 1. Check for duplicate names in input
    seen_all_names: set[str] = set()
    for m in manifests:
        if m.name in seen_all_names:
            raise PluginLoadError(f"Duplicate plugin name declared in discovery: {m.name!r}")
        seen_all_names.add(m.name)

    # 2. Filter out disabled plugins
    active_manifests = [m for m in manifests if m.name not in disabled_set]
    active_by_name: dict[str, PluginManifest] = {m.name: m for m in active_manifests}

    # 3. Check handler identity uniqueness across active plugins and __plugin_name__ integrity
    seen_handlers: dict[int, tuple[str, Any]] = {}
    for m in active_manifests:
        all_handlers = [hb.handler for hb in m.hooks] + [eb.handler for eb in m.events]
        for h in all_handlers:
            stamped = getattr(h, "__plugin_name__", None)
            if stamped != m.name:
                raise PluginLoadError(
                    f"Handler {h!r} in plugin {m.name!r} has mismatched or missing "
                    f"__plugin_name__={stamped!r}"
                )
            h_id = id(h)
            if h_id in seen_handlers:
                prev_plugin, _ = seen_handlers[h_id]
                if prev_plugin != m.name:
                    raise PluginLoadError(
                        f"Handler {h!r} is declared by both plugin {prev_plugin!r} and {m.name!r}"
                    )
            else:
                seen_handlers[h_id] = (m.name, h)

    # 4. Verify hook and event specifications
    for m in active_manifests:
        for hb in m.hooks:
            actual_version = hook_specs.get(hb.hook_name)
            if actual_version is None:
                raise PluginLoadError(
                    f"Plugin {m.name!r} binds to undefined hook point {hb.hook_name!r}"
                )
            if not is_semver_compatible(actual_version, hb.required_version):
                raise PluginLoadError(
                    f"Plugin {m.name!r} requires hook point {hb.hook_name!r} "
                    f"version {hb.required_version}, but core defines incompatible "
                    f"version {actual_version}"
                )

        for eb in m.events:
            if eb.event_type not in event_types:
                raise PluginLoadError(
                    f"Plugin {m.name!r} subscribes to unregistered event type {eb.event_type!r}"
                )

    # 5. Dependency existence, SemVer compatibility, and Inductive Invariant A
    for m in active_manifests:
        for dep in m.requires:
            if dep.plugin_name not in active_by_name:
                reason = (
                    "was disabled by configuration"
                    if dep.plugin_name in disabled_set
                    else "was not discovered"
                )
                raise PluginLoadError(
                    f"Plugin {m.name!r} requires plugin {dep.plugin_name!r}, which {reason}"
                )
            target = active_by_name[dep.plugin_name]
            if not is_semver_compatible(target.version, dep.min_version):
                raise PluginLoadError(
                    f"Plugin {m.name!r} requires plugin {dep.plugin_name!r} >= {dep.min_version}, "
                    f"but discovered version is {target.version}"
                )

            # Invariant A: Upward-closed default_enabled
            if m.default_enabled and not target.default_enabled:
                raise PluginLoadError(
                    f"Plugin {m.name!r} has default_enabled=True but depends on "
                    f"plugin {target.name!r} which has default_enabled=False "
                    "(violates Inductive Invariant A)"
                )

    return _topological_sort_manifests(active_manifests, active_by_name)


def bootstrap_plugins(
    builtin_manifests: Sequence[PluginManifest] = (),
    *,
    enable_entrypoints: bool | None = None,
    disabled_plugins: Iterable[str] | None = None,
) -> tuple[PluginManifest, ...]:
    """Bootstrap, validate, and wire plugins into hooks and event registries.

    Idempotent: unregisters any previously loaded plugin handlers before
    registering the resolved order.
    """
    from app.core.settings import get_settings

    settings = get_settings()

    if enable_entrypoints is None:
        enable_entrypoints = settings.enable_plugin_entrypoints
    if disabled_plugins is None:
        disabled_plugins = settings.disabled_plugins

    manifests: list[PluginManifest] = list(builtin_manifests)

    if enable_entrypoints:
        ep_manifests = discover_entrypoint_manifests()
        manifests.extend(ep_manifests)

    ordered = resolve_and_validate_plugins(
        manifests,
        disabled_plugins=disabled_plugins,
    )

    # Clean up previous registrations to remain strictly idempotent
    reset_loaded_plugins()

    # Register in topological order (defensively unregister first in case a handler was
    # registered outside bootstrap_plugins prior to this call)
    for manifest in ordered:
        _loaded_plugins[manifest.name] = manifest
        for hb in manifest.hooks:
            hooks.unregister(hb.hook_name, hb.handler)
            hooks.register(hb.hook_name, hb.handler)
        for eb in manifest.events:
            events.unregister(eb.event_type, eb.handler)
            events.subscribe(eb.event_type, eb.handler)

    return tuple(ordered)
