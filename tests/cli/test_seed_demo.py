"""tests/cli/test_seed_demo.py — production guard tests for seed_demo."""

from __future__ import annotations

import pytest

from app.cli.seed_demo import _main, seed_demo
from app.core.settings import get_settings


@pytest.mark.asyncio
async def test_seed_demo_rejects_production_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="is disabled in production"):
            await seed_demo(allow_production=False)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_seed_demo_main_cli_handles_production_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        exit_code = await _main([])
        assert exit_code == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_seed_demo_production_guard_allows_via_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_SEED_DEMO_IN_PRODUCTION", "1")
    get_settings.cache_clear()
    try:
        # Should not raise "disabled in production"
        # It proceeds into DB connection / execution
        summary = await seed_demo(allow_production=False)
        assert summary.company_id is not None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_seed_demo_production_guard_rejects_invalid_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_SEED_DEMO_IN_PRODUCTION", "0")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="is disabled in production"):
            await seed_demo(allow_production=False)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_seed_demo_fails_closed_on_mismatched_company_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set a target company code that does not exist when DB already has other companies
    summary = await seed_demo()
    assert summary.company_id is not None

    with pytest.raises(RuntimeError, match="target company 'NON_EXISTENT_CO' not found"):
        await seed_demo(company_code="NON_EXISTENT_CO")
