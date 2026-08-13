"""Minimal hook registry — ADR-006 Decision 1 + Consensus Revision R3.

Mirrors `app.core.events`'s style deliberately (module-level private dict +
module-level functions, not a class instance) — same reasoning as that
module's docstring: `app.main` (and, per ADR-006, `app.plugins.*`) just does
`from app.core import hooks` and calls `hooks.register(...)`.

**Hooks vs. events — two mechanisms, kept conceptually distinct on purpose**
(ADR-006 Decision 1, Option B explicitly rejected): `app.core.events`
announces facts that already happened, durably (outboxed, replayable).
A hook point is the opposite kind of thing — a synchronous, in-process
veto/augment point invoked *before* a state transition is allowed to
happen, never durably recorded, never replayed. Reusing the event bus for
"sales.order.confirming" would put a maybe-never-happened veto question
into the same durable log as facts, which is meaningless (or actively
misleading) to a future replay/webhook consumer. So: events = "what
happened", hooks = "should this be allowed to happen" — two mechanisms,
each honest about what it is.

**Execution semantics**: handlers for a given `hook_name` run sequentially,
in registration order, inside the caller's existing transaction/session —
nothing here opens a savepoint or catches exceptions on the caller's
behalf. A handler exception propagates unchanged (fail-closed): the calling
service function's transaction is expected to abort, exactly like an
`app.core.events` subscriber exception aborts a `publish()` call. A hook
point with zero registered handlers is a **silent no-op** — the calling
module (`sales`, here) must never need to know or care whether anything is
listening; that indifference is the entire point of "customize without
forking the core" (see `app/plugins/README.md`).

**Trust model** (master-plan §10.3): hook handlers are admin-installed,
trusted, in-process Python code — no sandboxing, no resource limits, no
signature verification. A hook that wants to reject a business operation
must raise an `app.core.exceptions.AppError` subclass so the existing
exception handlers in `app.main` map it to a proper HTTP status; a plugin
raising a bare `Exception` is a plugin bug and surfaces as an honest 500.

`unregister`/`reset` ship from day one (ADR-006 R3) — an append-only
registry makes the "pull the plugin out and prove the decoupling" test
(the ADR's central demonstration) unwritable.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class HookContext:
    """The minimum a hook handler needs to veto or augment an in-flight operation.

    Deliberately a plain frozen dataclass, not a pydantic model: unlike an
    `app.core.events` payload, a `HookContext` never crosses a
    serialization boundary (it is never outboxed, replayed, or sent over
    HTTP) — it is built and consumed entirely in-process, within the same
    call stack, so pydantic's validation/serialization machinery would add
    nothing here. Frozen so a handler cannot mutate the context out from
    under later handlers in the same `run()` call — a handler that wants to
    affect the operation raises (veto) rather than silently edits shared
    state.

    Week 4 has exactly one hook point (`sales.order.validate_confirm`), so
    every field below is what that point's one consumer
    (`app.plugins.credit_limit.check_credit_limit`) needs: which company/
    order/customer this is, and the order total the credit check sums
    against. Future hook points are expected to define their own, more
    specific context types rather than keep widening this one — this shape
    is Week 4's, not a permanent "the" hook context for all future points.
    """

    company_id: uuid.UUID
    order_id: uuid.UUID
    order_no: str | None
    customer_id: uuid.UUID
    total: Decimal


HookHandler = Callable[[AsyncSession, HookContext], Awaitable[None]]

# ---------------------------------------------------------------------------
# Registry (module-level state, mirroring app.core.events's style)
# ---------------------------------------------------------------------------

_handlers: dict[str, list[HookHandler]] = {}


def register(hook_name: str, fn: HookHandler) -> None:
    """Register `fn` to run (in registration order) whenever `hook_name` fires."""
    _handlers.setdefault(hook_name, []).append(fn)


def unregister(hook_name: str, fn: HookHandler) -> None:
    """Remove `fn` from `hook_name`'s handler list (ADR-006 R3).

    Silently no-ops if `hook_name` has no registered handlers, or if `fn`
    was never registered for it — same "discard, don't punish an
    already-true postcondition" choice as `app.core.events.unregister`,
    documented once there.
    """
    handlers = _handlers.get(hook_name)
    if handlers is None:
        return
    with suppress(ValueError):
        handlers.remove(fn)


def reset() -> None:
    """Clear every registered handler for every hook point (test-only, ADR-006 R3).

    Not called by any application code path — `app.main` registers the
    credit-limit plugin once at import time and that registration is meant
    to live for the process's whole lifetime. Tests that call this must
    save/restore around it if other tests in the same session depend on a
    real registration surviving (see `tests/core/test_hooks.py`'s fixture).
    """
    _handlers.clear()


async def run(hook_name: str, session: AsyncSession, context: HookContext) -> None:
    """Call every handler registered for `hook_name`, in registration order.

    No handlers registered -> no-op (the calling module never needs to know
    whether anything is listening). Any handler exception propagates
    unchanged — fail-closed, no handler's exception is ever caught here on
    the caller's behalf.
    """
    for handler in _handlers.get(hook_name, []):
        await handler(session, context)
