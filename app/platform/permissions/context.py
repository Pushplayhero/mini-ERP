"""`current_session_id` — which `sessions` row the current request/call is acting through.

WEEK9 brief round-3 finding 2: `app.core.authorization.ActorContext`
deliberately carries only `user_id`/`permission_codes`/`is_system_actor` —
which session a request came from is not an authorization-relevant fact and
doesn't belong on the `app.core` port. `switch_company()`/`logout()` still
need to know *which* `sessions` row to update/revoke, though — this module
is where that `platform.permissions`-internal fact lives, adding no new
port surface.

Mirrors `app.core.tenancy`/`app.core.authorization`'s identical `ContextVar`
+ bind/reset + context-manager shape.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_current_session_id: ContextVar[uuid.UUID | None] = ContextVar("current_session_id", default=None)


def set_current_session_id(session_id: uuid.UUID) -> Token[uuid.UUID | None]:
    return _current_session_id.set(session_id)


def reset_current_session_id(token: Token[uuid.UUID | None]) -> None:
    _current_session_id.reset(token)


def current_session_id_or_none() -> uuid.UUID | None:
    return _current_session_id.get()


class _SessionContextManager:
    """Context manager / convenience wrapper around set/reset for scripts and tests.

    Usage:
        with session_context(session_id):
            ...
    """

    def __init__(self, session_id: uuid.UUID) -> None:
        self._session_id = session_id
        self._token: Token[uuid.UUID | None] | None = None

    def __enter__(self) -> uuid.UUID:
        self._token = set_current_session_id(self._session_id)
        return self._session_id

    def __exit__(self, *exc_info: object) -> None:
        assert self._token is not None
        reset_current_session_id(self._token)


# Lowercase alias, mirroring `app.core.tenancy.company_context`/
# `app.core.authorization.actor_context`'s identical naming convention.
session_context = _SessionContextManager
