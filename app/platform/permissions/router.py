"""platform.permissions FastAPI routes (ADR-009 Decision 3/5/8, WEEK9 Slice B).

`POST /auth/login` needs no prior authentication — it is `Public()` once
Slice D's classification sweep runs (no route dependency is attached to it
here; Slice B only wires the router in, per the brief's "no route outside
`/auth/*` requires auth yet" — see that slice's own text).

`POST /auth/switch-company` and `POST /auth/logout` need a caller identity,
but `AuthenticationMiddleware` doesn't exist until Slice D — this slice's
two handlers therefore resolve the bearer token and bind `ActorContext`/
`current_session_id` **manually**, call `require_permission("session.
manage")` **inside** that binding, then let the `with` block's `__exit__`
reset both `ContextVar`s (success or exception, reverse bind order) before
returning. Slice D removes this manual resolution entirely once middleware
exists and attaches the real `RequiresPermission("session.manage")`
dependency instead (WEEK9 brief Decision 3, amended rounds 3/4/5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context, require_permission
from app.core.db import get_db_session
from app.core.exceptions import ActionDeniedError
from app.platform.permissions import service
from app.platform.permissions.context import session_context
from app.platform.permissions.schemas import (
    LoginRequest,
    LoginResponse,
    SwitchCompanyRequest,
    SwitchCompanyResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    return authorization.removeprefix("Bearer ").strip()


BearerToken = Annotated[str, Depends(_bearer_token)]


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, session: DbSession) -> LoginResponse:
    try:
        result = await service.login(session, payload.email, payload.password, payload.company_id)
    except service.InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except service.NoCompanyEntitlementError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.AmbiguousCompanySelectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except service.CompanyNotEntitledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return LoginResponse(
        access_token=result.raw_token,
        user_id=result.user_id,
        active_company_id=result.active_company_id,
    )


async def _resolve_and_authorize(
    session: AsyncSession, raw_token: str
) -> tuple[ActorContext, uuid.UUID]:
    """Slice B's temporary manual resolution — see module docstring; removed in Slice D."""
    try:
        resolved = await service.resolve_actor(session, raw_token)
    except service.InvalidSessionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except service.UserDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return resolved.actor_context, resolved.session_id


@router.post("/switch-company", response_model=SwitchCompanyResponse)
async def switch_company_endpoint(
    payload: SwitchCompanyRequest, session: DbSession, raw_token: BearerToken
) -> SwitchCompanyResponse:
    actor_ctx, session_id = await _resolve_and_authorize(session, raw_token)
    with actor_context(actor_ctx), session_context(session_id):
        try:
            require_permission("session.manage")
        except ActionDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        try:
            active_company_id = await service.switch_company(
                session, session_id, payload.company_id
            )
        except service.CompanyNotEntitledError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except service.InvalidSessionError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return SwitchCompanyResponse(active_company_id=active_company_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout_endpoint(session: DbSession, raw_token: BearerToken) -> None:
    actor_ctx, session_id = await _resolve_and_authorize(session, raw_token)
    with actor_context(actor_ctx), session_context(session_id):
        try:
            require_permission("session.manage")
        except ActionDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        try:
            await service.logout(session, session_id)
        except service.InvalidSessionError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
