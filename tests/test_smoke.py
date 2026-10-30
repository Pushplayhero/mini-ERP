"""Smoke test: the app boots and the health endpoint responds.

Deliberately minimal — this is the "does anything work at all" check the
brief asks for; the substantive coverage lives in tests/masterdata/.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_complete_tier1_allowlist(client: AsyncClient) -> None:
    """ADR-009 public-route allowlist: `/health` is Tier 2 application metadata;
    `/docs`, `/redoc`, `/openapi.json`, and `/docs/oauth2-redirect` are Tier 1 docs.
    """
    for path in ("/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        resp = await client.get(path)
        assert resp.status_code == 200, f"expected 200 for GET {path}, got {resp.status_code}"

    # Non-GET methods on Tier-1 documentation endpoints fail closed (401 or 405)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        resp = await client.post(path)
        assert resp.status_code in (
            401,
            405,
        ), f"expected 401/405 for POST {path}, got {resp.status_code}"


@pytest.mark.asyncio
async def test_authentication_middleware_leak_check(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Middleware leaves no bound ActorContext, current_session_id, or company_context.

    Checked across successful requests, 401 failures, and 403 failures.
    """
    from app.core.authorization import current_actor_or_none
    from app.core.tenancy import current_company_id_or_none
    from app.platform.permissions.context import current_session_id_or_none
    from app.platform.permissions.models import User
    from tests.platform.permissions._helpers import (
        create_company,
        create_role_without_permissions,
        create_user_with_role,
    )

    # 1. 401 unauthenticated request
    resp = await client.get("/api/v1/customers")
    assert resp.status_code == 401
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None

    # 2. Public request
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None

    # 3. Authenticated request success (200)
    company_id = await create_company(db_session, "SMOKECO1")
    user_id = await create_user_with_role(
        db_session, email="smoke_ok@example.com", password="pw", company_id=company_id
    )
    login_resp = await client.post(
        "/auth/login", json={"email": "smoke_ok@example.com", "password": "pw"}
    )
    token = login_resp.json()["access_token"]
    resp = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None

    # 4. Authenticated disabled user (403)
    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()
    resp = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None

    # 5. Authenticated user lacking route permission (403 ActionDeniedError)
    await create_role_without_permissions(db_session, "empty_role")
    company_b = await create_company(db_session, "SMOKECO2")
    await create_user_with_role(
        db_session,
        email="smoke_unprivileged@example.com",
        password="pw",
        company_id=company_b,
        role_code="empty_role",
    )
    login_b = await client.post(
        "/auth/login", json={"email": "smoke_unprivileged@example.com", "password": "pw"}
    )
    token_b = login_b.json()["access_token"]
    resp_b = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b.status_code == 403
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None


@pytest.mark.asyncio
async def test_protected_service_layer_authorization_checks(db_session: AsyncSession) -> None:
    """Protected service functions fail closed on missing or insufficient actor."""
    import uuid

    from app.core.authorization import ActorContext, actor_context
    from app.core.exceptions import ActionDeniedError
    from app.modules.sales import service as sales_service

    # 1. No actor bound -> raises ActionDeniedError (missing context)
    with pytest.raises(ActionDeniedError, match="No actor context is bound"):
        await sales_service.list_orders(db_session)

    # 2. Actor bound but lacks required permission -> raises ActionDeniedError
    unprivileged_actor = ActorContext(
        user_id=uuid.uuid4(),
        permission_codes=frozenset({"dummy.unrelated.permission"}),
    )
    with (
        actor_context(unprivileged_actor),
        pytest.raises(ActionDeniedError, match="lacks permission"),
    ):
        await sales_service.list_orders(db_session)


def test_unclassified_route_crashes_startup() -> None:
    from fastapi import FastAPI

    from app.main import _classify_routes_or_crash

    test_app = FastAPI()

    @test_app.get("/unclassified")
    async def unclassified() -> dict[str, str]:
        return {"status": "unclassified"}

    with pytest.raises(RuntimeError, match="carries 0 classification marker"):
        _classify_routes_or_crash(test_app)


def test_authenticated_route_is_a_valid_single_classification() -> None:
    from fastapi import Depends, FastAPI

    from app.core.authorization import Authenticated
    from app.main import _classify_routes_or_crash

    test_app = FastAPI()

    @test_app.get("/authenticated", dependencies=[Depends(Authenticated())])
    async def authenticated() -> dict[str, str]:
        return {"status": "authenticated"}

    _classify_routes_or_crash(test_app)


def test_multiple_classification_markers_crash_startup() -> None:
    from fastapi import Depends, FastAPI

    from app.core.authorization import Authenticated, Public
    from app.main import _classify_routes_or_crash

    test_app = FastAPI()

    @test_app.get(
        "/ambiguous",
        dependencies=[Depends(Public()), Depends(Authenticated())],
    )
    async def ambiguous() -> dict[str, str]:
        return {"status": "ambiguous"}

    with pytest.raises(RuntimeError, match="carries 2 classification marker"):
        _classify_routes_or_crash(test_app)


def test_shadowing_tier1_path_crashes_startup() -> None:
    from fastapi import Depends, FastAPI

    from app.core.authorization import Public
    from app.main import _classify_routes_or_crash

    test_app = FastAPI()

    @test_app.get("/docs", dependencies=[Depends(Public())])
    async def custom_docs() -> dict[str, str]:
        return {"status": "shadowed"}

    with pytest.raises(RuntimeError, match="Custom route shadows Tier-1 documentation path"):
        _classify_routes_or_crash(test_app)


def test_starlette_route_shadowing_tier1_crashes_startup() -> None:
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.main import _classify_routes_or_crash

    async def shadow(request: object) -> PlainTextResponse:
        return PlainTextResponse("shadow")

    test_app = FastAPI()
    test_app.router.routes.insert(0, Route("/docs", shadow, methods=["GET"]))

    with pytest.raises(RuntimeError, match="Custom route shadows Tier-1 documentation path"):
        _classify_routes_or_crash(test_app)
