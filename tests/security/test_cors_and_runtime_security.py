"""CORS fail-closed boundaries and runtime security validation tests (ADR-018)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.core.settings import get_settings


def test_runtime_security_rejects_missing_keys_and_unsafe_cors_in_staging_and_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_key = Fernet.generate_key().decode("ascii")

    # 1. Missing encryption keys in staging/production -> fail-closed
    for env_name in ("staging", "production"):
        monkeypatch.setenv("APP_ENVIRONMENT", env_name)
        monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", "")
        monkeypatch.setenv("MINI_ERP_CORS_ALLOWED_ORIGINS", "[]")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="MINI_ERP_SECRET_ENCRYPTION_KEYS"):
            get_settings().validate_runtime_security()

    # 2. Unsafe CORS origins in production -> fail-closed
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", valid_key)
    for bad_origin in (
        "*",
        "null",
        "http://erp.example.com",
        "https://user:pass@erp.example.com",
        "https://erp.example.com/subpath",
        "https://*.example.com",
        "https://:8000",
        "https://erp.example.com:notaport",
        "ftp://erp.example.com",
    ):
        monkeypatch.setenv("MINI_ERP_CORS_ALLOWED_ORIGINS", f'["{bad_origin}"]')
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            get_settings().validate_runtime_security()

    # 3. Wildcard + allow_credentials=True forbidden even in development
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    monkeypatch.setenv("MINI_ERP_CORS_ALLOWED_ORIGINS", '["*"]')
    monkeypatch.setenv("MINI_ERP_CORS_ALLOW_CREDENTIALS", "true")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="cors_allow_credentials"):
        get_settings().validate_runtime_security()

    # 4. Valid HTTPS origin + valid Fernet key passes in production
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", valid_key)
    monkeypatch.setenv("MINI_ERP_CORS_ALLOWED_ORIGINS", "https://erp.example.com/")
    monkeypatch.setenv("MINI_ERP_CORS_ALLOW_CREDENTIALS", "false")
    get_settings.cache_clear()
    settings = get_settings()
    settings.validate_runtime_security()
    assert settings.cors_allowed_origins == ["https://erp.example.com"]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_default_app_disables_cors_headers(client: AsyncClient) -> None:
    preflight = await client.options(
        "/api/v1/customers",
        headers={
            "Origin": "https://erp.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers

    health_resp = await client.get("/health", headers={"Origin": "https://erp.example.com"})
    assert health_resp.status_code == 200
    assert "access-control-allow-origin" not in health_resp.headers


@pytest.mark.asyncio
async def test_configured_cors_allows_only_whitelisted_origins_and_wraps_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Depends, FastAPI

    from app import main as app_main
    from app.core.authorization import Authenticated, Public

    valid_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", valid_key)
    monkeypatch.setenv("MINI_ERP_CORS_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("MINI_ERP_CORS_ALLOW_CREDENTIALS", "false")
    get_settings.cache_clear()

    try:
        test_app = FastAPI()

        @test_app.get("/health", dependencies=[Depends(Public())])
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

        @test_app.get("/auth/protected", dependencies=[Depends(Authenticated())])
        async def _protected() -> dict[str, str]:
            return {"ok": "yes"}

        test_app.middleware("http")(app_main.authentication_middleware)
        app_main._configure_cors_middleware(test_app)
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            # Allowed preflight
            ok_preflight = await ac.options(
                "/health",
                headers={
                    "Origin": "https://erp.example.com",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization,Content-Type",
                },
            )
            assert ok_preflight.status_code == 200
            assert ok_preflight.headers["access-control-allow-origin"] == "https://erp.example.com"
            assert "access-control-allow-credentials" not in ok_preflight.headers

            # Unauthenticated protected request -> 401 with BOTH no-store AND CORS header
            unauth_ok_origin = await ac.get(
                "/auth/protected",
                headers={"Origin": "https://erp.example.com"},
            )
            assert unauth_ok_origin.status_code == 401
            assert (
                unauth_ok_origin.headers["access-control-allow-origin"] == "https://erp.example.com"
            )
            assert unauth_ok_origin.headers["cache-control"] == "no-store"

            # Disallowed origin gets no Access-Control-Allow-Origin on preflight OR 401
            bad_preflight = await ac.options(
                "/health",
                headers={
                    "Origin": "https://evil.attacker.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in bad_preflight.headers

            unauth_bad_origin = await ac.get(
                "/auth/protected",
                headers={"Origin": "https://evil.attacker.com"},
            )
            assert unauth_bad_origin.status_code == 401
            assert "access-control-allow-origin" not in unauth_bad_origin.headers
    finally:
        get_settings.cache_clear()
