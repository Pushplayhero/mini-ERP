"""platform.permissions Pydantic v2 DTOs (ADR-009 Decision 3/8)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)
    # Required only when the user has more than one company entitlement
    # (ADR-009 Decision 8) — validated against `user_company_roles` at
    # login time; a user with exactly one entitlement gets it auto-selected
    # and may omit this.
    company_id: uuid.UUID | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    active_company_id: uuid.UUID


class SwitchCompanyRequest(BaseModel):
    company_id: uuid.UUID


class SwitchCompanyResponse(BaseModel):
    active_company_id: uuid.UUID
