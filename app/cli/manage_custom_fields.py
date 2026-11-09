"""CLI tool for managing tenant custom field definitions (ADR-011 Action Item 3).

Custom field definition and maintenance are tenant-scoped administrative operations
governed by `platform.customfield.read` and `platform.customfield.manage` permissions.
This CLI binds `CUSTOM_FIELD_ADMIN_ACTOR` with the required permissions and calls into
`app.platform.customfields.service`, enforcing permissions, company locking, and
runtime schema validation rules.

Entry points:
    `uv run python -m app.cli.manage_custom_fields list --company-code <CODE>`
        `[--entity-type <TYPE>] [--active-only]`
    `uv run python -m app.cli.manage_custom_fields get --company-code <CODE> --field-id <UUID>`
    `uv run python -m app.cli.manage_custom_fields define --company-code <CODE>`
        `--entity-type <TYPE> --key <KEY> --label <LABEL> --type <TYPE>`
        `[--required] [--default-value '<JSON>'] [--rules '<JSON>']`
    `uv run python -m app.cli.manage_custom_fields update --company-code <CODE> --field-id <UUID>`
        `[--label <LABEL>] [--required|--not-required]`
        `[--default-value '<JSON>'|--clear-default-value] [--rules '<JSON>'] [--active|--inactive]`
    `uv run python -m app.cli.manage_custom_fields deactivate --company-code <CODE>`
        `--field-id <UUID>`
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

# Side-effecting import: wires composition root and custom data validator
from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.customfields import CustomFieldEntityType, CustomFieldType
from app.core.db import dispose_engine, get_session_factory
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.tenancy import company_context
from app.modules.masterdata import service as masterdata_service
from app.platform.customfields import service as customfields_service
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionRead,
    CustomFieldDefinitionUpdate,
)

logger = logging.getLogger("app.cli.manage_custom_fields")

CUSTOM_FIELD_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000006"),
    permission_codes=frozenset({"platform.customfield.read", "platform.customfield.manage"}),
    is_system_actor=True,
)


def _validate_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DomainValidationError(f"{field_name} must not be empty.")
    return cleaned


def _parse_uuid(value: str | uuid.UUID, *, field_name: str = "field_id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError) as exc:
        raise DomainValidationError(f"Invalid UUID for {field_name}: {value!r}") from exc


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


async def list_custom_fields(
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    entity_type: str | CustomFieldEntityType | None = None,
    active_only: bool = False,
    session: AsyncSession | None = None,
) -> list[CustomFieldDefinitionRead]:
    """List custom field definitions for a company."""
    parsed_entity: str | CustomFieldEntityType | None = (
        entity_type.strip().lower() if isinstance(entity_type, str) else entity_type
    )

    async def _run(s: AsyncSession) -> list[CustomFieldDefinitionRead]:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(CUSTOM_FIELD_ADMIN_ACTOR), company_context(cid):
            return await customfields_service.list_custom_field_definitions(
                s,
                entity_type=parsed_entity,
                active_only=active_only,
            )

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def get_custom_field(
    field_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CustomFieldDefinitionRead:
    """Retrieve a single custom field definition by ID."""
    fid = _parse_uuid(field_id)

    async def _run(s: AsyncSession) -> CustomFieldDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(CUSTOM_FIELD_ADMIN_ACTOR), company_context(cid):
            return await customfields_service.get_custom_field_definition(s, fid)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def define_custom_field(
    data: CustomFieldDefinitionCreate | dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CustomFieldDefinitionRead:
    """Create a new custom field definition for a company."""
    payload = (
        data
        if isinstance(data, CustomFieldDefinitionCreate)
        else CustomFieldDefinitionCreate.model_validate(data)
    )

    async def _run(s: AsyncSession) -> CustomFieldDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(CUSTOM_FIELD_ADMIN_ACTOR), company_context(cid):
            return await customfields_service.create_custom_field_definition(s, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def update_custom_field(
    field_id: uuid.UUID | str,
    data: CustomFieldDefinitionUpdate | dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CustomFieldDefinitionRead:
    """Update an existing custom field definition."""
    fid = _parse_uuid(field_id)
    payload = (
        data
        if isinstance(data, CustomFieldDefinitionUpdate)
        else CustomFieldDefinitionUpdate.model_validate(data)
    )

    async def _run(s: AsyncSession) -> CustomFieldDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(CUSTOM_FIELD_ADMIN_ACTOR), company_context(cid):
            return await customfields_service.update_custom_field_definition(s, fid, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def deactivate_custom_field(
    field_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> CustomFieldDefinitionRead:
    """Deactivate (soft delete) a custom field definition."""
    fid = _parse_uuid(field_id)

    async def _run(s: AsyncSession) -> CustomFieldDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(CUSTOM_FIELD_ADMIN_ACTOR), company_context(cid):
            return await customfields_service.deactivate_custom_field_definition(s, fid)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.manage_custom_fields",
        description="Tenant Custom Field Definitions Administrative CLI Tool",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. list
    p_list = subparsers.add_parser("list", help="List custom field definitions for a company")
    p_list.add_argument("--company-code", type=str, help="Target company code")
    p_list.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_list.add_argument(
        "--entity-type",
        type=str,
        choices=[e.value for e in CustomFieldEntityType],
        help="Filter by business entity type",
    )
    p_list.add_argument(
        "--active-only",
        action="store_true",
        help="Filter active definitions only",
    )

    # 2. get
    p_get = subparsers.add_parser("get", help="Retrieve details of a single custom field")
    p_get.add_argument("--company-code", type=str, help="Target company code")
    p_get.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_get.add_argument("--field-id", type=str, required=True, help="Custom field definition UUID")

    # 3. define
    p_def = subparsers.add_parser("define", help="Define a new custom field for a company")
    p_def.add_argument("--company-code", type=str, help="Target company code")
    p_def.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_def.add_argument(
        "--entity-type",
        type=str,
        required=True,
        choices=[e.value for e in CustomFieldEntityType],
        help="Target business entity type",
    )
    p_def.add_argument("--key", type=str, required=True, help="Canonical field key name")
    p_def.add_argument("--label", type=str, required=True, help="Human-readable label")
    p_def.add_argument(
        "--type",
        type=str,
        required=True,
        choices=[t.value for t in CustomFieldType],
        help="Custom field data type",
    )
    p_def.add_argument("--required", action="store_true", help="Mark field as mandatory")
    p_def.add_argument("--default-value", type=str, help="Default value as JSON string")
    p_def.add_argument("--rules", type=str, help="Validation rules as JSON object string")

    # 4. update
    p_upd = subparsers.add_parser("update", help="Update an existing custom field definition")
    p_upd.add_argument("--company-code", type=str, help="Target company code")
    p_upd.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_upd.add_argument("--field-id", type=str, required=True, help="Custom field definition UUID")
    p_upd.add_argument("--label", type=str, help="Updated label")

    req_group = p_upd.add_mutually_exclusive_group()
    req_group.add_argument("--required", dest="is_required", action="store_true", default=None)
    req_group.add_argument("--not-required", dest="is_required", action="store_false", default=None)

    def_group = p_upd.add_mutually_exclusive_group()
    def_group.add_argument("--default-value", type=str, help="Updated default value as JSON string")
    def_group.add_argument(
        "--clear-default-value", action="store_true", help="Clear default value to null"
    )

    p_upd.add_argument("--rules", type=str, help="Updated validation rules as JSON object string")

    act_group = p_upd.add_mutually_exclusive_group()
    act_group.add_argument("--active", dest="is_active", action="store_true", default=None)
    act_group.add_argument("--inactive", dest="is_active", action="store_false", default=None)

    # 5. deactivate
    p_deact = subparsers.add_parser("deactivate", help="Deactivate a custom field definition")
    p_deact.add_argument("--company-code", type=str, help="Target company code")
    p_deact.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_deact.add_argument("--field-id", type=str, required=True, help="Custom field definition UUID")

    return parser


def _parse_json_value(raw: str, *, field_name: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainValidationError(f"Invalid JSON for {field_name}: {exc}") from exc


async def _handle_list(args: argparse.Namespace) -> None:
    items = await list_custom_fields(
        company_code=args.company_code,
        company_id=args.company_id,
        entity_type=args.entity_type,
        active_only=args.active_only,
    )
    print(f"Custom Fields ({len(items)}):")
    for item in items:
        act_str = "ACTIVE" if item.is_active else "INACTIVE"
        req_str = "REQUIRED" if item.is_required else "OPTIONAL"
        print(
            f"  - [{item.id}] {item.entity_type}.{item.field_key} ({item.field_type}) "
            f"[{act_str}|{req_str}]: label={item.label!r} default={item.default_value!r}"
        )


async def _handle_get(args: argparse.Namespace) -> None:
    res = await get_custom_field(
        args.field_id,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    act_str = "ACTIVE" if res.is_active else "INACTIVE"
    req_str = "REQUIRED" if res.is_required else "OPTIONAL"
    print(
        f"Field ID: {res.id}\n"
        f"Entity: {res.entity_type}\n"
        f"Key: {res.field_key}\n"
        f"Label: {res.label}\n"
        f"Type: {res.field_type}\n"
        f"Status: {act_str} | {req_str}\n"
        f"Default: {json.dumps(res.default_value)}\n"
        f"Validation Rules: {json.dumps(res.validation_rules)}"
    )


async def _handle_define(args: argparse.Namespace) -> None:
    rules_val = (
        _parse_json_value(args.rules, field_name="--rules") if args.rules is not None else {}
    )
    if not isinstance(rules_val, dict):
        raise DomainValidationError("--rules must be a JSON object.")
    default_val = (
        _parse_json_value(args.default_value, field_name="--default-value")
        if args.default_value is not None
        else None
    )
    payload_create = CustomFieldDefinitionCreate(
        entity_type=args.entity_type,
        field_key=args.key,
        label=args.label,
        field_type=args.type,
        is_required=args.required,
        default_value=default_val,
        validation_rules=rules_val,
    )
    res = await define_custom_field(
        payload_create,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Defined custom field '{res.field_key}' (ID: {res.id}) for entity '{res.entity_type}'")


async def _handle_update(args: argparse.Namespace) -> None:
    update_kwargs: dict[str, Any] = {}
    if args.label is not None:
        update_kwargs["label"] = args.label
    if args.is_required is not None:
        update_kwargs["is_required"] = args.is_required
    if args.is_active is not None:
        update_kwargs["is_active"] = args.is_active
    if args.rules is not None:
        rules_val = _parse_json_value(args.rules, field_name="--rules")
        if not isinstance(rules_val, dict):
            raise DomainValidationError("--rules must be a JSON object.")
        update_kwargs["validation_rules"] = rules_val
    if args.clear_default_value:
        update_kwargs["default_value"] = None
    elif args.default_value is not None:
        update_kwargs["default_value"] = _parse_json_value(
            args.default_value, field_name="--default-value"
        )

    payload_update = CustomFieldDefinitionUpdate(**update_kwargs)
    res = await update_custom_field(
        args.field_id,
        payload_update,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Updated custom field '{res.field_key}' (ID: {res.id})")


async def _handle_deactivate(args: argparse.Namespace) -> None:
    res = await deactivate_custom_field(
        args.field_id,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Deactivated custom field '{res.field_key}' (ID: {res.id})")


async def _main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    handlers = {
        "list": _handle_list,
        "get": _handle_get,
        "define": _handle_define,
        "update": _handle_update,
        "deactivate": _handle_deactivate,
    }

    try:
        handler = handlers.get(args.subcommand)
        if handler is None:
            print(f"Unknown subcommand: {args.subcommand}", file=sys.stderr)
            return 1
        await handler(args)
    except (
        ConflictError,
        DomainValidationError,
        ValidationError,
        NotFoundError,
        ActionDeniedError,
        ValueError,
    ) as exc:
        print(f"manage_custom_fields error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dispose:
            await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
