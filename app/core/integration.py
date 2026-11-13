"""Integration core port — ADR-013 / Week 13 Slice A.

Defines:
- Supported batch entity types (`BatchEntityType`)
- Supported import/export entity subsets (`IMPORT_SUPPORTED_ENTITIES`, `EXPORT_SUPPORTED_ENTITIES`)
- Domain exceptions (`IntegrationError`, `UnsupportedBatchEntityError`,
  `BatchImportValidationError`)
- Runtime protocol and registry for module export providers (`BatchExportProvider`)
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, DomainValidationError


class BatchEntityType(StrEnum):
    """Business entities supporting batch import and/or export."""

    CUSTOMER = "customer"
    PRODUCT = "product"
    ACCOUNT = "account"
    SALES_ORDER = "sales_order"
    INVOICE = "invoice"
    PAYMENT = "payment"
    JOURNAL_ENTRY = "journal_entry"
    STOCK_SUMMARY = "stock_summary"


class BatchDataFormat(StrEnum):
    """Supported batch data file formats."""

    CSV = "CSV"
    JSON = "JSON"


IMPORT_SUPPORTED_ENTITIES: frozenset[BatchEntityType] = frozenset(
    {
        BatchEntityType.CUSTOMER,
        BatchEntityType.PRODUCT,
        BatchEntityType.ACCOUNT,
    }
)

EXPORT_SUPPORTED_ENTITIES: frozenset[BatchEntityType] = frozenset(BatchEntityType)


class IntegrationError(AppError):
    """Base exception for platform integration failures."""


class UnsupportedBatchEntityError(DomainValidationError, IntegrationError):
    """Raised when an operation is requested on an unsupported batch entity type."""


class BatchImportValidationError(DomainValidationError, IntegrationError):
    """Raised when batch import data fails schema or domain validation."""


class BatchExportProvider(Protocol):
    """Protocol implemented by modules or platform adapters providing exportable rows."""

    async def __call__(
        self,
        session: AsyncSession,
        company_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Fetch rows for the active tenant."""
        ...


_export_providers: dict[str, BatchExportProvider] = {}


def register_export_provider(
    entity_type: str | BatchEntityType,
    provider: BatchExportProvider,
) -> None:
    """Register an export provider for `entity_type`."""
    key = str(entity_type.value if isinstance(entity_type, BatchEntityType) else entity_type)
    _export_providers[key] = provider


def get_export_provider(entity_type: str | BatchEntityType) -> BatchExportProvider | None:
    """Retrieve the registered export provider for `entity_type`, if any."""
    key = str(entity_type.value if isinstance(entity_type, BatchEntityType) else entity_type)
    return _export_providers.get(key)


def reset_export_providers() -> None:
    """Reset all registered export providers (used for test isolation)."""
    _export_providers.clear()


def is_supported_import_entity(entity_type: str | BatchEntityType) -> bool:
    """Check if entity_type is supported for batch import."""
    try:
        val = (
            entity_type
            if isinstance(entity_type, BatchEntityType)
            else BatchEntityType(str(entity_type))
        )
        return val in IMPORT_SUPPORTED_ENTITIES
    except ValueError:
        return False


def is_supported_export_entity(entity_type: str | BatchEntityType) -> bool:
    """Check if entity_type is supported for batch export."""
    try:
        val = (
            entity_type
            if isinstance(entity_type, BatchEntityType)
            else BatchEntityType(str(entity_type))
        )
        return val in EXPORT_SUPPORTED_ENTITIES
    except ValueError:
        return False
