"""platform.integration batch import and export engine — ADR-013 / Week 13 Slice C.

Implements:
1. Batch import for master data entities (Customer, Product, Account) with savepoint
   row validation, atomic rollback, `dry_run` support, and `customfields` validation.
2. Batch export for all supported business entities via registered Core providers.
3. CSV and JSON format support with zero 3rd-party dependencies.
4. OWASP CSV formula injection neutralization (applied exclusively on CSV export).
5. Service-layer RBAC (`require_permission`) plus byte/row limits failing closed.
6. Execution auditing in `integration_batch_jobs`.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import require_current_actor, require_permission
from app.core.customfields import CustomFieldEntityType, validate_and_normalize_custom_data
from app.core.exceptions import DomainValidationError
from app.core.integration import (
    EXPORT_SUPPORTED_ENTITIES,
    IMPORT_SUPPORTED_ENTITIES,
    BatchDataFormat,
    BatchEntityType,
    BatchImportValidationError,
    UnsupportedBatchEntityError,
    get_export_provider,
)
from app.core.settings import get_settings
from app.core.tenancy import company_context, require_current_company_id
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import Account, Customer, Product, Uom
from app.modules.masterdata.schemas import (
    AccountCreate,
    CustomerCreate,
    ProductCreate,
)
from app.platform.integration.models import IntegrationBatchJob

_STRICT_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$", re.ASCII)
_DANGEROUS_UNQUOTED_PREFIXES = ("=", "+", "-", "@", "|", ";")
_CONTROL_OR_LINE_BREAK_CHARS = ("\t", "\r", "\n", "\v", "\f", "\x85", "\u2028", "\u2029")


def _is_ignorable_edge_char(ch: str) -> bool:
    """Return True for any Unicode whitespace, separator, format, or control char."""
    return ch.isspace() or unicodedata.category(ch) in {"Cf", "Cc", "Zs", "Zl", "Zp"}


def _has_control_or_delim(value: str) -> bool:
    if value[0] in ("|", ";") or unicodedata.category(value[0]) in {"Cf", "Cc"}:
        return True
    return any(ch in value for ch in _CONTROL_OR_LINE_BREAK_CHARS)


def _strip_edge_ignorables(text: str, *, include_quotes: bool = False) -> str:
    start = 0
    end = len(text)
    while start < end and (
        _is_ignorable_edge_char(text[start]) or (include_quotes and text[start] in ('"', "'"))
    ):
        start += 1
    while end > start and _is_ignorable_edge_char(text[end - 1]):
        end -= 1
    return text[start:end]


def neutralize_formula(value: Any) -> Any:
    """Neutralize CSV/spreadsheet formula injection by prefixing an apostrophe."""
    if not isinstance(value, str) or not value:
        return value

    if _has_control_or_delim(value):
        return f"'{value}"

    stripped = _strip_edge_ignorables(value)
    # Exempt only clean ASCII decimal literals (never Inf, NaN, 1_000, 1e5, or full-width digits).
    if not stripped or (value == stripped and _STRICT_NUMERIC_RE.fullmatch(stripped)):
        return value

    unquoted = _strip_edge_ignorables(stripped, include_quotes=True)
    if stripped.startswith(_DANGEROUS_UNQUOTED_PREFIXES) or unquoted.startswith(
        _DANGEROUS_UNQUOTED_PREFIXES
    ):
        return f"'{value}"

    return value


def sanitize_row_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    """Sanitize all values in a dictionary row for CSV output."""
    return {k: neutralize_formula(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Batch Export Engine
# ---------------------------------------------------------------------------


async def export_batch_data(
    session: AsyncSession,
    entity_type: BatchEntityType,
    *,
    data_format: BatchDataFormat = BatchDataFormat.CSV,
    include_bom: bool = False,
) -> tuple[str, IntegrationBatchJob]:
    """Export entity records for the active tenant.

    Returns:
        tuple of (exported_content_string, IntegrationBatchJob_record)
    """
    require_permission("platform.integration.read")
    if entity_type not in EXPORT_SUPPORTED_ENTITIES:
        raise UnsupportedBatchEntityError(
            f"Entity '{entity_type}' is not supported for batch export"
        )

    company_id = require_current_company_id()
    actor = require_current_actor()

    provider = get_export_provider(entity_type)
    if provider is None:
        raise DomainValidationError(
            f"No export provider is registered for entity type '{entity_type}'"
        )

    with company_context(company_id):
        rows = await provider(session, company_id)
    total_rows = len(rows)

    if data_format == BatchDataFormat.CSV:
        buf = io.StringIO()
        if include_bom:
            buf.write("\ufeff")

        if rows:
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(sanitize_row_for_csv(row))
        content = buf.getvalue()
    elif data_format == BatchDataFormat.JSON:
        content = json.dumps(rows, indent=2, default=str)
    else:
        raise DomainValidationError(f"Unsupported export format: {data_format}")

    job = IntegrationBatchJob(
        company_id=company_id,
        job_type="EXPORT",
        entity_type=str(entity_type),
        format=str(data_format),
        status="COMPLETED",
        dry_run=False,
        total_rows=total_rows,
        succeeded_rows=total_rows,
        failed_rows=0,
        error_summary=[],
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return content, job


# ---------------------------------------------------------------------------
# Batch Import Engine
# ---------------------------------------------------------------------------


def _parse_raw_rows(content: str, data_format: BatchDataFormat) -> list[Any]:
    """Parse raw content into a list of raw row entries."""
    if data_format == BatchDataFormat.CSV:
        clean_content = content.lstrip("\ufeff")
        try:
            return [dict(row) for row in csv.DictReader(io.StringIO(clean_content))]
        except csv.Error as exc:
            raise BatchImportValidationError(f"Malformed CSV: {exc}") from exc
    if data_format == BatchDataFormat.JSON:
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, list):
                raise BatchImportValidationError("JSON import must be an array of objects")
            return parsed
        except json.JSONDecodeError as exc:
            raise BatchImportValidationError(f"Malformed JSON: {exc}") from exc
    raise BatchImportValidationError(f"Unsupported format: {data_format}")


def _extract_custom_data(row: dict[str, Any]) -> dict[str, Any]:
    """Extract custom_data from either a JSON object/string or `custom_data.*` CSV keys."""
    custom_data: dict[str, Any] = {}
    prefixed_keys = [k for k in row if isinstance(k, str) and k.startswith("custom_data.")]
    raw_custom = row.get("custom_data")

    if raw_custom is not None:
        if isinstance(raw_custom, dict):
            custom_data.update(raw_custom)
        elif isinstance(raw_custom, str):
            stripped = raw_custom.strip()
            if stripped:
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise DomainValidationError(f"Invalid custom_data JSON: {exc}") from exc
                if not isinstance(decoded, dict):
                    raise DomainValidationError("custom_data must decode to a JSON object")
                custom_data.update(decoded)
        else:
            raise DomainValidationError("custom_data must be a JSON object")

    for key in prefixed_keys:
        val = row.pop(key)
        field_key = key.removeprefix("custom_data.")
        if val != "" and val is not None:
            custom_data[field_key] = val

    row["custom_data"] = custom_data
    return row


async def import_batch_data(
    session: AsyncSession,
    entity_type: BatchEntityType,
    content: str,
    *,
    data_format: BatchDataFormat = BatchDataFormat.CSV,
    dry_run: bool = False,
) -> tuple[IntegrationBatchJob, list[dict[str, Any]]]:
    """Import masterdata records into the active tenant atomically per batch."""
    require_permission("platform.integration.import")
    if entity_type not in IMPORT_SUPPORTED_ENTITIES:
        raise UnsupportedBatchEntityError(
            f"Entity '{entity_type}' is not supported for batch import"
        )

    company_id = require_current_company_id()
    actor = require_current_actor()

    settings = get_settings()
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > settings.batch_import_max_bytes:
        raise BatchImportValidationError(
            f"Content length {len(content_bytes)} bytes exceeds max limit "
            f"{settings.batch_import_max_bytes} bytes"
        )

    raw_rows = _parse_raw_rows(content, data_format)
    if len(raw_rows) > settings.batch_import_max_rows:
        raise BatchImportValidationError(
            f"Row count {len(raw_rows)} exceeds limit {settings.batch_import_max_rows}"
        )

    errors: list[dict[str, Any]] = []
    succeeded_count = 0

    await masterdata_service.lock_company_unchecked(session, company_id)

    for idx, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, dict):
            errors.append(
                {
                    "row": idx,
                    "error": "Row must be an object",
                    "data": {"value": repr(raw_row)},
                }
            )
            continue
        if None in raw_row:
            errors.append(
                {
                    "row": idx,
                    "error": "Row has extra columns beyond CSV header",
                    "data": {str(k): v for k, v in raw_row.items()},
                }
            )
            continue

        row_data = dict(raw_row)
        try:
            async with session.begin_nested():
                prepared = _extract_custom_data(dict(row_data))
                if entity_type == BatchEntityType.CUSTOMER:
                    await _import_customer_row(session, company_id, prepared)
                elif entity_type == BatchEntityType.PRODUCT:
                    await _import_product_row(session, company_id, prepared)
                elif entity_type == BatchEntityType.ACCOUNT:
                    await _import_account_row(session, company_id, prepared)
                succeeded_count += 1
        except Exception as exc:
            errors.append({"row": idx, "error": str(exc), "data": row_data})

    total_rows = len(raw_rows)
    failed_count = len(errors)

    if dry_run or failed_count > 0:
        await session.rollback()
        status = "DRY_RUN" if dry_run and failed_count == 0 else "FAILED"
    else:
        await session.commit()
        status = "COMPLETED"

    job = IntegrationBatchJob(
        company_id=company_id,
        job_type="IMPORT",
        entity_type=str(entity_type),
        format=str(data_format),
        status=status,
        dry_run=dry_run,
        total_rows=total_rows,
        succeeded_rows=succeeded_count if status == "COMPLETED" else 0,
        failed_rows=failed_count,
        error_summary=errors[:100],
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job, errors


async def _import_customer_row(
    session: AsyncSession,
    company_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    if "currency_code" not in data or not data["currency_code"]:
        data["currency_code"] = "TWD"

    payload = CustomerCreate(**data)
    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.CUSTOMER,
        raw_custom_data=payload.custom_data,
        is_create=True,
    )
    customer = Customer(
        company_id=company_id,
        code=payload.code,
        name=payload.name,
        credit_limit=payload.credit_limit,
        currency_code=payload.currency_code,
        payment_terms_days=payload.payment_terms_days,
        is_active=payload.is_active,
        custom_data=normalized_custom_data,
    )
    session.add(customer)
    await session.flush()


async def _import_product_row(
    session: AsyncSession,
    company_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    uom_code = data.pop("uom_code", None)
    if "uom_id" not in data or not data["uom_id"]:
        if not uom_code:
            raise DomainValidationError("Product requires uom_id or uom_code")
        uom = await session.scalar(select(Uom).where(Uom.code == str(uom_code)))
        if uom is None:
            raise DomainValidationError(f"UOM code '{uom_code}' does not exist")
        data["uom_id"] = uom.id

    payload = ProductCreate(**data)
    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.PRODUCT,
        raw_custom_data=payload.custom_data,
        is_create=True,
    )
    product = Product(
        company_id=company_id,
        sku=payload.sku,
        name=payload.name,
        uom_id=payload.uom_id,
        list_price=payload.list_price,
        standard_cost=payload.standard_cost,
        is_active=payload.is_active,
        custom_data=normalized_custom_data,
    )
    session.add(product)
    await session.flush()


async def _import_account_row(
    session: AsyncSession,
    company_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    payload = AccountCreate(**data)
    normalized_custom_data = await validate_and_normalize_custom_data(
        session,
        entity_type=CustomFieldEntityType.ACCOUNT,
        raw_custom_data=payload.custom_data,
        is_create=True,
    )
    account = Account(
        company_id=company_id,
        code=payload.code,
        name=payload.name,
        type=payload.type,
        is_control=payload.is_control,
        is_active=payload.is_active,
        custom_data=normalized_custom_data,
    )
    session.add(account)
    await session.flush()
