"""tests/platform/permissions/test_router.py — `/auth/*` HTTP endpoints.

Covers the WEEK9 brief's Slice B test list: `/auth/switch-company`/
`/auth/logout`'s inline token resolution (valid/expired/missing token), and
the round-4 addition — `actor_context(...)`/`session_context(...)` leave no
bound `ContextVar` after both a successful request and a request that
raises mid-handler.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import current_actor_or_none
from app.platform.permissions import service
from app.platform.permissions.context import current_session_id_or_none
from app.platform.permissions.models import Session as SessionModel
from app.platform.permissions.models import User
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
)


def _assert_no_leaked_context() -> None:
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_endpoint_success(client: AsyncClient, db_session: AsyncSession) -> None:
    company_id = await create_company(db_session, "HTTPLOGIN")
    await create_user_with_role(
        db_session, email="http@example.com", password="pw12345", company_id=company_id
    )

    response = await client.post(
        "/auth/login", json={"email": "http@example.com", "password": "pw12345"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_company_id"] == str(company_id)
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20


@pytest.mark.asyncio
async def test_login_endpoint_wrong_password_is_401(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(db_session, "HTTPLOGIN2")
    await create_user_with_role(
        db_session, email="http2@example.com", password="pw12345", company_id=company_id
    )

    response = await client.post(
        "/auth/login", json={"email": "http2@example.com", "password": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_endpoint_ambiguous_company_is_400(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_a = await create_company(db_session, "HTTPMULTIA")
    company_b = await create_company(db_session, "HTTPMULTIB")
    user_id = await create_user_with_role(
        db_session, email="httpmulti@example.com", password="pw12345", company_id=company_a
    )
    await service.grant_role(db_session, user_id, company_b, "admin")

    response = await client.post(
        "/auth/login", json={"email": "httpmulti@example.com", "password": "pw12345"}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /auth/switch-company
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_company_missing_token_is_401_no_leak(client: AsyncClient) -> None:
    _assert_no_leaked_context()
    response = await client.post(
        "/auth/switch-company",
        json={"company_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 401
    _assert_no_leaked_context()


@pytest.mark.asyncio
async def test_switch_company_success_no_leak(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_a = await create_company(db_session, "SWHTTPA")
    company_b = await create_company(db_session, "SWHTTPB")
    user_id = await create_user_with_role(
        db_session, email="swhttp@example.com", password="pw12345", company_id=company_a
    )
    await service.grant_role(db_session, user_id, company_b, "admin")

    login_resp = await client.post(
        "/auth/login",
        json={
            "email": "swhttp@example.com",
            "password": "pw12345",
            "company_id": str(company_a),
        },
    )
    token = login_resp.json()["access_token"]
    _assert_no_leaked_context()

    response = await client.post(
        "/auth/switch-company",
        json={"company_id": str(company_b)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_company_id"] == str(company_b)
    _assert_no_leaked_context()  # round-4 addition: no leak after success


@pytest.mark.asyncio
async def test_switch_company_expired_token_is_401_no_leak(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(db_session, "EXPHTTP")
    await create_user_with_role(
        db_session, email="exphttp@example.com", password="pw12345", company_id=company_id
    )
    login_resp = await client.post(
        "/auth/login", json={"email": "exphttp@example.com", "password": "pw12345"}
    )
    token = login_resp.json()["access_token"]

    user_result = await db_session.execute(select(User).where(User.email == "exphttp@example.com"))
    user = user_result.scalars().one()
    session_result = await db_session.execute(
        select(SessionModel).where(SessionModel.user_id == user.id)
    )
    session_row = session_result.scalars().one()
    session_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    response = await client.post(
        "/auth/switch-company",
        json={"company_id": str(company_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    _assert_no_leaked_context()


@pytest.mark.asyncio
async def test_switch_company_insufficient_permission_is_403_no_leak(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A role with no `session.manage` grant — 403, and the exception path

    still resets both `ContextVar`s (round-4 addition's exception-path
    leak test).
    """
    company_a = await create_company(db_session, "NOPERMA")
    company_b = await create_company(db_session, "NOPERMB")
    await create_role_without_permissions(db_session, "guest")
    user_id = await create_user_with_role(
        db_session,
        email="noperm@example.com",
        password="pw12345",
        company_id=company_a,
        role_code="guest",
    )
    await service.grant_role(db_session, user_id, company_b, "guest")

    login_resp = await client.post(
        "/auth/login",
        json={
            "email": "noperm@example.com",
            "password": "pw12345",
            "company_id": str(company_a),
        },
    )
    token = login_resp.json()["access_token"]

    response = await client.post(
        "/auth/switch-company",
        json={"company_id": str(company_b)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    _assert_no_leaked_context()


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_endpoint_revokes_session_no_leak(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(db_session, "LOGOUTHTTP")
    await create_user_with_role(
        db_session, email="logouthttp@example.com", password="pw12345", company_id=company_id
    )

    login_resp = await client.post(
        "/auth/login", json={"email": "logouthttp@example.com", "password": "pw12345"}
    )
    token = login_resp.json()["access_token"]

    response = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 204
    _assert_no_leaked_context()

    # The token no longer resolves to any session (ADR-009 Decision 3:
    # revocation is real deletion, not a soft-revoke flag).
    response2 = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert response2.status_code == 401
    _assert_no_leaked_context()
