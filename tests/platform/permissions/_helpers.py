from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.masterdata.models import Company
from app.platform.permissions import service as permissions_service
from app.platform.permissions.models import Role, User, UserCompanyRole


async def create_company(session: AsyncSession, code: str) -> uuid.UUID:
    company = Company(code=code, name=f"{code} Inc.", functional_currency_code="TWD")
    session.add(company)
    await session.commit()
    return company.id


async def create_user_with_role(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    company_id: uuid.UUID,
    role_code: str = "admin",
) -> uuid.UUID:
    user = await permissions_service.add_user_with_grant(
        session, email=email, password=password, company_id=company_id, role_code=role_code
    )
    await session.commit()
    return user.id


async def create_test_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
) -> uuid.UUID:
    """Create a user with credentials but no company role grants."""
    user = User(
        email=email,
        password_hash=permissions_service.hash_password(password),
    )
    session.add(user)
    await session.commit()
    return user.id


async def grant_test_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    role_code: str,
) -> None:
    """Grant an existing user a role in a company."""
    session.add(UserCompanyRole(user_id=user_id, company_id=company_id, role_code=role_code))
    await session.commit()


async def create_role_without_permissions(session: AsyncSession, code: str) -> None:
    """A role deliberately granted zero permissions — for testing the

    session-lifecycle authentication path: a guest role may switch/logout,
    while permission-protected business routes still deny it. The seeded
    `admin` role retains `session.manage` (migration 0009).
    """
    session.add(Role(code=code, name=code.title()))
    await session.commit()
