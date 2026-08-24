"""Authentication and authorization test helpers for Slice D."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass

from httpx import AsyncClient

from app.core.db import get_session_factory
from app.platform.permissions import service as permissions_service

_COMPANY_TOKEN_CACHE: dict[uuid.UUID, str] = {}


def register_company_token(company_id: uuid.UUID, token: str) -> None:
    _COMPANY_TOKEN_CACHE[company_id] = token


def get_company_token(company_id: uuid.UUID) -> str | None:
    return _COMPANY_TOKEN_CACHE.get(company_id)


def clear_company_token_cache() -> None:
    _COMPANY_TOKEN_CACHE.clear()


@dataclass(frozen=True)
class CompanyAuth:
    company_id: uuid.UUID
    token: str
    headers: dict[str, str]


async def login_user(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    company_id: uuid.UUID | None = None,
) -> str:
    payload: dict[str, str] = {"email": email, "password": password}
    if company_id is not None:
        payload["company_id"] = str(company_id)
    resp = await client.post("/auth/login", json=payload)
    resp.raise_for_status()
    data = resp.json()
    return str(data["access_token"])


async def _ensure_anchor_admin_token(client: AsyncClient) -> str:
    """Ensure an initial bootstrap admin exists and return its access token.

    This ensures that ALL test companies (even the very first one in a test)
    are created strictly through the authenticated HTTP endpoint `POST /api/v1/companies`,
    exercising full route authorization, permission checks (masterdata.company.create),
    validation, and response handlers (ADR-009 Cutover).
    """
    existing_token = next(iter(_COMPANY_TOKEN_CACHE.values()), None)
    if existing_token is not None:
        return existing_token

    from app.cli.bootstrap_admin import BootstrapRefusedError, bootstrap_admin

    anchor_code = f"ANCH_{uuid.uuid4().hex[:6].upper()}"
    anchor_email = f"anchor_{uuid.uuid4().hex[:6]}@example.com"
    anchor_pw = "anchoradmin123"

    with contextlib.suppress(BootstrapRefusedError):
        await bootstrap_admin(
            email=anchor_email,
            password=anchor_pw,
            company_code=anchor_code,
            company_name="Anchor Corp",
        )

    token = await login_user(client, anchor_email, anchor_pw)
    return token


async def setup_company_with_admin(
    client: AsyncClient,
    *,
    code: str = "TEST",
    name: str | None = None,
    email: str | None = None,
    password: str = "adminpassword123",
) -> CompanyAuth:
    company_name = name or f"{code} Corp"
    user_email = email or f"admin_{code.lower()}_{uuid.uuid4().hex[:6]}@example.com"
    session_factory = get_session_factory()

    admin_token = await _ensure_anchor_admin_token(client)

    # Every company is created through the authenticated HTTP endpoint POST /api/v1/companies
    resp = await client.post(
        "/api/v1/companies",
        json={"code": code, "name": company_name, "functional_currency_code": "TWD"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if resp.status_code == 201:
        company_id = uuid.UUID(resp.json()["id"])
    else:
        raise RuntimeError(
            f"setup_company_with_admin: failed to create company {code} via HTTP: "
            f"{resp.status_code} {resp.text}"
        )

    # Bind the admin user and role to the new company
    async with session_factory() as session:
        await permissions_service.add_user_with_grant(
            session,
            email=user_email,
            password=password,
            company_id=company_id,
            role_code="admin",
        )
        await session.commit()

    token = await login_user(client, user_email, password, company_id=company_id)
    register_company_token(company_id, token)
    headers = {"Authorization": f"Bearer {token}"}
    return CompanyAuth(company_id=company_id, token=token, headers=headers)


def update_company_token(old_company_id: uuid.UUID, new_company_id: uuid.UUID, token: str) -> None:
    """Keep the company-to-token cache synchronized when a token switches its active company."""
    if old_company_id in _COMPANY_TOKEN_CACHE and _COMPANY_TOKEN_CACHE[old_company_id] == token:
        del _COMPANY_TOKEN_CACHE[old_company_id]
    _COMPANY_TOKEN_CACHE[new_company_id] = token


async def switch_company_and_update_auth(
    client: AsyncClient,
    auth: CompanyAuth,
    new_company_id: uuid.UUID,
) -> CompanyAuth:
    """Perform /auth/switch-company and atomically update the company-to-token cache."""
    resp = await client.post(
        "/auth/switch-company",
        json={"company_id": str(new_company_id)},
        headers=auth.headers,
    )
    resp.raise_for_status()
    update_company_token(auth.company_id, new_company_id, auth.token)
    return CompanyAuth(company_id=new_company_id, token=auth.token, headers=auth.headers)
