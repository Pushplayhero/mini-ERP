"""Unit tests for the integration Core Port (ADR-013 / Week 13 Slice A)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, DomainValidationError
from app.core.integration import (
    EXPORT_SUPPORTED_ENTITIES,
    IMPORT_SUPPORTED_ENTITIES,
    BatchEntityType,
    BatchImportValidationError,
    IntegrationError,
    UnsupportedBatchEntityError,
    get_export_provider,
    is_supported_export_entity,
    is_supported_import_entity,
    register_export_provider,
    reset_export_providers,
)


@pytest.fixture(autouse=True)
def _clean_export_providers() -> Any:
    """Save and restore export providers around port tests."""
    from app.core import integration

    original = dict(integration._export_providers)
    reset_export_providers()
    yield
    reset_export_providers()
    integration._export_providers.update(original)


def test_batch_entity_type_enums() -> None:
    assert len(BatchEntityType) == 8
    assert BatchEntityType.CUSTOMER.value == "customer"
    assert BatchEntityType.PRODUCT.value == "product"
    assert BatchEntityType.ACCOUNT.value == "account"
    assert BatchEntityType.SALES_ORDER.value == "sales_order"
    assert BatchEntityType.INVOICE.value == "invoice"
    assert BatchEntityType.PAYMENT.value == "payment"
    assert BatchEntityType.JOURNAL_ENTRY.value == "journal_entry"
    assert BatchEntityType.STOCK_SUMMARY.value == "stock_summary"


def test_supported_entity_subsets() -> None:
    expected_import = frozenset(
        {
            BatchEntityType.CUSTOMER,
            BatchEntityType.PRODUCT,
            BatchEntityType.ACCOUNT,
        }
    )
    assert expected_import == IMPORT_SUPPORTED_ENTITIES
    assert frozenset(BatchEntityType) == EXPORT_SUPPORTED_ENTITIES

    # is_supported_import_entity
    assert is_supported_import_entity(BatchEntityType.CUSTOMER) is True
    assert is_supported_import_entity("customer") is True
    assert is_supported_import_entity("product") is True
    assert is_supported_import_entity("account") is True
    assert is_supported_import_entity("sales_order") is False
    assert is_supported_import_entity("nonexistent") is False

    # is_supported_export_entity
    assert is_supported_export_entity(BatchEntityType.SALES_ORDER) is True
    assert is_supported_export_entity("sales_order") is True
    assert is_supported_export_entity("stock_summary") is True
    assert is_supported_export_entity("nonexistent") is False


def test_exceptions_hierarchy() -> None:
    assert issubclass(IntegrationError, AppError)
    assert issubclass(UnsupportedBatchEntityError, DomainValidationError)
    assert issubclass(UnsupportedBatchEntityError, IntegrationError)
    assert issubclass(BatchImportValidationError, DomainValidationError)
    assert issubclass(BatchImportValidationError, IntegrationError)


@pytest.mark.asyncio
async def test_register_and_get_export_provider() -> None:
    assert get_export_provider(BatchEntityType.SALES_ORDER) is None
    assert get_export_provider("sales_order") is None

    mock_provider = AsyncMock(return_value=[{"order_no": "SO-001", "total": "100.00"}])
    register_export_provider(BatchEntityType.SALES_ORDER, mock_provider)

    provider = get_export_provider("sales_order")
    assert provider is mock_provider
    assert provider is not None

    mock_session = AsyncMock(spec=AsyncSession)
    company_id = uuid.uuid4()
    rows = await provider(mock_session, company_id)
    assert rows == [{"order_no": "SO-001", "total": "100.00"}]
    mock_provider.assert_awaited_once_with(mock_session, company_id)

    reset_export_providers()
    assert get_export_provider(BatchEntityType.SALES_ORDER) is None
