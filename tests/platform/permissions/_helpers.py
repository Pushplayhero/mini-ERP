"""Shared helpers for tests/platform/permissions/* — mirrors the small

per-file `_create_company` helpers in tests/masterdata/*, plus a
permissions-specific "create a user granted a role in a company" helper.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import CompanyCreate
from app.platform.permissions import service as permissions_service
from app.platform.permissions.models import Role


async def create_company(session: AsyncSession, code: str) -> uuid.UUID:
    company = await masterdata_service.create_company(
        session, CompanyCreate(code=code, name=f"{code} Inc.", functional_currency_code="TWD")
    )
    return company.id


async def create_user_with_role(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    company_id: uuid.UUID,
    role_code: str = "admin",
) -> uuid.UUID:
    user = await permissions_service.create_user(session, email, password)
    await permissions_service.grant_role(session, user.id, company_id, role_code)
    return user.id


async def create_role_without_permissions(session: AsyncSession, code: str) -> None:
    """A role deliberately granted zero permissions — for testing the

    `require_permission("session.manage")` denial path, distinct from the
    seeded `admin` role which always has it (migration 0009).
    """
    session.add(Role(code=code, name=code.title()))
    await session.commit()
