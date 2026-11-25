"""platform.permissions FastAPI routes (ADR-009 Decision 3/5/8 & ADR-018).

`POST /auth/login` is public (`dependencies=[Depends(Public())]`), taking user
credentials and returning an opaque session bearer token with initial company tenancy.

`POST /auth/switch-company`, `POST /auth/rotate`, `POST /auth/logout`, and
`POST /auth/logout-all` are authenticated routes protected by
`AuthenticationMiddleware` and `dependencies=[Depends(Authenticated())]`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Authenticated, Public
from app.core.db import get_db_session
from app.platform.permissions import service
from app.platform.permissions.schemas import (
    LoginRequest,
    LoginResponse,
    RotateSessionResponse,
    SwitchCompanyRequest,
    SwitchCompanyResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _set_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(Public())])
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    session: DbSession,
) -> LoginResponse:
    _set_no_store_headers(response)
    # FastAPI validates the bounded request body before entering this function,
    # so malformed requests cannot consume a shared peer's attempt quota.
    peer = request.client.host if request.client is not None else "unknown"
    allowed, retry_after = request.app.state.login_rate_limiter.allow(peer)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    client_ip = request.client.host if request.client is not None else None
    user_agent = request.headers.get("user-agent")
    try:
        async with request.app.state.login_verification_semaphore:
            result = await service.login(
                session,
                payload.email,
                payload.password,
                payload.company_id,
                ip_address=client_ip,
                user_agent=user_agent,
            )
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


@router.post(
    "/switch-company",
    response_model=SwitchCompanyResponse,
    dependencies=[Depends(Authenticated())],
)
async def switch_company_endpoint(
    response: Response,
    payload: SwitchCompanyRequest,
    session: DbSession,
) -> SwitchCompanyResponse:
    _set_no_store_headers(response)
    try:
        active_company_id = await service.switch_company(session, payload.company_id)
    except service.CompanyNotEntitledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.UserDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.InvalidSessionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return SwitchCompanyResponse(active_company_id=active_company_id)


@router.post(
    "/rotate",
    response_model=RotateSessionResponse,
    dependencies=[Depends(Authenticated())],
)
async def rotate_session_endpoint(
    response: Response,
    session: DbSession,
) -> RotateSessionResponse:
    _set_no_store_headers(response)
    try:
        result = await service.rotate_session(session)
    except service.UserDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.InvalidSessionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return RotateSessionResponse(
        access_token=result.raw_token,
        user_id=result.user_id,
        active_company_id=result.active_company_id,
        expires_at=result.expires_at,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(Authenticated())],
)
async def logout_endpoint(response: Response, session: DbSession) -> None:
    _set_no_store_headers(response)
    try:
        await service.logout(session)
    except service.InvalidSessionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(Authenticated())],
)
async def logout_all_endpoint(response: Response, session: DbSession) -> None:
    _set_no_store_headers(response)
    try:
        await service.logout_all(session)
    except service.UserDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.InvalidSessionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
