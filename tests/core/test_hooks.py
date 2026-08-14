"""tests/core/test_hooks.py — `app.core.hooks` (ADR-006 Decision 1 + R3).

Each test uses its own uniquely-named `hooks_test.*` hook point, mirroring
`tests/core/test_events.py`'s convention, so tests never collide with each
other. The `reset()`/`unregister()`-exercising tests additionally need to
not leak into the *real* registration `app.main` makes once at import time
(`SALES_ORDER_VALIDATE_CONFIRM -> credit_limit.check_credit_limit`) — the
autouse fixture below snapshots and restores the whole registry around
every test in this file so that never happens, regardless of what any test
here does to it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks


@pytest.fixture(autouse=True)
def _isolated_hook_registry():
    """Snapshot + restore `app.core.hooks._handlers` around every test here.

    Necessary because this file explicitly exercises `reset()` (which wipes
    *every* hook registration process-wide, including `app.main`'s real
    `SALES_ORDER_VALIDATE_CONFIRM -> credit_limit.check_credit_limit`
    registration made once at import time). Without this, a `reset()` test
    here would permanently disable the credit-limit plugin for every test
    that runs afterward in the same pytest session — `tests/sales/*` relies
    on that module-level wiring having run exactly once and staying intact.
    """
    saved = {name: list(fns) for name, fns in hooks._handlers.items()}
    yield
    hooks._handlers.clear()
    hooks._handlers.update(saved)


def _ctx(**overrides: object) -> hooks.HookContext:
    defaults: dict[str, object] = {
        "company_id": uuid.uuid4(),
        "order_id": uuid.uuid4(),
        "order_no": "SO-2026-000001",
        "customer_id": uuid.uuid4(),
        "total": Decimal("100"),
    }
    defaults.update(overrides)
    return hooks.HookContext(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_calls_registered_handler_with_context(db_session: AsyncSession) -> None:
    calls: list[hooks.HookContext] = []

    async def handler(session: AsyncSession, context: hooks.HookContext) -> None:
        assert session is db_session
        calls.append(context)

    hooks.register("hooks_test.basic", handler)
    context = _ctx()

    await hooks.run("hooks_test.basic", db_session, context)

    assert calls == [context]


@pytest.mark.asyncio
async def test_run_calls_multiple_handlers_in_registration_order(db_session: AsyncSession) -> None:
    order: list[str] = []

    async def first(_session: AsyncSession, _context: hooks.HookContext) -> None:
        order.append("first")

    async def second(_session: AsyncSession, _context: hooks.HookContext) -> None:
        order.append("second")

    hooks.register("hooks_test.ordering", first)
    hooks.register("hooks_test.ordering", second)

    await hooks.run("hooks_test.ordering", db_session, _ctx())

    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_run_propagates_handler_exception(db_session: AsyncSession) -> None:
    async def boom(_session: AsyncSession, _context: hooks.HookContext) -> None:
        raise RuntimeError("handler exploded")

    hooks.register("hooks_test.fails", boom)

    with pytest.raises(RuntimeError, match="handler exploded"):
        await hooks.run("hooks_test.fails", db_session, _ctx())


@pytest.mark.asyncio
async def test_a_later_handler_does_not_run_after_an_earlier_one_raises(
    db_session: AsyncSession,
) -> None:
    calls: list[str] = []

    async def boom(_session: AsyncSession, _context: hooks.HookContext) -> None:
        calls.append("boom")
        raise RuntimeError("stop here")

    async def never(_session: AsyncSession, _context: hooks.HookContext) -> None:
        calls.append("never")

    hooks.register("hooks_test.short_circuit", boom)
    hooks.register("hooks_test.short_circuit", never)

    with pytest.raises(RuntimeError, match="stop here"):
        await hooks.run("hooks_test.short_circuit", db_session, _ctx())

    assert calls == ["boom"]


@pytest.mark.asyncio
async def test_unregister_stops_handler_from_being_called(db_session: AsyncSession) -> None:
    calls: list[str] = []

    async def handler(_session: AsyncSession, _context: hooks.HookContext) -> None:
        calls.append("called")

    hooks.register("hooks_test.unreg", handler)
    hooks.unregister("hooks_test.unreg", handler)

    await hooks.run("hooks_test.unreg", db_session, _ctx())

    assert calls == []


@pytest.mark.asyncio
async def test_unregister_removes_every_occurrence_of_a_duplicate_registration(
    db_session: AsyncSession,
) -> None:
    """Diff-review regression: `unregister` used to call `list.remove()`,
    which only strips the *first* match. If `handler` had somehow been
    registered twice for the same hook point, one `unregister` call left it
    still active — contradicting `unregister`'s own documented "this is not
    listening" postcondition.
    """
    calls: list[str] = []

    async def handler(_session: AsyncSession, _context: hooks.HookContext) -> None:
        calls.append("called")

    hooks.register("hooks_test.dup_unreg", handler)
    hooks.register("hooks_test.dup_unreg", handler)
    hooks.unregister("hooks_test.dup_unreg", handler)

    await hooks.run("hooks_test.dup_unreg", db_session, _ctx())

    assert calls == []


def test_unregister_of_a_handler_never_registered_is_a_silent_no_op() -> None:
    async def never_registered(_session: AsyncSession, _context: hooks.HookContext) -> None:
        pass

    # Unknown hook_name entirely.
    hooks.unregister("hooks_test.unknown_hook_point", never_registered)

    # Known hook_name, but this particular handler was never added to it.
    hooks.register("hooks_test.discard_semantics", never_registered)
    hooks.unregister("hooks_test.discard_semantics", never_registered)
    # A second unregister of the same (now-removed) handler must also
    # silently no-op, not raise.
    hooks.unregister("hooks_test.discard_semantics", never_registered)


@pytest.mark.asyncio
async def test_reset_clears_all_registrations(db_session: AsyncSession) -> None:
    calls: list[str] = []

    async def handler(_session: AsyncSession, _context: hooks.HookContext) -> None:
        calls.append("called")

    hooks.register("hooks_test.reset_a", handler)
    hooks.register("hooks_test.reset_b", handler)

    hooks.reset()

    await hooks.run("hooks_test.reset_a", db_session, _ctx())
    await hooks.run("hooks_test.reset_b", db_session, _ctx())

    assert calls == []


@pytest.mark.asyncio
async def test_run_with_no_handlers_registered_is_a_no_op(db_session: AsyncSession) -> None:
    # Must not raise, must not require any prior registration for this
    # hook_name — sales must be able to `hooks.run(...)` this hook point
    # even when zero plugins are listening.
    await hooks.run("hooks_test.nothing_ever_registered_here", db_session, _ctx())
