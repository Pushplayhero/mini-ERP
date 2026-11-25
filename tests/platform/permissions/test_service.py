"""tests/platform/permissions/test_service.py — login/session flow (ADR-009 Decision 3/8).

Covers the WEEK9 brief's Slice B test list: login success/failure incl.
multi-company selection, Argon2id hash verification, session-token
SHA-256 lookup correctness.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.authorization import ActorContext, actor_context
from app.core.exceptions import ActionDeniedError
from app.platform.permissions import service
from app.platform.permissions.context import session_context
from app.platform.permissions.models import Session as SessionModel
from app.platform.permissions.models import User
from tests.platform.permissions._helpers import (
    create_company,
    create_test_user,
    create_user_with_role,
    grant_test_role,
)

# ---------------------------------------------------------------------------
# Argon2id hashing (ADR-009 Decision 2)
# ---------------------------------------------------------------------------


def test_hash_password_uses_argon2id() -> None:
    hashed = service.hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert service.verify_password("correct horse battery staple", hashed)
    assert not service.verify_password("wrong password", hashed)


# ---------------------------------------------------------------------------
# login()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_single_company(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "LOGIN1")
    await create_user_with_role(
        db_session, email="one@example.com", password="pw12345", company_id=company_id
    )

    result = await service.login(db_session, "one@example.com", "pw12345", None)
    assert result.active_company_id == company_id
    assert len(result.raw_token) > 20


@pytest.mark.asyncio
async def test_login_is_case_insensitive_on_email(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "LOGINCI")
    await create_user_with_role(
        db_session, email="Mixed@Example.com", password="pw12345", company_id=company_id
    )

    result = await service.login(db_session, "mixed@example.com", "pw12345", None)
    assert result.active_company_id == company_id


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "LOGIN2")
    await create_user_with_role(
        db_session, email="two@example.com", password="pw12345", company_id=company_id
    )

    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "two@example.com", "wrong", None)


@pytest.mark.asyncio
async def test_login_rejects_unknown_email(db_session: AsyncSession) -> None:
    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "nobody@example.com", "whatever", None)


@pytest.mark.asyncio
async def test_login_rejects_disabled_user(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "LOGIN3")
    user_id = await create_user_with_role(
        db_session, email="three@example.com", password="pw12345", company_id=company_id
    )
    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()

    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "three@example.com", "pw12345", None)


@pytest.mark.asyncio
async def test_login_no_entitlement_raises(db_session: AsyncSession) -> None:
    await create_test_user(db_session, email="nowhere@example.com", password="pw12345")

    with pytest.raises(service.NoCompanyEntitlementError):
        await service.login(db_session, "nowhere@example.com", "pw12345", None)


@pytest.mark.asyncio
async def test_login_always_calls_verify_password_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex diff-review finding (Slice B round 1): the (deliberately slow)

    Argon2id `verify_password()` call must run exactly once per login
    attempt regardless of whether the email resolves to a usable user —
    short-circuiting past it for an unknown/disabled-user email would make
    those cases measurably faster than a wrong-password attempt against a
    real, active account: a timing side channel `InvalidCredentialsError`'s
    "one response for all three causes" design is supposed to prevent.
    """
    calls = 0
    real_verify_password = service.verify_password

    def _counting_verify_password(password: str, password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(service, "verify_password", _counting_verify_password)

    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "unknown-entirely@example.com", "whatever", None)
    assert calls == 1

    company_id = await create_company(db_session, "TIMING1")
    user_id = await create_user_with_role(
        db_session, email="timing@example.com", password="pw12345", company_id=company_id
    )
    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()

    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "timing@example.com", "whatever", None)
    assert calls == 2

    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "timing@example.com", "wrong-password", None)
    assert calls == 3


@pytest.mark.asyncio
async def test_login_password_verification_runs_off_event_loop_thread(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_loop_thread = threading.get_ident()
    verification_threads: list[int] = []

    def verify_password(password: str, password_hash: str) -> bool:
        verification_threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(service, "verify_password", verify_password)
    with pytest.raises(service.InvalidCredentialsError):
        await service.login(db_session, "unknown@example.com", "wrong", None)

    assert len(verification_threads) == 1
    assert verification_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_login_multi_company_requires_selection(db_session: AsyncSession) -> None:
    company_a = await create_company(db_session, "MULTIA")
    company_b = await create_company(db_session, "MULTIB")
    user_id = await create_user_with_role(
        db_session, email="multi@example.com", password="pw12345", company_id=company_a
    )
    await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="admin")

    with pytest.raises(service.AmbiguousCompanySelectionError):
        await service.login(db_session, "multi@example.com", "pw12345", None)

    result = await service.login(db_session, "multi@example.com", "pw12345", company_b)
    assert result.active_company_id == company_b


@pytest.mark.asyncio
async def test_login_rejects_company_not_entitled(db_session: AsyncSession) -> None:
    company_a = await create_company(db_session, "ENT1")
    company_b = await create_company(db_session, "ENT2")
    await create_user_with_role(
        db_session, email="ent@example.com", password="pw12345", company_id=company_a
    )

    with pytest.raises(service.CompanyNotEntitledError):
        await service.login(db_session, "ent@example.com", "pw12345", company_b)


# ---------------------------------------------------------------------------
# resolve_actor() — session-token SHA-256 lookup correctness (ADR-009 Decision 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_token_stored_as_sha256_not_raw(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "TOKHASH")
    await create_user_with_role(
        db_session, email="tok@example.com", password="pw12345", company_id=company_id
    )
    result = await service.login(db_session, "tok@example.com", "pw12345", None)

    row = await db_session.get(SessionModel, result.session_id)
    assert row is not None
    assert row.token_hash != result.raw_token
    assert len(row.token_hash) == 64  # SHA-256 hex digest length
    assert row.token_hash == service._hash_token(result.raw_token)


@pytest.mark.asyncio
async def test_resolve_actor_looks_up_by_token_correctly(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "TOKHASH2")
    await create_user_with_role(
        db_session, email="tok2@example.com", password="pw12345", company_id=company_id
    )
    result = await service.login(db_session, "tok2@example.com", "pw12345", None)

    resolved = await service.resolve_actor(db_session, result.raw_token)
    assert resolved.session_id == result.session_id
    assert resolved.actor_context.user_id == result.user_id
    assert resolved.active_company_id == company_id
    assert "session.manage" in resolved.actor_context.permission_codes

    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, result.raw_token + "x")


@pytest.mark.asyncio
async def test_resolve_actor_rejects_expired_session(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "EXPIRED")
    await create_user_with_role(
        db_session, email="exp@example.com", password="pw12345", company_id=company_id
    )
    result = await service.login(db_session, "exp@example.com", "pw12345", None)

    row = await db_session.get(SessionModel, result.session_id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, result.raw_token)


@pytest.mark.asyncio
async def test_resolve_actor_disabled_user_is_distinct_from_invalid_session(
    db_session: AsyncSession,
) -> None:
    """ADR-009 Decision 8: a disabled user's otherwise-valid session raises

    `UserDisabledError`, distinct from `InvalidSessionError` — the caller
    (router.py, Slice D's middleware) maps these to 403 vs 401 respectively.
    """
    company_id = await create_company(db_session, "DISABLED")
    user_id = await create_user_with_role(
        db_session, email="dis@example.com", password="pw12345", company_id=company_id
    )
    result = await service.login(db_session, "dis@example.com", "pw12345", None)

    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()

    with pytest.raises(service.UserDisabledError):
        await service.resolve_actor(db_session, result.raw_token)


# ---------------------------------------------------------------------------
# switch_company() / logout()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_company_re_checks_entitlement(db_session: AsyncSession) -> None:
    company_a = await create_company(db_session, "SWA")
    company_b = await create_company(db_session, "SWB")
    user_id = await create_user_with_role(
        db_session, email="switch@example.com", password="pw12345", company_id=company_a
    )
    result = await service.login(db_session, "switch@example.com", "pw12345", None)
    resolved = await service.resolve_actor(db_session, result.raw_token)

    with actor_context(resolved.actor_context), session_context(result.session_id):
        with pytest.raises(service.CompanyNotEntitledError):
            await service.switch_company(db_session, company_b)

        await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="admin")
        new_company = await service.switch_company(db_session, company_b)
        assert new_company == company_b

    resolved_after = await service.resolve_actor(db_session, result.raw_token)
    assert resolved_after.active_company_id == company_b


@pytest.mark.asyncio
async def test_logout_deletes_session_row(db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "LOGOUT1")
    await create_user_with_role(
        db_session, email="out@example.com", password="pw12345", company_id=company_id
    )
    result = await service.login(db_session, "out@example.com", "pw12345", None)
    resolved = await service.resolve_actor(db_session, result.raw_token)

    with actor_context(resolved.actor_context), session_context(result.session_id):
        await service.logout(db_session)

    row = await db_session.execute(select(SessionModel).where(SessionModel.id == result.session_id))
    assert row.scalars().first() is None
    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, result.raw_token)


@pytest.mark.asyncio
async def test_switch_company_and_logout_require_actor(db_session: AsyncSession) -> None:
    with pytest.raises(ActionDeniedError, match="No actor context is bound"):
        await service.switch_company(db_session, uuid.uuid4())
    with pytest.raises(ActionDeniedError, match="No actor context is bound"):
        await service.logout(db_session)


@pytest.mark.asyncio
async def test_switch_company_and_logout_require_session_context(
    db_session: AsyncSession,
) -> None:
    actor = ActorContext(user_id=uuid.uuid4(), permission_codes=frozenset())
    with (
        actor_context(actor),
        pytest.raises(service.InvalidSessionError, match="No session context"),
    ):
        await service.switch_company(db_session, uuid.uuid4())
    with (
        actor_context(actor),
        pytest.raises(service.InvalidSessionError, match="No session context"),
    ):
        await service.logout(db_session)


@pytest.mark.asyncio
async def test_switch_company_and_logout_reject_missing_session_row(
    db_session: AsyncSession,
) -> None:
    actor = ActorContext(user_id=uuid.uuid4(), permission_codes=frozenset())
    missing_session_id = uuid.uuid4()
    with (
        actor_context(actor),
        session_context(missing_session_id),
        pytest.raises(service.InvalidSessionError, match="not found"),
    ):
        await service.switch_company(db_session, uuid.uuid4())
    with (
        actor_context(actor),
        session_context(missing_session_id),
        pytest.raises(service.InvalidSessionError, match="not found"),
    ):
        await service.logout(db_session)


@pytest.mark.asyncio
async def test_switch_company_and_logout_reject_session_actor_mismatch(
    db_session: AsyncSession,
) -> None:
    """Slice D requirement: ambient session user_id must match current actor user_id."""
    company_a = await create_company(db_session, "HIJACKA")
    await create_user_with_role(
        db_session, email="victim@example.com", password="pw12345", company_id=company_a
    )
    await create_user_with_role(
        db_session, email="attacker@example.com", password="pw12345", company_id=company_a
    )
    login_a = await service.login(db_session, "victim@example.com", "pw12345", None)
    login_b = await service.login(db_session, "attacker@example.com", "pw12345", None)
    actor_b = (await service.resolve_actor(db_session, login_b.raw_token)).actor_context

    # Attacker tries to switch or logout Victim's session
    with actor_context(actor_b), session_context(login_a.session_id):
        with pytest.raises(service.InvalidSessionError):
            await service.switch_company(db_session, company_a)
        with pytest.raises(service.InvalidSessionError):
            await service.logout(db_session)


@pytest.mark.asyncio
async def test_switch_company_rejects_expired_session_and_disabled_user(
    db_session: AsyncSession,
) -> None:
    company_a = await create_company(db_session, "SWEXPA")
    company_b = await create_company(db_session, "SWEXPB")
    user_id = await create_user_with_role(
        db_session, email="swexp@example.com", password="pw12345", company_id=company_a
    )
    await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="admin")
    result = await service.login(db_session, "swexp@example.com", "pw12345", company_a)
    resolved = await service.resolve_actor(db_session, result.raw_token)

    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()

    with (
        actor_context(resolved.actor_context),
        session_context(result.session_id),
        pytest.raises(service.UserDisabledError),
    ):
        await service.switch_company(db_session, company_b)

    user.is_active = True
    session_row = await db_session.get(SessionModel, result.session_id)
    assert session_row is not None
    session_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with (
        actor_context(resolved.actor_context),
        session_context(result.session_id),
        pytest.raises(service.InvalidSessionError, match="expired"),
    ):
        await service.switch_company(db_session, company_b)


# ---------------------------------------------------------------------------
# ADR-018: idle timeout, throttled touch, active cap, rotate_session, logout_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_idle_timeout_and_throttled_touch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.settings import get_settings

    monkeypatch.setenv("MINI_ERP_SESSION_IDLE_TIMEOUT_MINUTES", "30")
    monkeypatch.setenv("MINI_ERP_SESSION_TOUCH_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    try:
        co_id = await create_company(db_session, "IDLECO")
        await create_user_with_role(
            db_session, email="idle@example.com", password="pw12345", company_id=co_id
        )
        res = await service.login(
            db_session,
            "idle@example.com",
            "pw12345",
            None,
            ip_address="203.0.113.10",
            user_agent="pytest-agent/1.0",
        )
        row = await db_session.get(SessionModel, res.session_id)
        assert row is not None
        assert row.ip_address == "203.0.113.10"
        assert row.user_agent == "pytest-agent/1.0"
        initial_touch = row.last_active_at

        # Within touch interval -> last_active_at is not rewritten
        await service.resolve_actor(db_session, res.raw_token)
        await db_session.refresh(row)
        assert row.last_active_at == initial_touch

        # Past touch interval (5 minutes ago) -> resolve_actor refreshes last_active_at
        past_touch = datetime.now(UTC) - timedelta(minutes=5)
        row.last_active_at = past_touch
        await db_session.commit()
        await service.resolve_actor(db_session, res.raw_token)
        await db_session.refresh(row)
        assert row.last_active_at > past_touch

        # Past idle timeout (31 minutes ago) -> rejected with InvalidSessionError
        row.last_active_at = datetime.now(UTC) - timedelta(minutes=31)
        await db_session.commit()
        with pytest.raises(service.InvalidSessionError, match="inactivity"):
            await service.resolve_actor(db_session, res.raw_token)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_login_enforces_max_active_sessions_per_user(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.settings import get_settings

    monkeypatch.setenv("MINI_ERP_SESSION_MAX_ACTIVE_PER_USER", "2")
    get_settings.cache_clear()
    try:
        co_id = await create_company(db_session, "CAPCO")
        await create_user_with_role(
            db_session, email="cap@example.com", password="pw12345", company_id=co_id
        )
        s1 = await service.login(db_session, "cap@example.com", "pw12345", None)
        s2 = await service.login(db_session, "cap@example.com", "pw12345", None)
        s3 = await service.login(db_session, "cap@example.com", "pw12345", None)

        # s1 evicted; s2 and s3 remain active
        with pytest.raises(service.InvalidSessionError):
            await service.resolve_actor(db_session, s1.raw_token)
        assert (await service.resolve_actor(db_session, s2.raw_token)).session_id == s2.session_id
        assert (await service.resolve_actor(db_session, s3.raw_token)).session_id == s3.session_id
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_rotate_session_and_logout_all_lifecycle(db_session: AsyncSession) -> None:
    co_id = await create_company(db_session, "ROTCO")
    await create_user_with_role(
        db_session, email="rot@example.com", password="pw12345", company_id=co_id
    )
    await create_user_with_role(
        db_session, email="other@example.com", password="pw12345", company_id=co_id
    )

    s1 = await service.login(db_session, "rot@example.com", "pw12345", None)
    s2 = await service.login(db_session, "rot@example.com", "pw12345", None)
    s_other = await service.login(db_session, "other@example.com", "pw12345", None)

    row1 = await db_session.get(SessionModel, s1.session_id)
    assert row1 is not None
    original_expires_at = row1.expires_at

    resolved = await service.resolve_actor(db_session, s1.raw_token)
    with actor_context(resolved.actor_context), session_context(s1.session_id):
        rotated = await service.rotate_session(db_session)

    assert rotated.raw_token != s1.raw_token
    assert rotated.session_id == s1.session_id
    assert rotated.expires_at == original_expires_at  # absolute cap is never extended

    # Old token immediately invalid; new token resolves
    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, s1.raw_token)
    resolved_new = await service.resolve_actor(db_session, rotated.raw_token)
    assert resolved_new.session_id == s1.session_id

    # logout_all removes both s1 and s2, leaving other@example.com untouched
    with actor_context(resolved_new.actor_context), session_context(s1.session_id):
        revoked_count = await service.logout_all(db_session)
    assert revoked_count == 2

    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, rotated.raw_token)
    with pytest.raises(service.InvalidSessionError):
        await service.resolve_actor(db_session, s2.raw_token)
    other_actor = await service.resolve_actor(db_session, s_other.raw_token)
    assert other_actor.session_id == s_other.session_id


@pytest.mark.asyncio
async def test_concurrent_logins_respect_max_active_sessions_per_user(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.settings import get_settings

    monkeypatch.setenv("MINI_ERP_SESSION_MAX_ACTIVE_PER_USER", "1")
    get_settings.cache_clear()
    try:
        co_id = await create_company(db_session, "CONCLOGINCO")
        user_id = await create_user_with_role(
            db_session, email="conclogin@example.com", password="pw12345", company_id=co_id
        )
        await db_session.commit()

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async def _login_once() -> service.LoginResult:
            async with session_factory() as s:
                return await service.login(s, "conclogin@example.com", "pw12345", co_id)

        results = await asyncio.gather(*[_login_once() for _ in range(4)])

        session_count = await db_session.scalar(
            select(func.count()).select_from(SessionModel).where(SessionModel.user_id == user_id)
        )
        assert session_count == 1

        valid_count = 0
        for res in results:
            try:
                await service.resolve_actor(db_session, res.raw_token)
                valid_count += 1
            except service.InvalidSessionError:
                pass
        assert valid_count == 1
    finally:
        get_settings.cache_clear()
