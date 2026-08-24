"""RBAC authorization primitives (ADR-009 / WEEK9 Slice A).

Mirrors `app.core.tenancy`'s shape deliberately (same `ContextVar` +
bind/reset/context-manager pattern), for the identical reason: this is the
one narrow, deliberate exception to "core is primitives only," alongside
`tenancy.py`'s precedent.

Two pieces:

1. `ActorContext` / the `ContextVar` it's bound through / `require_permission()`
   — the authorization **port** (ADR-009 Decision 4). There is exactly one
   producer of `ActorContext` (`app.platform.permissions`'s resolution
   service) and exactly one binding point per entry path (`Authentication
   Middleware` for HTTP requests; a purpose-specific actor bound directly by
   a CLI script for non-HTTP paths) — this is deliberately NOT a
   `Protocol`/ABC `platform.permissions` "implements"; there is nothing to
   swap yet. `app.core` never imports `app.platform` or any business
   module — this module only *consumes* an already-built `ActorContext`,
   it never resolves one itself.

2. `Public` / `Authenticated` / `RequiresPermission` — the FastAPI dependency factories a
   route attaches to declare its own classification (ADR-009 Decision 7).
   **Amended per the WEEK9 brief's round-1 Codex finding**: ADR-009's
   Action Item 4 originally placed these in `app.platform.permissions`,
   which is unimplementable against Decision 9's own import contract
   (`app.modules must not import app.platform` — a business-module router
   could never import them from there). They live here instead, since
   `app.core` is the port every business module IS allowed to depend on —
   see `docs/adr/ADR-009-platform-permissions.md`'s Action Item 4 for the
   dated correction note, and `docs/adr/WEEK9-platform-permissions-
   implementation-brief.md` for the full reasoning.

No `SYSTEM_ACTOR` constant lives here (ADR-009 Decision 4/10, explicit
prohibition) — purpose-specific actor constants (`DEMO_SEED_ACTOR`,
`REFERENCE_DATA_ADMIN_ACTOR`, `COMPANY_ADMIN_ACTOR`, ...) are defined where
they're used (Slices B/D/E), never centrally here, so no single constant can
become a de facto all-permissions bypass.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

from app.core.exceptions import ActionDeniedError


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Who is making the current request/call, and what they may do.

    `is_system_actor` is a marker for audit/logging purposes only — it is
    **not** an authorization bypass. `require_permission()` below always
    checks `permission_codes` membership regardless of this flag; a
    purpose-specific actor still only carries the narrow, enumerated
    permission set it actually needs (ADR-009 Decision 4's explicit
    rejection of a shared, all-permissions `SYSTEM_ACTOR`).
    """

    user_id: uuid.UUID
    permission_codes: frozenset[str]
    is_system_actor: bool = False


_current_actor: ContextVar[ActorContext | None] = ContextVar("current_actor", default=None)


def set_current_actor(ctx: ActorContext) -> Token[ActorContext | None]:
    """Bind `ctx` as the active actor for the current context (task).

    Returns a token that must be passed to `reset_current_actor` when the
    scope ends (e.g. in a `finally` block), mirroring `ContextVar.reset` —
    prefer the `actor_context(...)` context manager below over calling this
    directly.
    """
    return _current_actor.set(ctx)


def reset_current_actor(token: Token[ActorContext | None]) -> None:
    _current_actor.reset(token)


def current_actor_or_none() -> ActorContext | None:
    """Return the active `ActorContext`, or `None` if no context is bound."""
    return _current_actor.get()


def require_current_actor() -> ActorContext:
    """Return the active `ActorContext`, or raise ActionDeniedError if none is bound."""
    ctx = _current_actor.get()
    if ctx is None:
        raise ActionDeniedError(
            "No actor context is bound. Fail-closed per ADR-009 / app.core.authorization."
        )
    return ctx


def require_permission(action: str) -> None:
    """Raise `ActionDeniedError` unless the bound actor may perform `action`.

    Fail-closed per ADR-009 Decision 7: no bound context, or a bound
    context lacking `action` in its `permission_codes`, both raise —
    there is no silent "allow by default" path. Reads the ambient
    `ContextVar`; it does not take an `ActorContext` argument, so a caller
    must bind one first (via `actor_context(...)` below, or the request
    middleware in the HTTP path) before calling this.
    """
    ctx = _current_actor.get()
    if ctx is None:
        raise ActionDeniedError(
            f"No actor context is bound; cannot authorize {action!r}. "
            "Fail-closed per ADR-009 Decision 7 / app.core.authorization."
        )
    if action not in ctx.permission_codes:
        raise ActionDeniedError(f"Actor {ctx.user_id} lacks permission {action!r}.")


class _ActorContextManager:
    """Context manager / convenience wrapper around set/reset for scripts and tests.

    Usage:
        with actor_context(ctx):
            ...  # require_permission(...) inside here sees `ctx`
    """

    def __init__(self, ctx: ActorContext) -> None:
        self._ctx = ctx
        self._token: Token[ActorContext | None] | None = None

    def __enter__(self) -> ActorContext:
        self._token = set_current_actor(self._ctx)
        return self._ctx

    def __exit__(self, *exc_info: object) -> None:
        assert self._token is not None
        reset_current_actor(self._token)


# Lowercase alias, mirroring `app.core.tenancy.company_context`'s identical
# naming convention for the identical reason (ruff N801 exempted via this
# alias rather than a rule suppression).
actor_context = _ActorContextManager


# ---------------------------------------------------------------------------
# Route classification (ADR-009 Decision 7) — FastAPI dependency factories.
# ---------------------------------------------------------------------------
# Every route must carry exactly one of these three, attached via FastAPI's
# `dependencies=[Depends(...)]`. Slice D's `AuthenticationMiddleware` and
# startup route-classification check both detect which one a route carries
# by inspecting `route.dependencies` for one of `Public`, `Authenticated`, or
# `RequiresPermission` (see the WEEK9 brief's Decision 5 for the full
# route-matching mechanism) — that inspection logic is Slice D's job; this
# module only defines the three markers themselves.


class Public:
    """Marker dependency: this route requires no authentication at all.

    Reserve for the small, explicit ADR-009 §7.2 Q5 allowlist (e.g.
    `GET /uom`, `GET /currencies`) and `/auth/login` — never the default.
    """

    def __call__(self) -> None:
        return None


class Authenticated:
    """Dependency: require an authenticated actor without requiring a permission.

    This is a real fail-closed dependency. It is used for session-lifecycle
    operations whose authorization is established by the authenticated session
    and ownership checks rather than by the active company's permission set.
    """

    def __call__(self) -> None:
        require_current_actor()


class RequiresPermission:
    """Dependency: enforces `require_permission(code)` for this route.

    A real, executing check (ADR-009 Decision 7a) — not documentation.
    Reads the `ActorContext` `AuthenticationMiddleware` already bound for
    this request before any route handler runs.
    """

    def __init__(self, code: str) -> None:
        self.code = code

    def __call__(self) -> None:
        require_permission(self.code)
