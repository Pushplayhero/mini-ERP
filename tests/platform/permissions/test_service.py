"""tests/platform/permissions/test_service.py — login/session flow (ADR-009 Decision 3/8).

Covers the WEEK9 brief's Slice B test list: login success/failure incl.
multi-company selection, Argon2id hash verification, session-token
SHA-256 lookup correctness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
