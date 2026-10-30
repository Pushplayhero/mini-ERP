"""CLI tool for managing global reference data (ADR-009 Decision 11).

Global reference-data writes (UoM, UoM conversions, currencies, exchange rates)
are administrative operations outside any tenant scope and outside regular HTTP
API traffic. This CLI binds `REFERENCE_DATA_ADMIN_ACTOR` with the minimal
required permissions and calls into `masterdata.service` for real, enforcing
`require_permission()` at the service boundary.

Entry point: `uv run python -m app.cli.manage_reference_data <subcommand> [options]`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

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
from app.modules.masterdata.models import Currency, ExchangeRate, Uom, UomConversion
from app.modules.masterdata.schemas import (
    CurrencyCreate,
    ExchangeRateCreate,
    UomConversionCreate,
    UomCreate,
)

logger = logging.getLogger("app.cli.manage_reference_data")

_FACTOR_QUANTUM = Decimal("0.000001")  # NUMERIC(20, 6)
_RATE_QUANTUM = Decimal("0.0000000001")  # NUMERIC(20, 10)

REFERENCE_DATA_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
    permission_codes=frozenset(
        {
            "masterdata.uom.create",
            "masterdata.uom_conversion.create",
            "masterdata.currency.create",
            "masterdata.exchange_rate.create",
        }
    ),
    is_system_actor=True,
)


def _validate_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DomainValidationError(f"{field_name} must not be empty.")
    return cleaned


def _quantize_positive_decimal(value: Decimal, *, quantum: Decimal, field_name: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise DomainValidationError(f"{field_name} must be finite and greater than zero.")
    quantized = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    if quantized <= 0:
        raise DomainValidationError(
            f"{field_name} must remain greater than zero after rounding to {quantum}."
        )
    return quantized


def _parse_positive_decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {raw!r}") from exc
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(
            f"decimal value must be finite and greater than zero: {raw!r}"
        )
    return value


async def create_uom(
    code: str,
    name: str,
    *,
    session: AsyncSession | None = None,
) -> Uom:
    cleaned_code = _validate_non_empty(code, field_name="UoM code")
    cleaned_name = _validate_non_empty(name, field_name="UoM name")
    payload = UomCreate(code=cleaned_code, name=cleaned_name)
    with actor_context(REFERENCE_DATA_ADMIN_ACTOR):
        if session is not None:
            return await masterdata_service.create_uom(session, payload)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            return await masterdata_service.create_uom(db_session, payload)


async def create_uom_conversion(
    from_uom_id: uuid.UUID,
    to_uom_id: uuid.UUID,
    factor: Decimal,
    *,
    session: AsyncSession | None = None,
) -> UomConversion:
    rounded_factor = _quantize_positive_decimal(
        factor, quantum=_FACTOR_QUANTUM, field_name="UoM conversion factor"
    )
    payload = UomConversionCreate(
        from_uom_id=from_uom_id,
        to_uom_id=to_uom_id,
        factor=rounded_factor,
    )
    with actor_context(REFERENCE_DATA_ADMIN_ACTOR):
        if session is not None:
            return await masterdata_service.create_uom_conversion(session, payload)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            return await masterdata_service.create_uom_conversion(db_session, payload)


async def create_currency(
    code: str,
    name: str,
    decimal_places: int = 2,
    *,
    session: AsyncSession | None = None,
) -> Currency:
    cleaned_code = _validate_non_empty(code, field_name="Currency code")
    cleaned_name = _validate_non_empty(name, field_name="Currency name")
    if decimal_places < 0 or decimal_places > 6:
        raise DomainValidationError("Currency decimal_places must be between 0 and 6.")
    payload = CurrencyCreate(
        code=cleaned_code,
        name=cleaned_name,
        decimal_places=decimal_places,
    )
    with actor_context(REFERENCE_DATA_ADMIN_ACTOR):
        if session is not None:
            return await masterdata_service.create_currency(session, payload)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            return await masterdata_service.create_currency(db_session, payload)


async def create_exchange_rate(
    from_currency_code: str,
    to_currency_code: str,
    rate: Decimal,
    rate_date: date,
    *,
    session: AsyncSession | None = None,
) -> ExchangeRate:
    cleaned_from = _validate_non_empty(from_currency_code, field_name="from_currency_code")
    cleaned_to = _validate_non_empty(to_currency_code, field_name="to_currency_code")
    rounded_rate = _quantize_positive_decimal(
        rate, quantum=_RATE_QUANTUM, field_name="Exchange rate"
    )
    payload = ExchangeRateCreate(
        from_currency_code=cleaned_from,
        to_currency_code=cleaned_to,
        rate=rounded_rate,
        rate_date=rate_date,
    )
    with actor_context(REFERENCE_DATA_ADMIN_ACTOR):
        if session is not None:
            return await masterdata_service.create_exchange_rate(session, payload)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            return await masterdata_service.create_exchange_rate(db_session, payload)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage global reference data (UoM, currencies, exchange rates)."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # create-uom
    uom_p = subparsers.add_parser("create-uom", help="Create a Unit of Measure.")
    uom_p.add_argument("--code", required=True, help="UoM code, e.g. EA, KG.")
    uom_p.add_argument("--name", required=True, help="UoM descriptive name.")

    # create-uom-conversion
    conv_p = subparsers.add_parser(
        "create-uom-conversion", help="Create a Unit of Measure conversion."
    )
    conv_p.add_argument("--from-uom-id", required=True, type=uuid.UUID, help="Source UoM UUID.")
    conv_p.add_argument("--to-uom-id", required=True, type=uuid.UUID, help="Target UoM UUID.")
    conv_p.add_argument(
        "--factor",
        required=True,
        type=_parse_positive_decimal,
        help="Conversion factor from source to target (> 0).",
    )

    # create-currency
    cur_p = subparsers.add_parser("create-currency", help="Create a global currency.")
    cur_p.add_argument("--code", required=True, help="ISO-4217 3-letter currency code, e.g. TWD.")
    cur_p.add_argument("--name", required=True, help="Currency name.")
    cur_p.add_argument(
        "--decimal-places",
        type=int,
        default=2,
        help="Number of decimal places (default: 2, TWD: 0).",
    )

    # create-exchange-rate
    rate_p = subparsers.add_parser("create-exchange-rate", help="Create an exchange rate.")
    rate_p.add_argument(
        "--from-currency",
        "--from-currency-code",
        dest="from_currency_code",
        required=True,
        help="Source currency code (3 letters).",
    )
    rate_p.add_argument(
        "--to-currency",
        "--to-currency-code",
        dest="to_currency_code",
        required=True,
        help="Target currency code (3 letters).",
    )
    rate_p.add_argument(
        "--rate",
        required=True,
        type=_parse_positive_decimal,
        help="Exchange rate (> 0).",
    )
    rate_p.add_argument(
        "--rate-date",
        "--effective-date",
        dest="rate_date",
        required=True,
        type=date.fromisoformat,
        help="Effective date (YYYY-MM-DD).",
    )

    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    try:
        args = _parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    try:
        if args.subcommand == "create-uom":
            uom = await create_uom(code=args.code, name=args.name)
            print(f"Created UoM: id={uom.id} code={uom.code} name={uom.name}")
        elif args.subcommand == "create-uom-conversion":
            conv = await create_uom_conversion(
                from_uom_id=args.from_uom_id,
                to_uom_id=args.to_uom_id,
                factor=args.factor,
            )
            print(
                f"Created UoM conversion: id={conv.id} "
                f"from={conv.from_uom_id} to={conv.to_uom_id} factor={conv.factor}"
            )
        elif args.subcommand == "create-currency":
            cur = await create_currency(
                code=args.code,
                name=args.name,
                decimal_places=args.decimal_places,
            )
            print(
                f"Created Currency: code={cur.code} name={cur.name} "
                f"decimal_places={cur.decimal_places}"
            )
        elif args.subcommand == "create-exchange-rate":
            rate = await create_exchange_rate(
                from_currency_code=args.from_currency_code,
                to_currency_code=args.to_currency_code,
                rate=args.rate,
                rate_date=args.rate_date,
            )
            print(
                f"Created Exchange Rate: id={rate.id} "
                f"{rate.from_currency_code}->{rate.to_currency_code} "
                f"rate={rate.rate} date={rate.rate_date}"
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
        ValueError,
    ) as exc:
        print(f"manage_reference_data error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dispose:
            await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
