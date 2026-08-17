"""Application settings, loaded from environment variables / .env.

See `.env.example` for the full list of variables a deployment must set.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "mini-erp"
    environment: str = "development"
    debug: bool = False

    # Postgres connection. Async driver (asyncpg) is mandatory — see
    # open-erp-master-plan.md §2/§10: production is always Postgres, never SQLite.
    database_url: str = "postgresql+asyncpg://mini_erp:mini_erp@localhost:5432/mini_erp"
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Name of the request header a trusted upstream (reverse proxy / auth
    # middleware) sets to carry the authenticated user's active company.
    # Phase 1 has no auth/RBAC yet (that's Phase 2 — platform.permissions), so
    # this header is the deliberate, documented stand-in: it plays the same
    # role a verified JWT claim will play later, and is never sourced from a
    # request *body* field a client could spoof into a write payload.
    tenant_header_name: str = "X-Company-Id"

    # ADR-009 Decision 3 / WEEK9 Slice B: how long an opaque bearer session
    # token stays valid after `POST /auth/login`. Not an ADR-009-pinned
    # number — an ordinary implementation parameter, configurable per
    # deployment. `switch-company`/every authenticated request never
    # extends it (no sliding expiry) — a session's lifetime is fixed at
    # creation; the user must log in again once it lapses.
    session_ttl_hours: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
