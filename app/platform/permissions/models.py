"""platform.permissions SQLAlchemy models — maps migration 0009's six tables.

`User`/`Role`/`Permission` are global catalogs (no `TenantScopedMixin`),
mirroring `masterdata`'s own treatment of `Uom`/`Currency`. `UserCompanyRole`
(the entitlement table) and `Session` are deliberately ALSO not
`TenantScopedMixin` — ADR-009 Decision 1's "Options Considered" entry:
entitlement resolution determines *which* company to bind, so it must be
queryable before any company context exists, the same reasoning that keeps
`Company` itself un-tenant-scoped.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Case-insensitive uniqueness is enforced by migration 0009's functional
    # index on `lower(email)`, not by a column-level UNIQUE constraint — no
    # ORM-level equivalent is declared here to avoid claiming a second,
    # weaker (case-sensitive) uniqueness guarantee that doesn't match reality.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Role(Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Permission(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("roles.code", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        String(128), ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True
    )


class UserCompanyRole(Base):
    """The entitlement table: which user may act as which company, under which role."""

    __tablename__ = "user_company_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "company_id", "role_code", name="uq_user_company_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("roles.code", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Session(Base):
    """An opaque bearer session (ADR-009 Decision 3).

    `token_hash` is the SHA-256 hex digest of the raw token handed to the
    client at login — never the raw token itself, and never Argon2id-hashed
    (a fast, deterministic hash is required for the indexed
    `WHERE token_hash = ?` lookup every authenticated request performs;
    Argon2id's deliberate slowness defends low-entropy *passwords*, which a
    256-bit random token does not need).
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    active_company_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
