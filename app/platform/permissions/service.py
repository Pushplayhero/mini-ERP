"""platform.permissions service layer (ADR-009 Decision 2/3/8 & ADR-018).

`login()` / `resolve_actor()` / `switch_company()` / `rotate_session()` /
`logout()` / `logout_all()` — the sole producer of `app.core.authorization.ActorContext`.
Session-lifecycle entry points call `require_current_actor()` and may raise
`ActionDeniedError` when called without an actor. Ownership and entitlement failures
remain this module's own exceptions; `router.py` maps its domain exceptions to HTTP
status.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import (
    ActorContext,
    current_actor_or_none,
    require_current_actor,
)
from app.core.exceptions import AppError
from app.core.settings import Settings, get_settings
from app.platform.permissions.context import (
    InvalidSessionError as InvalidSessionError,  # noqa: PLC0414
)
from app.platform.permissions.context import (
    require_current_session_id,
)
from app.platform.permissions.models import RolePermission, User, UserCompanyRole
from app.platform.permissions.models import Session as SessionModel

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Argon2id (ADR-009 Decision 2) — the current OWASP-recommended default."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


# A fixed, never-matched hash `login()` verifies against when no usable
# user exists, so the (deliberately slow) Argon2id call always runs once —
# never skipped for an unknown/disabled-user email (Codex diff-review
# finding, Slice B round 1: skipping it created a timing side channel).
# Computed once at import time, not per request.
_DUMMY_PASSWORD_HASH = hash_password("mini-erp-constant-time-login-dummy")


def _hash_token(raw_token: str) -> str:
    """SHA-256, deliberately NOT Argon2id (ADR-009 Decision 3) — a fast,

    deterministic hash is required for the indexed `WHERE token_hash = ?`
    lookup every authenticated request performs; Argon2id's slowness
    defends low-entropy *passwords*, which a 256-bit random token does not
    need.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _truncate_metadata(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


# ---------------------------------------------------------------------------
# Errors — this module's own vocabulary, mapped to HTTP status by router.py.
# ---------------------------------------------------------------------------


class InvalidCredentialsError(AppError):
    """Login failed: unknown email, wrong password, or a disabled user.

    Deliberately one exception for all three causes — distinguishing them
    in the response would leak which emails are registered.
    """


class NoCompanyEntitlementError(AppError):
    """Credentials were valid, but the user has zero company entitlements."""


class AmbiguousCompanySelectionError(AppError):
    """A multi-company user must specify `company_id` at login (ADR-009 Decision 8)."""


class CompanyNotEntitledError(AppError):
    """The requested `company_id` is not one this user/session is entitled to."""


class UserDisabledError(AppError):
    """A valid, unexpired session belongs to a disabled user.

    ADR-009 Decision 8, explicit: this is 403 ("authenticated, not
    authorized"), not 401 — the token itself is genuine, the fresh
    `is_active` check is what fails.
    """


@dataclass(frozen=True, slots=True)
class LoginResult:
    raw_token: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    active_company_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RotateSessionResult:
    raw_token: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    active_company_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedActor:
    actor_context: ActorContext
    session_id: uuid.UUID
    active_company_id: uuid.UUID


def _session_invalidation_reason(
    session_row: SessionModel,
    now: datetime,
    settings: Settings,
) -> str | None:
    """Return error reason if `session_row` exceeded hard TTL or idle timeout."""
    if session_row.expires_at <= now:
        return "Session has expired."
    idle_deadline = session_row.last_active_at + timedelta(
        minutes=settings.session_idle_timeout_minutes
    )
    if idle_deadline <= now:
        return "Session has expired due to inactivity."
    return None


async def _resolve_permission_codes(
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID
) -> frozenset[str]:
    """Per-request entitlement resolution (ADR-009 Decision 8) — always

    fresh, never cached beyond the `sessions` row itself, so a revoked
    role/permission takes effect on the very next request.
    """
    result = await session.execute(
        select(RolePermission.permission_code)
        .join(UserCompanyRole, UserCompanyRole.role_code == RolePermission.role_code)
        .where(UserCompanyRole.user_id == user_id, UserCompanyRole.company_id == company_id)
    )
    return frozenset(row[0] for row in result.all())


async def login(
    session: AsyncSession,
    email: str,
    password: str,
    company_id: uuid.UUID | None,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> LoginResult:
    """ADR-009 Decision 8 & ADR-018: login with constant-time Argon2id and session cap."""
    result = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    found_user = result.scalars().first()
    usable_user = found_user if (found_user is not None and found_user.is_active) else None
    password_hash_to_check = (
        usable_user.password_hash if usable_user is not None else _DUMMY_PASSWORD_HASH
    )
    password_ok = await asyncio.to_thread(verify_password, password, password_hash_to_check)
    if usable_user is None or not password_ok:
        raise InvalidCredentialsError("Invalid email or password.")
    user = usable_user

    entitlements = await session.execute(
        select(UserCompanyRole.company_id).where(UserCompanyRole.user_id == user.id).distinct()
    )
    entitled_company_ids = {row[0] for row in entitlements.all()}
    if not entitled_company_ids:
        raise NoCompanyEntitlementError(f"User {user.id} has no company entitlements.")

    if company_id is not None:
        if company_id not in entitled_company_ids:
            raise CompanyNotEntitledError(
                f"User {user.id} is not entitled to company {company_id}."
            )
        active_company_id = company_id
    elif len(entitled_company_ids) == 1:
        active_company_id = next(iter(entitled_company_ids))
    else:
        raise AmbiguousCompanySelectionError(
            f"User {user.id} has multiple company entitlements; company_id is required."
        )

    settings = get_settings()
    now = datetime.now(UTC)

    # Serialize per-user session issuance under row locks (locking User first so
    # zero-row / delete+insert READ COMMITTED races cannot exceed the cap),
    # purge stale sessions, and evict least-recently-active sessions beyond limit.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing_sessions = list(
        (
            await session.scalars(
                select(SessionModel)
                .where(SessionModel.user_id == user.id)
                .order_by(
                    SessionModel.last_active_at.asc(),
                    SessionModel.created_at.asc(),
                    SessionModel.id.asc(),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    active_sessions: list[SessionModel] = []
    for existing in existing_sessions:
        if _session_invalidation_reason(existing, now, settings) is not None:
            await session.delete(existing)
        else:
            active_sessions.append(existing)

    overflow = len(active_sessions) - (settings.session_max_active_per_user - 1)
    for stale in active_sessions[: max(0, overflow)]:
        await session.delete(stale)

    raw_token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(hours=settings.session_ttl_hours)
    session_row = SessionModel(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        active_company_id=active_company_id,
        expires_at=expires_at,
        last_active_at=now,
        ip_address=_truncate_metadata(ip_address, 64),
        user_agent=_truncate_metadata(user_agent, 255),
    )
    session.add(session_row)
    await session.commit()
    await session.refresh(session_row)

    return LoginResult(
        raw_token=raw_token,
        session_id=session_row.id,
        user_id=user.id,
        active_company_id=active_company_id,
    )


async def resolve_actor(session: AsyncSession, raw_token: str) -> ResolvedActor:
    """Resolve a bearer token to a fresh `ActorContext` (ADR-009 Decision 8 & ADR-018)."""
    token_hash = _hash_token(raw_token)
    result = await session.execute(
        select(SessionModel)
        .where(SessionModel.token_hash == token_hash)
        .execution_options(populate_existing=True)
    )
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError("No session matches the presented token.")

    settings = get_settings()
    now = datetime.now(UTC)
    invalid_reason = _session_invalidation_reason(session_row, now, settings)
    if invalid_reason is not None:
        raise InvalidSessionError(invalid_reason)

    user_result = await session.execute(
        select(User).where(User.id == session_row.user_id).execution_options(populate_existing=True)
    )
    user = user_result.scalars().first()
    if user is None:
        raise InvalidSessionError(f"Session {session_row.id} has no resolvable user.")
    if not user.is_active:
        raise UserDisabledError(f"User {user.id} is disabled.")

    permission_codes = await _resolve_permission_codes(
        session, user.id, session_row.active_company_id
    )

    elapsed_seconds = (now - session_row.last_active_at).total_seconds()
    if elapsed_seconds >= settings.session_touch_interval_seconds:
        await session.execute(
            update(SessionModel).where(SessionModel.id == session_row.id).values(last_active_at=now)
        )
        await session.commit()

    actor_ctx = ActorContext(user_id=user.id, permission_codes=permission_codes)
    return ResolvedActor(
        actor_context=actor_ctx,
        session_id=session_row.id,
        active_company_id=session_row.active_company_id,
    )


def _assert_current_actor_owns_session(session_row: SessionModel) -> None:
    actor = current_actor_or_none()
    if actor is None:
        raise InvalidSessionError("No actor context is bound for the current session.")
    if session_row.user_id != actor.user_id:
        raise InvalidSessionError("The current actor does not own the requested session.")


async def _lock_owned_active_session(
    session: AsyncSession,
    *,
    now: datetime,
    settings: Settings,
) -> tuple[SessionModel, User]:
    """Lock the current session row FOR UPDATE and validate ownership, TTL, idle, and user."""
    require_current_actor()
    session_id = require_current_session_id()
    result = await session.execute(
        select(SessionModel)
        .where(SessionModel.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError(f"Session {session_id} not found.")

    _assert_current_actor_owns_session(session_row)

    invalid_reason = _session_invalidation_reason(session_row, now, settings)
    if invalid_reason is not None:
        raise InvalidSessionError(invalid_reason)

    user_result = await session.execute(
        select(User).where(User.id == session_row.user_id).execution_options(populate_existing=True)
    )
    user = user_result.scalars().first()
    if user is None:
        raise InvalidSessionError(f"Session {session_row.id} has no resolvable user.")
    if not user.is_active:
        raise UserDisabledError(f"User {user.id} is disabled.")

    return session_row, user


async def switch_company(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    """Re-point a session's active company, re-checking entitlement immediately."""
    settings = get_settings()
    now = datetime.now(UTC)
    session_row, _ = await _lock_owned_active_session(session, now=now, settings=settings)

    entitled = await session.execute(
        select(UserCompanyRole.id).where(
            UserCompanyRole.user_id == session_row.user_id,
            UserCompanyRole.company_id == company_id,
        )
    )
    if entitled.first() is None:
        raise CompanyNotEntitledError(
            f"User {session_row.user_id} is not entitled to company {company_id}."
        )

    session_row.active_company_id = company_id
    session_row.last_active_at = now
    await session.commit()
    return company_id


async def rotate_session(session: AsyncSession) -> RotateSessionResult:
    """Atomically rotate the current bearer token without extending absolute `expires_at`."""
    settings = get_settings()
    now = datetime.now(UTC)
    session_row, user = await _lock_owned_active_session(session, now=now, settings=settings)

    new_raw_token = secrets.token_urlsafe(32)
    session_row.token_hash = _hash_token(new_raw_token)
    session_row.last_active_at = now
    await session.commit()

    return RotateSessionResult(
        raw_token=new_raw_token,
        session_id=session_row.id,
        user_id=user.id,
        active_company_id=session_row.active_company_id,
        expires_at=session_row.expires_at,
    )


async def logout(session: AsyncSession) -> None:
    """Revoke a session — deletion, not a soft-revoke flag."""
    require_current_actor()
    session_id = require_current_session_id()
    result = await session.execute(
        select(SessionModel)
        .where(SessionModel.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError(f"Session {session_id} not found.")

    _assert_current_actor_owns_session(session_row)

    await session.delete(session_row)
    await session.commit()


def _rowcount(result: object) -> int:
    count = getattr(result, "rowcount", 0)
    return int(count) if isinstance(count, int) else 0


async def logout_all(session: AsyncSession) -> int:
    """Revoke every active session owned by the current authenticated user."""
    settings = get_settings()
    now = datetime.now(UTC)
    _, user = await _lock_owned_active_session(session, now=now, settings=settings)

    res = await session.execute(delete(SessionModel).where(SessionModel.user_id == user.id))
    await session.commit()
    return _rowcount(res)


async def purge_expired_sessions(session: AsyncSession) -> int:
    """Delete all sessions past absolute expiry or idle timeout (operator CLI)."""
    settings = get_settings()
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    res = await session.execute(
        delete(SessionModel).where(
            or_(
                SessionModel.expires_at <= now,
                SessionModel.last_active_at <= idle_cutoff,
            )
        )
    )
    await session.commit()
    return _rowcount(res)


async def revoke_user_sessions_by_email(
    session: AsyncSession, email: str
) -> tuple[uuid.UUID | None, int]:
    """Delete all sessions for the user identified by `email` (operator CLI)."""
    user = await session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None:
        return None, 0
    res = await session.execute(delete(SessionModel).where(SessionModel.user_id == user.id))
    await session.commit()
    return user.id, _rowcount(res)


# ---------------------------------------------------------------------------
# Bootstrap-only helpers (used by `app/cli/bootstrap_admin.py`, Rule 10b —
# no `ActorContext`/`require_permission()` involved, see that module).
# ---------------------------------------------------------------------------


async def add_user_with_grant(
    session: AsyncSession, *, email: str, password: str, company_id: uuid.UUID, role_code: str
) -> User:
    """Non-committing: adds `User` + `UserCompanyRole` to the session."""
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    grant = UserCompanyRole(user_id=user.id, company_id=company_id, role_code=role_code)
    session.add(grant)
    await session.flush()
    return user


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return result.scalar_one()
