"""platform.permissions service layer (ADR-009 Decision 2/3/8).

`login()` / `resolve_actor()` / `switch_company()` / `logout()` — the sole
producer of `app.core.authorization.ActorContext` (ADR-009 Decision 4).
Session-lifecycle entry points call `require_current_actor()` and may raise
`ActionDeniedError` when called without an actor. Ownership and entitlement failures
remain this module's own exceptions; `router.py` maps its domain exceptions to HTTP
status.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import (
    ActorContext,
    current_actor_or_none,
    require_current_actor,
)
from app.core.exceptions import AppError
from app.core.settings import get_settings
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
class ResolvedActor:
    actor_context: ActorContext
    session_id: uuid.UUID
    active_company_id: uuid.UUID


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
    session: AsyncSession, email: str, password: str, company_id: uuid.UUID | None
) -> LoginResult:
    """ADR-009 Decision 8: multi-company selection, entitlement-validated immediately.

    Codex diff-review finding (Slice B round 1): `verify_password()` — an
    intentionally slow Argon2id call — must run exactly once on every login
    attempt, regardless of whether the email resolves to a usable user.
    Short-circuiting past it for an unknown/disabled-user email would make
    those two cases measurably faster than a known-active-user-wrong-
    password attempt, a timing side channel that defeats
    `InvalidCredentialsError`'s whole "one response for all three causes"
    point — the response body already refuses to distinguish them; the
    clock must not either.
    """
    result = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    found_user = result.scalars().first()
    usable_user = found_user if (found_user is not None and found_user.is_active) else None
    password_hash_to_check = (
        usable_user.password_hash if usable_user is not None else _DUMMY_PASSWORD_HASH
    )
    password_ok = verify_password(password, password_hash_to_check)
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

    raw_token = secrets.token_urlsafe(32)
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    session_row = SessionModel(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        active_company_id=active_company_id,
        expires_at=expires_at,
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
    """Resolve a bearer token to a fresh `ActorContext` (ADR-009 Decision 8:

    per-request revalidation, never cached beyond the `sessions` row).
    Raises `InvalidSessionError` (401) for a missing/expired session, or
    `UserDisabledError` (403) for a valid session whose user is now
    disabled — the two are deliberately distinct exceptions, not one,
    since ADR-009 Decision 8 requires they map to different HTTP statuses.
    """
    token_hash = _hash_token(raw_token)
    result = await session.execute(
        select(SessionModel).where(SessionModel.token_hash == token_hash)
    )
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError("No session matches the presented token.")
    if session_row.expires_at <= datetime.now(UTC):
        raise InvalidSessionError("Session has expired.")

    user_result = await session.execute(select(User).where(User.id == session_row.user_id))
    user = user_result.scalars().first()
    if user is None:
        # A session whose user no longer resolves is indistinguishable,
        # from the caller's point of view, from a session that never
        # existed at all — same 401 as a bad token.
        raise InvalidSessionError(f"Session {session_row.id} has no resolvable user.")
    if not user.is_active:
        raise UserDisabledError(f"User {user.id} is disabled.")

    permission_codes = await _resolve_permission_codes(
        session, user.id, session_row.active_company_id
    )
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


async def switch_company(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    """Re-point a session's active company, re-checking entitlement immediately."""
    require_current_actor()
    session_id = require_current_session_id()
    result = await session.execute(select(SessionModel).where(SessionModel.id == session_id))
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError(f"Session {session_id} not found.")

    _assert_current_actor_owns_session(session_row)

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
    await session.commit()
    return company_id


async def logout(session: AsyncSession) -> None:
    """Revoke a session — deletion, not a soft-revoke flag.

    ADR-009 Decision 3: "Revocation: Trivial — delete the row."
    """
    require_current_actor()
    session_id = require_current_session_id()
    result = await session.execute(select(SessionModel).where(SessionModel.id == session_id))
    session_row = result.scalars().first()
    if session_row is None:
        raise InvalidSessionError(f"Session {session_id} not found.")

    _assert_current_actor_owns_session(session_row)

    await session.delete(session_row)
    await session.commit()


# ---------------------------------------------------------------------------
# Bootstrap-only helpers (used by `app/cli/bootstrap_admin.py`, Rule 10b —
# no `ActorContext`/`require_permission()` involved, see that module).
# ---------------------------------------------------------------------------


async def add_user_with_grant(
    session: AsyncSession, *, email: str, password: str, company_id: uuid.UUID, role_code: str
) -> User:
    """Non-committing: adds `User` + `UserCompanyRole` to the session,

    flushing (assigning both their ids) but never committing — the caller
    owns the transaction boundary and must commit.

    Codex diff-review finding (Slice B rounds 1/2): `bootstrap_admin.py`
    originally called separate, each-self-committing `create_user()` +
    `grant_role()`. A failure between the two left a real `users` row with
    no entitlement, permanently unrecoverable (`bootstrap_admin.py` refuses
    to run a second time once any `users` row exists, Rule 10b). Round 1's
    first fix merged the two writes into one commit here but round 2 caught
    that this alone was insufficient: `bootstrap_admin.py` also creates
    reference data/a company *before* calling this, through
    `masterdata_service`'s own self-committing `create_currency`/
    `create_uom`/`create_company` — each of those commits independently
    ends the whole advisory-lock-protected transaction early, defeating the
    lock. `bootstrap_admin.py` now builds every write (reference data,
    company, user, grant) in one uncommitted transaction and calls this
    non-committing function last, committing exactly once itself at the
    very end — see that module for the full sequence.
    """
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()  # assigns user.id without committing
    grant = UserCompanyRole(user_id=user.id, company_id=company_id, role_code=role_code)
    session.add(grant)
    await session.flush()
    return user


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return result.scalar_one()
