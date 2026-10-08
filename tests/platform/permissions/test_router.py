"""tests/platform/permissions/test_router.py — `/auth/*` HTTP endpoints.

Covers the Slice D session-lifecycle HTTP contract: valid, expired, missing,
and malformed bearer tokens; guest-role A → B → A switching followed by logout;
and middleware cleanup of actor, session, and company `ContextVar`s after
successful and rejected requests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import current_actor_or_none
from app.core.tenancy import current_company_id_or_none
from app.platform.permissions import service
from app.platform.permissions.context import current_session_id_or_none
from app.platform.permissions.models import Session as SessionModel
from app.platform.permissions.models import User
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
    grant_test_role,
)


def _assert_no_leaked_context() -> None:
    assert current_actor_or_none() is None
    assert current_session_id_or_none() is None
    assert current_company_id_or_none() is None


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
    await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="admin")

    response = await client.post(
        "/auth/login", json={"email": "httpmulti@example.com", "password": "pw12345"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_login_rejects_oversized_body_before_password_verification(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def verify_password(password: str, password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr("app.platform.permissions.service.verify_password", verify_password)

    async def oversized_body() -> AsyncIterator[bytes]:
        yield b'{"email":"large@example.com","password":"'
        yield b"x" * 20_000
        yield b'"}'

    response = await client.post(
        "/auth/login", content=oversized_body(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert calls == 0


@pytest.mark.asyncio
async def test_login_rejects_overlong_password_before_password_verification(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def verify_password(password: str, password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr("app.platform.permissions.service.verify_password", verify_password)
    response = await client.post(
        "/auth/login", json={"email": "large@example.com", "password": "x" * 1025}
    )

    assert response.status_code == 422
    assert calls == 0


@pytest.mark.asyncio
async def test_login_rate_limit_rejects_repeated_peer_before_password_verification(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def verify_password(password: str, password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr("app.platform.permissions.service.verify_password", verify_password)
    responses = [
        await client.post(
            "/auth/login",
            json={"email": f"user-{index}@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
        )
        for index in range(11)
    ]

    assert [response.status_code for response in responses] == [401] * 10 + [429]
    assert calls == 10


@pytest.mark.asyncio
async def test_malformed_login_requests_do_not_consume_peer_quota(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def verify_password(password: str, password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr("app.platform.permissions.service.verify_password", verify_password)
    malformed = [
        await client.post("/auth/login", content=b"", headers={"Content-Type": "application/json"})
        for _ in range(10)
    ]
    valid_shape = await client.post(
        "/auth/login", json={"email": "unknown@example.com", "password": "wrong"}
    )

    assert [response.status_code for response in malformed] == [422] * 10
    assert valid_shape.status_code == 401
    assert calls == 1


@pytest.mark.asyncio
async def test_login_verifications_are_bounded_per_process(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    maximum_active = 0

    async def login(*_args: object, **_kwargs: object) -> service.LoginResult:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return service.LoginResult(
            raw_token="test-token",
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            active_company_id=uuid.uuid4(),
        )

    monkeypatch.setattr("app.platform.permissions.router.service.login", login)
    responses = await asyncio.gather(
        *[
            client.post(
                "/auth/login",
                json={"email": f"parallel-{index}@example.com", "password": "valid"},
            )
            for index in range(8)
        ]
    )

    assert [response.status_code for response in responses] == [200] * 8
    assert maximum_active == 4


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
    await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="admin")

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
async def test_guest_can_switch_company_round_trip_and_logout_no_leak(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Session lifecycle is authenticated, not gated by ``session.manage``."""
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
    await grant_test_role(db_session, user_id=user_id, company_id=company_b, role_code="guest")

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
    assert response.status_code == 200, response.text
    assert response.json()["active_company_id"] == str(company_b)
    _assert_no_leaked_context()

    response_back = await client.post(
        "/auth/switch-company",
        json={"company_id": str(company_a)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response_back.status_code == 200, response_back.text
    assert response_back.json()["active_company_id"] == str(company_a)
    _assert_no_leaked_context()

    logout_response = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout_response.status_code == 204
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


@pytest.mark.asyncio
async def test_bearer_scheme_case_insensitivity_and_malformed_headers(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    company_id = await create_company(db_session, "BEARERCASE")
    await create_user_with_role(
        db_session, email="bearer_case@example.com", password="pw", company_id=company_id
    )
    login_resp = await client.post(
        "/auth/login", json={"email": "bearer_case@example.com", "password": "pw"}
    )
    token = login_resp.json()["access_token"]

    # 1. Lowercase "bearer <token>"
    resp = await client.get("/api/v1/customers", headers={"Authorization": f"bearer {token}"})
    assert resp.status_code == 200
    _assert_no_leaked_context()

    # 2. Uppercase "BEARER <token>"
    resp = await client.get("/api/v1/customers", headers={"Authorization": f"BEARER {token}"})
    assert resp.status_code == 200
    _assert_no_leaked_context()

    # 3. Malformed: wrong scheme
    resp = await client.get("/api/v1/customers", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 401
    _assert_no_leaked_context()

    # 4. Malformed: empty token
    resp = await client.get("/api/v1/customers", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    _assert_no_leaked_context()

    # 5. Malformed: scheme only
    resp = await client.get("/api/v1/customers", headers={"Authorization": "Bearer"})
    assert resp.status_code == 401
    _assert_no_leaked_context()
