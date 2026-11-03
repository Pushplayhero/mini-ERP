"""Core plugin primitives and declaration contracts — ADR-010.

Mirrors `app.core.authorization`'s architectural shape deliberately: pure data
structures (`PluginManifest`, `HookBinding`, `EventBinding`, `PluginDependency`),
immutable validation, SemVer compatibility logic, and the execution-time
`PluginRuntimeGate` Port + `plugin_config_context`.

Dependency inversion rule:
- `app.core` and `app.plugins` must NEVER import `app.platform` (CI-enforced by
  import-linter).
- `PluginManifest` lives here in `app.core.plugins` so that future and built-in
  plugins (`app.plugins.*`) only depend on `app.core`, while the composition root
  (`app.main`) wires the actual database-backed runtime gate implementation from
  `app.platform.plugins.service`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError

if TYPE_CHECKING:
    from app.core.events import EventHandler
    from app.core.hooks import HookHandler

_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PLUGIN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,99}$")

TConfig = TypeVar("TConfig", bound=BaseModel)


class PluginLoadError(AppError):
    """Raised when plugin discovery, SemVer validation, or sorting fails."""


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a strict SemVer string 'X.Y.Z' into non-negative integers.

    Raises `PluginLoadError` if version does not strictly match MAJOR.MINOR.PATCH.
    """
    match = _SEMVER_PATTERN.match(version)
    if not match:
        raise PluginLoadError(f"version {version!r} is not a valid strict SemVer 'X.Y.Z' string")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_semver_compatible(actual: str, required: str) -> bool:
    """Evaluate SemVer Caret (^) compatibility between an actual and required version.

    Rules per ADR-010 Decision 2:
    - For actual.major >= 1: compatible iff actual.major == required.major and
      (actual.minor, actual.patch) >= (required.minor, required.patch).
    - For actual.major == 0: compatible iff actual.major == 0 and required.major == 0 and
      actual.minor == required.minor and actual.patch >= required.patch.
    """
    act_major, act_minor, act_patch = parse_semver(actual)
    req_major, req_minor, req_patch = parse_semver(required)

    if act_major >= 1:
        return act_major == req_major and (act_minor, act_patch) >= (req_minor, req_patch)

    # act_major == 0
    return req_major == 0 and act_minor == req_minor and act_patch >= req_patch


@dataclass(frozen=True, slots=True)
class PluginDependency:
    """Inter-plugin dependency declaration."""

    plugin_name: str
    min_version: str


@dataclass(frozen=True, slots=True)
class HookBinding:
    """Binding of a hook handler to a versioned hook point."""

    hook_name: str
    required_version: str
    handler: HookHandler


@dataclass(frozen=True, slots=True)
class EventBinding:
    """Binding of an event handler to a registered event type."""

    event_type: str
    handler: EventHandler


@dataclass(frozen=True)
class PluginManifest:
    """Declarative specification of a plugin."""

    name: str
    version: str
    title: str
    description: str
    default_enabled: bool = False
    requires: tuple[PluginDependency, ...] = ()
    config_schema: type[BaseModel] | None = None
    hooks: tuple[HookBinding, ...] = ()
    events: tuple[EventBinding, ...] = ()

    def __post_init__(self) -> None:
        if not _PLUGIN_NAME_PATTERN.match(self.name):
            raise PluginLoadError(
                f"plugin name {self.name!r} must match pattern ^[a-z][a-z0-9_]{{1,99}}$"
            )

        parse_semver(self.version)

        if not self.title.strip():
            raise PluginLoadError(f"plugin {self.name!r} title cannot be blank")

        # Validate requires
        seen_deps: set[str] = set()
        for dep in self.requires:
            if not _PLUGIN_NAME_PATTERN.match(dep.plugin_name):
                raise PluginLoadError(
                    f"plugin {self.name!r} depends on invalid plugin name {dep.plugin_name!r}"
                )
            if dep.plugin_name == self.name:
                raise PluginLoadError(f"plugin {self.name!r} cannot depend on itself")
            if dep.plugin_name in seen_deps:
                raise PluginLoadError(
                    f"plugin {self.name!r} declares duplicate dependency {dep.plugin_name!r}"
                )
            seen_deps.add(dep.plugin_name)
            parse_semver(dep.min_version)

        # Validate duplicate bindings in this manifest
        seen_hooks: set[tuple[str, Any]] = set()
        hook_handler_ids: set[int] = set()
        for hb in self.hooks:
            parse_semver(hb.required_version)
            key = (hb.hook_name, hb.handler)
            if key in seen_hooks:
                raise PluginLoadError(
                    f"plugin {self.name!r} declares duplicate hook binding for {hb.hook_name!r}"
                )
            seen_hooks.add(key)
            hook_handler_ids.add(id(hb.handler))

        seen_events: set[tuple[str, Any]] = set()
        for eb in self.events:
            if id(eb.handler) in hook_handler_ids:
                raise PluginLoadError(
                    f"plugin {self.name!r} cannot reuse the same handler {eb.handler!r} "
                    "across both hooks and events"
                )
            key_eb = (eb.event_type, eb.handler)
            if key_eb in seen_events:
                raise PluginLoadError(
                    f"plugin {self.name!r} declares duplicate event binding for {eb.event_type!r}"
                )
            seen_events.add(key_eb)

        # Inductive Invariant B (ADR-010 Decision 2 & R5):
        # If config_schema is provided, verify extra='forbid' and model_validate({}) succeeds.
        if self.config_schema is not None:
            if not (
                isinstance(self.config_schema, type) and issubclass(self.config_schema, BaseModel)
            ):
                raise PluginLoadError(
                    f"plugin {self.name!r} config_schema must be a BaseModel subclass"
                )
            extra_setting = self.config_schema.model_config.get("extra")
            if extra_setting != "forbid":
                raise PluginLoadError(
                    f"plugin {self.name!r} config_schema must configure model_config extra='forbid'"
                )
            try:
                self.config_schema.model_validate({})
            except Exception as exc:
                raise PluginLoadError(
                    f"plugin {self.name!r} config_schema failed default model_validate({{}}): {exc}"
                ) from exc

        # Safe __plugin_name__ stamping (ADR-010 R4):
        # First verify no handler is already bound to another plugin.
        all_handlers: list[Any] = [hb.handler for hb in self.hooks] + [
            eb.handler for eb in self.events
        ]
        for handler in all_handlers:
            existing = getattr(handler, "__plugin_name__", None)
            if existing is not None and existing != self.name:
                raise PluginLoadError(
                    f"handler {handler!r} is already bound to plugin {existing!r} "
                    f"and cannot be shared with plugin {self.name!r}"
                )

        # All checks passed, stamp __plugin_name__
        for handler in all_handlers:
            handler.__plugin_name__ = self.name

    @property
    def default_config_dict(self) -> dict[str, Any]:
        """Return canonical JSON-compatible default configuration dictionary."""
        if self.config_schema is None:
            return {}
        return self.config_schema.model_validate({}).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Execution-time context and runtime gate port
# ---------------------------------------------------------------------------

PluginRuntimeGate = Callable[
    [AsyncSession, str, Callable[[], Awaitable[None]]],
    Awaitable[None],
]

_plugin_runtime_gate: PluginRuntimeGate | None = None
_current_plugin_config: ContextVar[BaseModel | None] = ContextVar(
    "current_plugin_config", default=None
)


def set_plugin_runtime_gate(gate: PluginRuntimeGate | None) -> None:
    """Wire or reset the process-wide plugin runtime gate implementation."""
    global _plugin_runtime_gate
    _plugin_runtime_gate = gate


def get_plugin_runtime_gate() -> PluginRuntimeGate | None:
    """Return the currently wired plugin runtime gate, or None."""
    return _plugin_runtime_gate


def set_current_plugin_config(config: BaseModel | None) -> Token[BaseModel | None]:
    """Bind the validated plugin configuration model for the current task/context."""
    return _current_plugin_config.set(config)


def reset_current_plugin_config(token: Token[BaseModel | None]) -> None:
    """Reset the plugin configuration context to its previous token value."""
    _current_plugin_config.reset(token)


def current_plugin_config_or_none() -> BaseModel | None:
    """Return the active plugin config model, or None if unbound."""
    return _current_plugin_config.get()


def get_current_plugin_config(schema: type[TConfig]) -> TConfig:
    """Return the active plugin configuration as an instance of `schema`.

    If no plugin config is currently bound (e.g. in standalone unit tests that
    directly invoke a plugin handler without the runtime gate), falls back to
    instantiating `schema.model_validate({})`.

    Raises `PluginLoadError` if the bound configuration is not an instance of `schema`.
    """
    ctx = _current_plugin_config.get()
    if ctx is None:
        return schema.model_validate({})
    if not isinstance(ctx, schema):
        raise PluginLoadError(
            f"Expected plugin config schema {schema.__name__}, got {type(ctx).__name__}"
        )
    return ctx


class _PluginConfigContextManager:
    """Context manager for binding `_current_plugin_config` within a block."""

    def __init__(self, config: BaseModel | None) -> None:
        self._config = config
        self._token: Token[BaseModel | None] | None = None

    def __enter__(self) -> BaseModel | None:
        self._token = set_current_plugin_config(self._config)
        return self._config

    def __exit__(self, *exc_info: object) -> None:
        assert self._token is not None
        reset_current_plugin_config(self._token)


plugin_config_context = _PluginConfigContextManager
