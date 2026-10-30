"""CLI tool for managing companies / tenant root (ADR-009 Decision 12).

Company creation is an administrative operation outside any tenant scope and
outside regular HTTP API traffic. This CLI binds `COMPANY_ADMIN_ACTOR` with the
minimal required permission (`masterdata.company.create`) and calls into
`masterdata.service.create_company` for real, enforcing `require_permission()`
at the service boundary.

Entry point:
    `uv run python -m app.cli.manage_companies --code <CODE> --name <NAME>`
    `[--functional-currency-code TWD]`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import ActorContext, actor_context
from app.core.db import dispose_engine, get_session_factory
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import Company
from app.modules.masterdata.schemas import CompanyCreate

logger = logging.getLogger("app.cli.manage_companies")

COMPANY_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
    permission_codes=frozenset({"masterdata.company.create"}),
    is_system_actor=True,
)


def _validate_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DomainValidationError(f"{field_name} must not be empty.")
    return cleaned


async def create_company(
    code: str,
    name: str,
    functional_currency_code: str = "TWD",
    *,
    session: AsyncSession | None = None,
) -> Company:
    cleaned_code = _validate_non_empty(code, field_name="Company code")
    cleaned_name = _validate_non_empty(name, field_name="Company name")
    payload = CompanyCreate(
        code=cleaned_code,
        name=cleaned_name,
        functional_currency_code=functional_currency_code.strip(),
    )
    with actor_context(COMPANY_ADMIN_ACTOR):
        if session is not None:
            return await masterdata_service.create_company(session, payload)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            return await masterdata_service.create_company(db_session, payload)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a company (tenant root).")
    # Support optional `create-company` subcommand or direct flags:
    effective_argv = argv
    if effective_argv and effective_argv[0] == "create-company":
        effective_argv = effective_argv[1:]

    parser.add_argument("--code", required=True, help="Unique company code, e.g. ACME.")
    parser.add_argument("--name", required=True, help="Company legal or display name.")
    parser.add_argument(
        "--functional-currency-code",
        "--functional-currency",
        dest="functional_currency_code",
        default="TWD",
        help="Company functional currency code (Phase 1 supports TWD only; default: TWD).",
    )
    return parser.parse_args(effective_argv)


async def _main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    try:
        args = _parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    try:
        company = await create_company(
            code=args.code,
            name=args.name,
            functional_currency_code=args.functional_currency_code,
        )
        print(
            f"Created Company: id={company.id} code={company.code} "
            f"name={company.name} functional_currency={company.functional_currency_code}"
        )
    except (
        ConflictError,
        DomainValidationError,
        ValidationError,
        NotFoundError,
        ActionDeniedError,
        ValueError,
    ) as exc:
        print(f"manage_companies error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dispose:
            await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
