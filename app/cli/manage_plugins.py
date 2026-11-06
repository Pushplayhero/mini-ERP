"""CLI tool for managing tenant plugins and configurations (ADR-010 Action Item 3).

Plugin enablement and configuration are tenant-scoped administrative operations
governed by `platform.plugin.read` and `platform.plugin.manage` permissions.
This CLI binds `PLUGIN_ADMIN_ACTOR` with the required permissions and calls into
`app.platform.plugins.service`, enforcing permissions, company locking, and
runtime dependency invariants.

Entry points:
    `uv run python -m app.cli.manage_plugins list --company-code <CODE>`
    `uv run python -m app.cli.manage_plugins get --company-code <CODE> --plugin <NAME>`
    `uv run python -m app.cli.manage_plugins enable --company-code <CODE>`
    `    --plugin <NAME> [--config '<JSON>']`
    `uv run python -m app.cli.manage_plugins disable --company-code <CODE> --plugin <NAME>`
    `uv run python -m app.cli.manage_plugins configure --company-code <CODE>`
    `    --plugin <NAME> --config '<JSON>' [--enable|--disable]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# Side-effecting import: wires composition root and bootstraps built-in plugins
from app import main as _app_main
from app.core.authorization import ActorContext, actor_context
from app.core.db import dispose_engine, get_session_factory
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.plugins import PluginLoadError, get_plugin_runtime_gate, set_plugin_runtime_gate
from app.core.tenancy import company_context
from app.modules.masterdata import service as masterdata_service
from app.platform.plugins import loader
from app.platform.plugins import service as plugins_service
from app.platform.plugins.schemas import CompanyPluginRead, CompanyPluginUpdate

logger = logging.getLogger("app.cli.manage_plugins")

PLUGIN_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
    permission_codes=frozenset({"platform.plugin.read", "platform.plugin.manage"}),
    is_system_actor=True,
)


def _validate_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DomainValidationError(f"{field_name} must not be empty.")
    return cleaned


def _ensure_plugins_loaded() -> None:
    if get_plugin_runtime_gate() is None:
        set_plugin_runtime_gate(plugins_service.execute_with_plugin_gate_unchecked)
    if not loader.get_loaded_plugins():
        loader.bootstrap_plugins(_app_main.BUILTIN_PLUGIN_MANIFESTS)


async def _resolve_company_id(
    session: AsyncSession,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if company_code is None and company_id is None:
        raise DomainValidationError("Either company_code or company_id must be provided.")

    cleaned_code = (
        _validate_non_empty(company_code, field_name="Company code")
        if company_code is not None
        else None
    )
    if company_id is not None:
        company = await masterdata_service.load_company_unchecked(session, company_id)
        if cleaned_code is not None and company.code != cleaned_code:
            raise DomainValidationError(
                f"Company ID {company_id} has code {company.code!r}, which does not match "
                f"provided code {cleaned_code!r}."
            )
        return company.id

    assert cleaned_code is not None
    companies = await masterdata_service.list_companies_unchecked(session)
    for c in companies:
        if c.code == cleaned_code:
            return c.id
    raise NotFoundError("Company", cleaned_code)


async def list_plugins(
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> list[CompanyPluginRead]:
    """List all loaded plugins with their effective status for a company."""
    _ensure_plugins_loaded()

    async def _run(s: AsyncSession) -> list[CompanyPluginRead]:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(PLUGIN_ADMIN_ACTOR), company_context(cid):
            return await plugins_service.list_company_plugins(s)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def get_plugin(
    plugin_name: str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CompanyPluginRead:
    """Retrieve details and effective status for a single plugin."""
    _ensure_plugins_loaded()

    cleaned_plugin = _validate_non_empty(plugin_name, field_name="Plugin name")

    async def _run(s: AsyncSession) -> CompanyPluginRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(PLUGIN_ADMIN_ACTOR), company_context(cid):
            return await plugins_service.get_company_plugin(s, cleaned_plugin)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def enable_plugin(
    plugin_name: str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    config: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> CompanyPluginRead:
    """Enable a plugin for a company, optionally supplying updated configuration."""
    _ensure_plugins_loaded()

    cleaned_plugin = _validate_non_empty(plugin_name, field_name="Plugin name")

    async def _run(s: AsyncSession) -> CompanyPluginRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        payload = CompanyPluginUpdate(is_enabled=True, config=config)
        with actor_context(PLUGIN_ADMIN_ACTOR), company_context(cid):
            return await plugins_service.update_company_plugin(s, cleaned_plugin, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def disable_plugin(
    plugin_name: str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CompanyPluginRead:
    """Disable a plugin for a company."""
    _ensure_plugins_loaded()

    cleaned_plugin = _validate_non_empty(plugin_name, field_name="Plugin name")

    async def _run(s: AsyncSession) -> CompanyPluginRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        payload = CompanyPluginUpdate(is_enabled=False)
        with actor_context(PLUGIN_ADMIN_ACTOR), company_context(cid):
            return await plugins_service.update_company_plugin(s, cleaned_plugin, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def configure_plugin(
    plugin_name: str,
    config: dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    is_enabled: bool | None = None,
    session: AsyncSession | None = None,
) -> CompanyPluginRead:
    """Update configuration for a plugin, optionally toggling enablement."""
    _ensure_plugins_loaded()

    cleaned_plugin = _validate_non_empty(plugin_name, field_name="Plugin name")

    async def _run(s: AsyncSession) -> CompanyPluginRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        payload = CompanyPluginUpdate(is_enabled=is_enabled, config=config)
        with actor_context(PLUGIN_ADMIN_ACTOR), company_context(cid):
            return await plugins_service.update_company_plugin(s, cleaned_plugin, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


def _parse_json_config(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise DomainValidationError(f"Invalid JSON configuration: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DomainValidationError("Configuration JSON must be an object (dict).")
    return parsed


def _add_company_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--company-code", help="Target company code, e.g. MAIN.")
    parser.add_argument(
        "--company-id",
        type=uuid.UUID,
        help="Target company UUID, e.g. 12345678-1234-5678-1234-567812345678.",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage tenant plugin enablement and configuration."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List all loaded plugins for a company.")
    _add_company_args(p_list)

    # get
    p_get = subparsers.add_parser("get", help="Get effective status of a single plugin.")
    _add_company_args(p_get)
    p_get.add_argument("--plugin", required=True, help="Plugin name, e.g. credit_limit.")

    # enable
    p_enable = subparsers.add_parser("enable", help="Enable a plugin for a company.")
    _add_company_args(p_enable)
    p_enable.add_argument("--plugin", required=True, help="Plugin name, e.g. credit_limit.")
    p_enable.add_argument("--config", help="Optional JSON configuration string.")

    # disable
    p_disable = subparsers.add_parser("disable", help="Disable a plugin for a company.")
    _add_company_args(p_disable)
    p_disable.add_argument("--plugin", required=True, help="Plugin name, e.g. credit_limit.")

    # configure
    p_config = subparsers.add_parser("configure", help="Update plugin configuration.")
    _add_company_args(p_config)
    p_config.add_argument("--plugin", required=True, help="Plugin name, e.g. credit_limit.")
    p_config.add_argument("--config", required=True, help="JSON configuration string.")
    toggle_group = p_config.add_mutually_exclusive_group()
    toggle_group.add_argument(
        "--enable", dest="toggle_enable", action="store_true", default=None, help="Also enable."
    )
    toggle_group.add_argument(
        "--disable", dest="toggle_enable", action="store_false", help="Also disable."
    )

    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    try:
        args = _parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    try:
        config_dict = _parse_json_config(getattr(args, "config", None))
        if args.subcommand == "list":
            items = await list_plugins(
                company_code=args.company_code,
                company_id=args.company_id,
            )
            print(f"Plugins ({len(items)}):")
            for item in items:
                status_str = "ENABLED" if item.is_enabled else "DISABLED"
                custom_str = " (customized)" if item.is_customized else " (default)"
                print(
                    f"  - {item.plugin_name} v{item.version} [{status_str}]{custom_str}: "
                    f"config={item.config}"
                )
        elif args.subcommand == "get":
            res = await get_plugin(
                args.plugin,
                company_code=args.company_code,
                company_id=args.company_id,
            )
            status_str = "ENABLED" if res.is_enabled else "DISABLED"
            custom_str = "customized" if res.is_customized else "default"
            print(
                f"Plugin: {res.plugin_name} v{res.version}\n"
                f"Title: {res.title}\n"
                f"Status: {status_str} ({custom_str})\n"
                f"Requires: {[d.plugin_name for d in res.requires]}\n"
                f"Config: {json.dumps(res.config)}"
            )
        elif args.subcommand == "enable":
            res = await enable_plugin(
                args.plugin,
                company_code=args.company_code,
                company_id=args.company_id,
                config=config_dict,
            )
            print(
                f"Enabled plugin '{res.plugin_name}': "
                f"is_enabled={res.is_enabled} config={json.dumps(res.config)}"
            )
        elif args.subcommand == "disable":
            res = await disable_plugin(
                args.plugin,
                company_code=args.company_code,
                company_id=args.company_id,
            )
            print(f"Disabled plugin '{res.plugin_name}': is_enabled={res.is_enabled}")
        elif args.subcommand == "configure":
            if config_dict is None:
                raise DomainValidationError("--config must provide non-empty JSON object.")
            res = await configure_plugin(
                args.plugin,
                config_dict,
                company_code=args.company_code,
                company_id=args.company_id,
                is_enabled=args.toggle_enable,
            )
            print(
                f"Configured plugin '{res.plugin_name}': "
                f"is_enabled={res.is_enabled} config={json.dumps(res.config)}"
            )
        else:
            print(f"Unknown subcommand: {args.subcommand}", file=sys.stderr)
            return 1
    except (
        ConflictError,
        DomainValidationError,
        ValidationError,
        NotFoundError,
        ActionDeniedError,
        PluginLoadError,
        ValueError,
    ) as exc:
        print(f"manage_plugins error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dispose:
            await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
