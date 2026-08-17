"""tests/core/test_authorization.py — `app.core.authorization` (ADR-009 / WEEK9 Slice A).

Covers the WEEK9 brief's Slice A test list: `ActorContext` `ContextVar`
bind/reset round-trips, `require_permission()`'s two fail-closed paths (no
context bound; insufficient permission), and migration 0009's placeholder
seed catalog (`admin` role granted `session.manage`) resolving correctly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import String, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import (
    ActorContext,
    actor_context,
    current_actor_or_none,
    require_permission,
    reset_current_actor,
    set_current_actor,
)
from app.core.exceptions import ActionDeniedError


def _ctx(
    *, permission_codes: frozenset[str] = frozenset(), is_system_actor: bool = False
) -> ActorContext:
    return ActorContext(
        user_id=uuid.uuid4(), permission_codes=permission_codes, is_system_actor=is_system_actor
    )


# ---------------------------------------------------------------------------
# ContextVar bind/reset
# ---------------------------------------------------------------------------


def test_no_context_bound_by_default() -> None:
    assert current_actor_or_none() is None


def test_set_and_reset_round_trip() -> None:
    ctx = _ctx(permission_codes=frozenset({"sales.order.create"}))
    token = set_current_actor(ctx)
    try:
        assert current_actor_or_none() is ctx
    finally:
        reset_current_actor(token)
    assert current_actor_or_none() is None


def test_actor_context_manager_binds_and_unbinds() -> None:
    ctx = _ctx(permission_codes=frozenset({"sales.order.create"}))
    assert current_actor_or_none() is None
    with actor_context(ctx) as bound:
        assert bound is ctx
        assert current_actor_or_none() is ctx
    assert current_actor_or_none() is None


def test_actor_context_manager_unbinds_on_exception() -> None:
    ctx = _ctx(permission_codes=frozenset())
    with pytest.raises(RuntimeError), actor_context(ctx):
        assert current_actor_or_none() is ctx
        raise RuntimeError("boom")
    assert current_actor_or_none() is None


# ---------------------------------------------------------------------------
# require_permission() — both fail-closed paths
# ---------------------------------------------------------------------------


def test_require_permission_raises_with_no_context_bound() -> None:
    assert current_actor_or_none() is None
    with pytest.raises(ActionDeniedError):
        require_permission("sales.order.create")


def test_require_permission_raises_with_insufficient_permission() -> None:
    ctx = _ctx(permission_codes=frozenset({"sales.order.read"}))
    with actor_context(ctx), pytest.raises(ActionDeniedError):
        require_permission("sales.order.create")


def test_require_permission_succeeds_with_matching_permission() -> None:
    ctx = _ctx(permission_codes=frozenset({"sales.order.create"}))
    with actor_context(ctx):
        require_permission("sales.order.create")  # must not raise


def test_is_system_actor_is_not_an_implicit_bypass() -> None:
    """ADR-009 Decision 4: `is_system_actor` is an audit marker, never a bypass."""
    ctx = _ctx(permission_codes=frozenset(), is_system_actor=True)
    with actor_context(ctx), pytest.raises(ActionDeniedError):
        require_permission("sales.order.create")


# ---------------------------------------------------------------------------
# Migration 0009's placeholder seed catalog
# ---------------------------------------------------------------------------


# Raw Core table references (never the ORM model — `platform.permissions`'s
# models don't exist until Slice B; migration 0009 already created these
# tables, and this test only needs to read them), mirroring the same
# pattern `ledger.service.get_trial_balance` uses to read `masterdata`'s
# `accounts` table without importing it.
_role_permissions = table(
    "role_permissions",
    column("role_code", String),
    column("permission_code", String),
)


@pytest.mark.asyncio
async def test_admin_role_is_granted_session_manage(db_session: AsyncSession) -> None:
    """WEEK9 brief Decision 2: 0009 must seed `admin` -> `session.manage`
    directly (not deferred to migration 0010) so the first bootstrap admin
    (Slice B) can call `/auth/switch-company`/`/auth/logout` immediately
    after Slice D's cutover.
    """
    result = await db_session.execute(
        select(_role_permissions.c.permission_code).where(_role_permissions.c.role_code == "admin")
    )
    granted = {row[0] for row in result.all()}
    assert "session.manage" in granted
