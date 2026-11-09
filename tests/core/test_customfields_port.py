"""Unit tests for the customfields Core Port (ADR-011 / Week 11 Slice A)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.customfields import (
    FIELD_KEY_PATTERN,
    CustomDataValidator,
    CustomDataValidatorPort,
    CustomFieldEntityType,
    CustomFieldType,
    CustomFieldValidationError,
    get_custom_data_validator,
    set_custom_data_validator,
    validate_and_normalize_custom_data,
)
from app.core.exceptions import DomainValidationError


@pytest.fixture(autouse=True)
def _reset_custom_data_validator() -> Any:
    """Ensure the validator port is reset before and after each test."""
    original = get_custom_data_validator()
    set_custom_data_validator(None)
    yield
    set_custom_data_validator(original)


def test_custom_field_type_enums() -> None:
    assert CustomFieldType.TEXT == "TEXT"
    assert CustomFieldType.INTEGER == "INTEGER"
    assert CustomFieldType.DECIMAL == "DECIMAL"
    assert CustomFieldType.BOOLEAN == "BOOLEAN"
    assert CustomFieldType.DATE == "DATE"
    assert CustomFieldType.ENUM == "ENUM"
    assert len(CustomFieldType) == 6


def test_custom_field_entity_type_enums() -> None:
    expected_entities = {
        "customer",
        "product",
        "account",
        "accounting_period",
        "journal_entry",
        "sales_order",
        "invoice",
        "payment",
    }
    assert {e.value for e in CustomFieldEntityType} == expected_entities
    assert CustomFieldEntityType.CUSTOMER.value == "customer"
    assert CustomFieldEntityType.PRODUCT.value == "product"
    assert CustomFieldEntityType.ACCOUNT.value == "account"
    assert CustomFieldEntityType.ACCOUNTING_PERIOD.value == "accounting_period"
    assert CustomFieldEntityType.JOURNAL_ENTRY.value == "journal_entry"
    assert CustomFieldEntityType.SALES_ORDER.value == "sales_order"
    assert CustomFieldEntityType.INVOICE.value == "invoice"
    assert CustomFieldEntityType.PAYMENT.value == "payment"


def test_field_key_pattern_and_port_alias() -> None:
    assert CustomDataValidatorPort is CustomDataValidator
    assert FIELD_KEY_PATTERN.match("a")
    assert FIELD_KEY_PATTERN.match("einvoice_carrier_id")
    assert FIELD_KEY_PATTERN.match("a" * 64)
    assert not FIELD_KEY_PATTERN.match("")
    assert not FIELD_KEY_PATTERN.match("1_starts_with_digit")
    assert not FIELD_KEY_PATTERN.match("UpperCase")
    assert not FIELD_KEY_PATTERN.match("has-hyphen")
    assert not FIELD_KEY_PATTERN.match("a" * 65)


def test_custom_field_validation_error_is_domain_validation_error() -> None:
    err = CustomFieldValidationError("Field 'vip_tier' is required.")
    assert isinstance(err, DomainValidationError)
    assert str(err) == "Field 'vip_tier' is required."


@pytest.mark.asyncio
async def test_validate_and_normalize_fallback_when_no_validator_registered() -> None:
    """When no validator is registered, returns a defensive copy and merges on partial update."""
    dummy_session = AsyncMock(spec=AsyncSession)
    raw_data = {"notes": "test", "vip": True}

    result = await validate_and_normalize_custom_data(
        dummy_session,
        CustomFieldEntityType.CUSTOMER,
        raw_data,
        is_create=True,
    )
    assert result == raw_data
    assert result is not raw_data

    # Partial update fallback merges existing_custom_data with raw_custom_data
    updated = await validate_and_normalize_custom_data(
        dummy_session,
        CustomFieldEntityType.CUSTOMER,
        {"vip": False},
        is_create=False,
        existing_custom_data={"notes": "kept", "vip": True},
    )
    assert updated == {"notes": "kept", "vip": False}


@pytest.mark.asyncio
async def test_validate_and_normalize_delegates_to_registered_validator() -> None:
    """When validator is registered, port forwards arguments and returns normalized data."""
    dummy_session = AsyncMock(spec=AsyncSession)
    raw_data = {"vip_level": "gold"}
    normalized_data = {"vip_level": "GOLD", "loyalty_points": 100}

    class MockValidator(CustomDataValidator):
        async def __call__(
            self,
            session: AsyncSession,
            entity_type: str,
            raw_custom_data: dict[str, Any],
            *,
            is_create: bool = False,
            existing_custom_data: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert session is dummy_session
            assert entity_type == "customer"
            assert raw_custom_data == raw_data
            assert is_create is True
            assert existing_custom_data == {"loyalty_points": 100}
            return normalized_data

    validator = MockValidator()
    set_custom_data_validator(validator)
    assert get_custom_data_validator() is validator

    result = await validate_and_normalize_custom_data(
        dummy_session,
        CustomFieldEntityType.CUSTOMER,
        raw_data,
        is_create=True,
        existing_custom_data={"loyalty_points": 100},
    )
    assert result == normalized_data


@pytest.mark.asyncio
async def test_validate_and_normalize_raises_domain_error_from_validator() -> None:
    """Validator exceptions propagate directly through the core port."""
    dummy_session = AsyncMock(spec=AsyncSession)

    class FailingValidator(CustomDataValidator):
        async def __call__(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            raise CustomFieldValidationError("Unknown custom field 'extra_foo'")

    set_custom_data_validator(FailingValidator())

    with pytest.raises(CustomFieldValidationError, match="Unknown custom field 'extra_foo'"):
        await validate_and_normalize_custom_data(
            dummy_session,
            CustomFieldEntityType.CUSTOMER,
            {"extra_foo": "bad"},
        )
