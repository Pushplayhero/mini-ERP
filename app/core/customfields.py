"""Custom fields core port — ADR-011 / Week 11 Slice A.

Defines:
- Supported custom field types (`CustomFieldType`)
- Supported business entity types (`CustomFieldEntityType`)
- Canonical `field_key` format pattern (`FIELD_KEY_PATTERN`)
- Domain validation exceptions (`CustomFieldValidationError`)
- The `CustomDataValidator` (`CustomDataValidatorPort`) runtime protocol and port functions
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainValidationError

FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class CustomFieldType(StrEnum):
    """Supported data types for tenant-defined custom fields."""

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    ENUM = "ENUM"


class CustomFieldEntityType(StrEnum):
    """Tenant-scoped business entities supporting custom field extension."""

    CUSTOMER = "customer"
    PRODUCT = "product"
    ACCOUNT = "account"
    ACCOUNTING_PERIOD = "accounting_period"
    JOURNAL_ENTRY = "journal_entry"
    SALES_ORDER = "sales_order"
    INVOICE = "invoice"
    PAYMENT = "payment"


class CustomFieldValidationError(DomainValidationError):
    """Raised when custom_data violates registered definitions or validation rules."""


class CustomDataValidator(Protocol):
    """Protocol for the runtime custom fields validation engine."""

    async def __call__(
        self,
        session: AsyncSession,
        entity_type: str,
        raw_custom_data: dict[str, Any],
        *,
        is_create: bool = False,
        existing_custom_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate, normalize, and return clean custom_data."""
        ...


CustomDataValidatorPort = CustomDataValidator

_validator: CustomDataValidator | None = None


def set_custom_data_validator(validator: CustomDataValidator | None) -> None:
    """Register the runtime validator implementation (called at composition root)."""
    global _validator
    _validator = validator


def get_custom_data_validator() -> CustomDataValidator | None:
    """Return the currently registered runtime validator, if any."""
    return _validator


async def validate_and_normalize_custom_data(
    session: AsyncSession,
    entity_type: str,
    raw_custom_data: dict[str, Any],
    *,
    is_create: bool = False,
    existing_custom_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize custom_data dictionary through the registered port.

    If a validator is registered by platform.customfields, delegates validation
    and normalization to it. If no validator is registered (e.g. standalone test
    runs without the platform capability active), returns a defensive copy (merging
    with `existing_custom_data` on partial updates when `is_create=False`).
    """
    if _validator is not None:
        return await _validator(
            session,
            entity_type,
            raw_custom_data,
            is_create=is_create,
            existing_custom_data=existing_custom_data,
        )
    if not is_create and existing_custom_data is not None:
        return {**existing_custom_data, **raw_custom_data}
    return dict(raw_custom_data)
