"""Application settings, loaded from environment variables / .env.

See `.env.example` for the full list of variables a deployment must set.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_NON_PRODUCTION_ENVIRONMENTS = frozenset({"development", "dev", "test", "local"})


def _parse_str_list(raw: Any) -> list[str]:
    """Parse list[str] from JSON array, comma-separated text, or sequence."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, tuple | set):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("Expected a JSON list of strings.")
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in stripped.split(",") if part.strip()]
    raise ValueError("Expected a list of strings or comma-separated string.")


def _validate_fernet_key_string(raw_key: str) -> None:
    """Validate that `raw_key` is a canonical 32-byte URL-safe base64 Fernet key."""
    if not raw_key or any(ch.isspace() for ch in raw_key):
        raise ValueError("Fernet encryption key must be non-empty and contain no whitespace.")
    try:
        Fernet(raw_key.encode("ascii"))
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ValueError(
            "Invalid Fernet encryption key in MINI_ERP_SECRET_ENCRYPTION_KEYS."
        ) from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "mini-erp"
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENVIRONMENT", "ENVIRONMENT", "environment"),
    )
    debug: bool = False

    # Postgres connection. Async driver (asyncpg) is mandatory — see
    # open-erp-master-plan.md §2/§10: production is always Postgres, never SQLite.
    database_url: str = "postgresql+asyncpg://mini_erp:mini_erp@localhost:5432/mini_erp"
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # ADR-018: PostgreSQL Row-Level Security (RLS) enforcement for tenant-scoped tables.
    database_enforce_rls: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "MINI_ERP_DATABASE_ENFORCE_RLS",
            "DATABASE_ENFORCE_RLS",
            "database_enforce_rls",
        ),
    )

    # ADR-009 Decision 3 & ADR-018: bearer session absolute TTL, idle timeout,
    # throttled last_active_at touch interval, and active session cap per user.
    session_ttl_hours: int = Field(
        default=12,
        ge=1,
        validation_alias=AliasChoices(
            "MINI_ERP_SESSION_TTL_HOURS",
            "SESSION_TTL_HOURS",
            "session_ttl_hours",
        ),
    )
    session_idle_timeout_minutes: int = Field(
        default=60,
        ge=1,
        validation_alias=AliasChoices(
            "MINI_ERP_SESSION_IDLE_TIMEOUT_MINUTES",
            "SESSION_IDLE_TIMEOUT_MINUTES",
            "session_idle_timeout_minutes",
        ),
    )
    session_touch_interval_seconds: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices(
            "MINI_ERP_SESSION_TOUCH_INTERVAL_SECONDS",
            "SESSION_TOUCH_INTERVAL_SECONDS",
            "session_touch_interval_seconds",
        ),
    )
    session_max_active_per_user: int = Field(
        default=10,
        ge=1,
        validation_alias=AliasChoices(
            "MINI_ERP_SESSION_MAX_ACTIVE_PER_USER",
            "SESSION_MAX_ACTIVE_PER_USER",
            "session_max_active_per_user",
        ),
    )

    # ADR-018 (VULN-06 remediation): comma-separated or JSON array of MultiFernet keys.
    # Primary key is keys[0]; older keys remain in keys[1:] for zero-downtime rotation.
    secret_encryption_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "MINI_ERP_SECRET_ENCRYPTION_KEYS",
            "SECRET_ENCRYPTION_KEYS",
            "secret_encryption_keys",
        ),
    )

    # ADR-018: Fail-closed CORS configuration (disabled by default when list is empty).
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "MINI_ERP_CORS_ALLOWED_ORIGINS",
            "CORS_ALLOWED_ORIGINS",
            "cors_allowed_origins",
        ),
    )
    cors_allow_credentials: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "MINI_ERP_CORS_ALLOW_CREDENTIALS",
            "CORS_ALLOW_CREDENTIALS",
            "cors_allow_credentials",
        ),
    )
    cors_max_age_seconds: int = Field(
        default=600,
        ge=0,
        validation_alias=AliasChoices(
            "MINI_ERP_CORS_MAX_AGE_SECONDS",
            "CORS_MAX_AGE_SECONDS",
            "cors_max_age_seconds",
        ),
    )

    # ADR-010 Decisions 1 & 2 / WEEK10 Slice A: plugin discovery and operator
    # overrides. enable_plugin_entrypoints controls scanning for external
    # python entry points (mini_erp.plugins group). disabled_plugins allows
    # operators to exclude specific discovered plugins at deploy time.
    enable_plugin_entrypoints: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "MINI_ERP_ENABLE_PLUGIN_ENTRYPOINTS",
            "ENABLE_PLUGIN_ENTRYPOINTS",
            "enable_plugin_entrypoints",
        ),
    )
    disabled_plugins: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "MINI_ERP_DISABLED_PLUGINS",
            "DISABLED_PLUGINS",
            "disabled_plugins",
        ),
    )

    # ADR-013 / WEEK13 Slice A: platform.integration settings
    webhook_allow_insecure_http: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "MINI_ERP_WEBHOOK_ALLOW_INSECURE_HTTP",
            "WEBHOOK_ALLOW_INSECURE_HTTP",
            "webhook_allow_insecure_http",
        ),
    )
    webhook_timeout_seconds: float = 10.0
    webhook_max_response_bytes: int = 65536
    webhook_lease_seconds: int = 60
    batch_import_max_rows: int = 2000
    batch_import_max_bytes: int = 1_048_576

    @field_validator("secret_encryption_keys", mode="before")
    @classmethod
    def _parse_secret_encryption_keys(cls, value: Any) -> list[str]:
        keys = _parse_str_list(value)
        for key in keys:
            _validate_fernet_key_string(key)
        return keys

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_allowed_origins(cls, value: Any) -> list[str]:
        origins = _parse_str_list(value)
        normalized: list[str] = []
        for origin in origins:
            cleaned = origin.strip().rstrip("/")
            if cleaned:
                normalized.append(cleaned)
        return normalized

    @property
    def is_production_like(self) -> bool:
        """Return True outside explicit local dev/test environments."""
        return self.environment.strip().lower() not in _NON_PRODUCTION_ENVIRONMENTS

    def validate_runtime_security(self) -> None:
        """Fail-closed startup validation for encryption keys and CORS origins (ADR-018)."""
        for key in self.secret_encryption_keys:
            _validate_fernet_key_string(key)

        if self.is_production_like and not self.secret_encryption_keys:
            raise RuntimeError(
                "MINI_ERP_SECRET_ENCRYPTION_KEYS must be configured when APP_ENVIRONMENT "
                f"is {self.environment!r}."
            )

        for origin in self.cors_allowed_origins:
            if any(ch.isspace() or ord(ch) < 32 for ch in origin):
                raise RuntimeError(f"Invalid CORS origin {origin!r}: contains whitespace/control.")
            if origin.lower() == "null":
                raise RuntimeError("Invalid CORS origin 'null': opaque null origin is forbidden.")
            if origin == "*":
                if self.is_production_like:
                    raise RuntimeError(
                        "Wildcard CORS origin '*' is forbidden in staging/production."
                    )
                if self.cors_allow_credentials:
                    raise RuntimeError(
                        "Wildcard CORS origin '*' cannot be combined with "
                        "cors_allow_credentials=True."
                    )
                continue

            parsed = urlsplit(origin)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
            if scheme not in ("http", "https") or not parsed.netloc or not hostname:
                raise RuntimeError(
                    f"Invalid CORS origin {origin!r}: must be a full http(s) origin."
                )
            if "*" in hostname or hostname.lower() == "null":
                raise RuntimeError(
                    f"Invalid CORS origin {origin!r}: wildcard or null hostname is forbidden."
                )
            try:
                _ = parsed.port
            except ValueError as exc:
                raise RuntimeError(f"Invalid CORS origin {origin!r}: invalid port.") from exc
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise RuntimeError(
                    f"Invalid CORS origin {origin!r}: must not contain path, query, or fragment."
                )
            if parsed.username is not None or parsed.password is not None:
                raise RuntimeError(f"Invalid CORS origin {origin!r}: userinfo is forbidden.")
            if self.is_production_like and scheme != "https":
                raise RuntimeError(
                    f"Insecure HTTP CORS origin {origin!r} is forbidden in staging/production."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
