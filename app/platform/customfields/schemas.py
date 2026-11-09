"""platform.customfields Pydantic schemas — ADR-011 / Week 11 Slice B.

Defines API DTOs and type validation/normalization engine for tenant custom fields.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.customfields import (
    FIELD_KEY_PATTERN,
    CustomFieldEntityType,
    CustomFieldType,
    CustomFieldValidationError,
)

_DECIMAL_QUANTUM = Decimal("0.000001")
_MAX_DECIMAL_EXCLUSIVE = Decimal("100000000000000")  # 10^14
_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1


def _parse_and_quantize_decimal(value: Any, context_name: str) -> Decimal:
    """Parse a decimal value and quantize via Banker's Rounding (ROUND_HALF_EVEN)."""
    if isinstance(value, bool):
        raise CustomFieldValidationError(
            f"{context_name} must be a valid decimal number, got boolean {value!r}"
        )
    if isinstance(value, float):
        raise CustomFieldValidationError(
            f"{context_name} rejects float {value!r} to avoid binary floating-point inaccuracy. "
            "Pass as string, int, or Decimal."
        )
    try:
        dec = Decimal(str(value) if not isinstance(value, Decimal) else value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CustomFieldValidationError(
            f"{context_name} is not a valid decimal number: {value!r}"
        ) from exc

    if dec.is_nan() or dec.is_infinite():
        raise CustomFieldValidationError(f"{context_name} cannot be NaN or Infinity, got {value!r}")

    rounded = dec.quantize(_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)
    if not (Decimal("0") <= rounded < _MAX_DECIMAL_EXCLUSIVE):
        raise CustomFieldValidationError(
            f"{context_name} out of bounds: {rounded} must be in [0, 10^14)"
        )
    return rounded


def _validate_text_rules(rules: dict[str, Any]) -> dict[str, Any]:
    allowed = {"min_length", "max_length", "regex"}
    extra = set(rules) - allowed
    if extra:
        raise CustomFieldValidationError(
            f"Unknown validation rule(s) {sorted(extra)} for TEXT field"
        )
    normalized: dict[str, Any] = {}
    min_len = rules.get("min_length")
    max_len = rules.get("max_length")
    regex = rules.get("regex")

    if min_len is not None:
        if isinstance(min_len, bool) or not isinstance(min_len, int) or min_len < 0:
            raise CustomFieldValidationError("min_length must be a non-negative integer")
        normalized["min_length"] = min_len

    if max_len is not None:
        if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len < 1:
            raise CustomFieldValidationError("max_length must be a positive integer >= 1")
        if min_len is not None and max_len < min_len:
            raise CustomFieldValidationError("max_length cannot be less than min_length")
        normalized["max_length"] = max_len

    if regex is not None:
        if not isinstance(regex, str) or not regex.strip():
            raise CustomFieldValidationError("regex must be a non-empty string pattern")
        try:
            re.compile(regex)
        except re.error as exc:
            raise CustomFieldValidationError(f"Invalid regex pattern {regex!r}: {exc}") from exc
        normalized["regex"] = regex
    return normalized


def _validate_integer_rules(rules: dict[str, Any]) -> dict[str, Any]:
    allowed = {"min_value", "max_value"}
    extra = set(rules) - allowed
    if extra:
        raise CustomFieldValidationError(
            f"Unknown validation rule(s) {sorted(extra)} for INTEGER field"
        )
    normalized: dict[str, Any] = {}
    min_val = rules.get("min_value")
    max_val = rules.get("max_value")

    if min_val is not None:
        if isinstance(min_val, bool) or not isinstance(min_val, int):
            raise CustomFieldValidationError("min_value must be an integer")
        if not (_MIN_INT64 <= min_val <= _MAX_INT64):
            raise CustomFieldValidationError("min_value out of 64-bit integer bounds")
        normalized["min_value"] = min_val

    if max_val is not None:
        if isinstance(max_val, bool) or not isinstance(max_val, int):
            raise CustomFieldValidationError("max_value must be an integer")
        if not (_MIN_INT64 <= max_val <= _MAX_INT64):
            raise CustomFieldValidationError("max_value out of 64-bit integer bounds")
        if min_val is not None and max_val < min_val:
            raise CustomFieldValidationError("max_value cannot be less than min_value")
        normalized["max_value"] = max_val
    return normalized


def _validate_decimal_rules(rules: dict[str, Any]) -> dict[str, Any]:
    allowed = {"min_value", "max_value"}
    extra = set(rules) - allowed
    if extra:
        raise CustomFieldValidationError(
            f"Unknown validation rule(s) {sorted(extra)} for DECIMAL field"
        )
    normalized: dict[str, Any] = {}
    min_val_raw = rules.get("min_value")
    max_val_raw = rules.get("max_value")
    min_val_dec: Decimal | None = None
    max_val_dec: Decimal | None = None

    if min_val_raw is not None:
        min_val_dec = _parse_and_quantize_decimal(min_val_raw, "validation_rules.min_value")
        normalized["min_value"] = f"{min_val_dec:.6f}"

    if max_val_raw is not None:
        max_val_dec = _parse_and_quantize_decimal(max_val_raw, "validation_rules.max_value")
        normalized["max_value"] = f"{max_val_dec:.6f}"

    if min_val_dec is not None and max_val_dec is not None and max_val_dec < min_val_dec:
        raise CustomFieldValidationError("max_value cannot be less than min_value")
    return normalized


def _validate_date_rules(rules: dict[str, Any]) -> dict[str, Any]:
    allowed = {"min_date", "max_date"}
    extra = set(rules) - allowed
    if extra:
        raise CustomFieldValidationError(
            f"Unknown validation rule(s) {sorted(extra)} for DATE field"
        )
    normalized: dict[str, Any] = {}
    min_d_raw = rules.get("min_date")
    max_d_raw = rules.get("max_date")
    min_d: date | None = None
    max_d: date | None = None

    if min_d_raw is not None:
        if not isinstance(min_d_raw, str):
            raise CustomFieldValidationError("min_date must be an ISO-8601 date string")
        try:
            min_d = date.fromisoformat(min_d_raw)
        except ValueError as exc:
            raise CustomFieldValidationError(
                f"min_date {min_d_raw!r} is not a valid ISO date: {exc}"
            ) from exc
        normalized["min_date"] = min_d.isoformat()

    if max_d_raw is not None:
        if not isinstance(max_d_raw, str):
            raise CustomFieldValidationError("max_date must be an ISO-8601 date string")
        try:
            max_d = date.fromisoformat(max_d_raw)
        except ValueError as exc:
            raise CustomFieldValidationError(
                f"max_date {max_d_raw!r} is not a valid ISO date: {exc}"
            ) from exc
        normalized["max_date"] = max_d.isoformat()

    if min_d is not None and max_d is not None and max_d < min_d:
        raise CustomFieldValidationError("max_date cannot be earlier than min_date")
    return normalized


def _validate_enum_rules(rules: dict[str, Any]) -> dict[str, Any]:
    allowed = {"choices"}
    extra = set(rules) - allowed
    if extra:
        raise CustomFieldValidationError(
            f"Unknown validation rule(s) {sorted(extra)} for ENUM field"
        )
    choices = rules.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CustomFieldValidationError("ENUM field requires non-empty list of choices")
    cleaned_choices: list[str] = []
    for idx, item in enumerate(choices):
        if not isinstance(item, str) or not item.strip():
            raise CustomFieldValidationError(
                f"ENUM choice at index {idx} must be a non-empty string, got {item!r}"
            )
        cleaned_choices.append(item.strip())
    if len(cleaned_choices) != len(set(cleaned_choices)):
        raise CustomFieldValidationError("ENUM choices list contains duplicate entries")
    return {"choices": cleaned_choices}


def validate_and_normalize_validation_rules(
    field_type: CustomFieldType | str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize validation_rules according to the field_type."""
    if not isinstance(rules, dict):
        raise CustomFieldValidationError("validation_rules must be a dictionary")

    ft = CustomFieldType(field_type)
    if ft == CustomFieldType.TEXT:
        return _validate_text_rules(rules)
    if ft == CustomFieldType.INTEGER:
        return _validate_integer_rules(rules)
    if ft == CustomFieldType.DECIMAL:
        return _validate_decimal_rules(rules)
    if ft == CustomFieldType.BOOLEAN:
        if rules:
            raise CustomFieldValidationError("BOOLEAN custom fields do not accept validation rules")
        return {}
    if ft == CustomFieldType.DATE:
        return _validate_date_rules(rules)
    if ft == CustomFieldType.ENUM:
        return _validate_enum_rules(rules)
    raise CustomFieldValidationError(f"Unsupported custom field type: {field_type!r}")


def _validate_text_value(field_key: str, value: Any, rules: dict[str, Any]) -> str:
    if not isinstance(value, str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' expects string, got {type(value).__name__}"
        )
    min_len = rules.get("min_length")
    max_len = rules.get("max_length")
    regex = rules.get("regex")

    if min_len is not None and len(value) < min_len:
        raise CustomFieldValidationError(
            f"Field '{field_key}' length {len(value)} is shorter than min_length {min_len}"
        )
    if max_len is not None and len(value) > max_len:
        raise CustomFieldValidationError(
            f"Field '{field_key}' length {len(value)} exceeds max_length {max_len}"
        )
    if regex is not None and not re.fullmatch(regex, value):
        raise CustomFieldValidationError(
            f"Field '{field_key}' value does not match regex pattern {regex!r}"
        )
    return str(value)


def _validate_integer_value(field_key: str, value: Any, rules: dict[str, Any]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CustomFieldValidationError(
            f"Field '{field_key}' expects integer, got {type(value).__name__}"
        )
    if not (_MIN_INT64 <= value <= _MAX_INT64):
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {value} out of 64-bit integer bounds"
        )
    min_val = rules.get("min_value")
    max_val = rules.get("max_value")
    if min_val is not None and value < min_val:
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {value} is less than min_value {min_val}"
        )
    if max_val is not None and value > max_val:
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {value} exceeds max_value {max_val}"
        )
    return int(value)


def _validate_decimal_value(field_key: str, value: Any, rules: dict[str, Any]) -> str:
    quantized = _parse_and_quantize_decimal(value, f"Field '{field_key}'")
    min_val_str = rules.get("min_value")
    max_val_str = rules.get("max_value")
    if min_val_str is not None and quantized < Decimal(min_val_str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {quantized} is less than min_value {min_val_str}"
        )
    if max_val_str is not None and quantized > Decimal(max_val_str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {quantized} exceeds max_value {max_val_str}"
        )
    return f"{quantized:.6f}"


def _validate_date_value(field_key: str, value: Any, rules: dict[str, Any]) -> str:
    if not isinstance(value, str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' expects ISO-8601 date string, got {type(value).__name__}"
        )
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as exc:
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {value!r} is not a valid ISO date: {exc}"
        ) from exc

    min_d_str = rules.get("min_date")
    max_d_str = rules.get("max_date")
    if min_d_str is not None and parsed_date < date.fromisoformat(min_d_str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' date {parsed_date} is earlier than min_date {min_d_str}"
        )
    if max_d_str is not None and parsed_date > date.fromisoformat(max_d_str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' date {parsed_date} is later than max_date {max_d_str}"
        )
    return parsed_date.isoformat()


def _validate_enum_value(field_key: str, value: Any, rules: dict[str, Any]) -> str:
    if not isinstance(value, str):
        raise CustomFieldValidationError(
            f"Field '{field_key}' expects string enum choice, got {type(value).__name__}"
        )
    choices = rules.get("choices", [])
    if value not in choices:
        raise CustomFieldValidationError(
            f"Field '{field_key}' value {value!r} is not an allowed choice in {choices}"
        )
    return str(value)


def validate_and_normalize_field_value(
    field_key: str,
    field_type: CustomFieldType | str,
    value: Any,
    rules: dict[str, Any],
) -> Any:
    """Validate a single field value against its field_type and validation_rules."""
    ft = CustomFieldType(field_type)
    if ft == CustomFieldType.TEXT:
        return _validate_text_value(field_key, value, rules)
    if ft == CustomFieldType.INTEGER:
        return _validate_integer_value(field_key, value, rules)
    if ft == CustomFieldType.DECIMAL:
        return _validate_decimal_value(field_key, value, rules)
    if ft == CustomFieldType.BOOLEAN:
        if not isinstance(value, bool):
            raise CustomFieldValidationError(
                f"Field '{field_key}' expects boolean, got {type(value).__name__}"
            )
        return value
    if ft == CustomFieldType.DATE:
        return _validate_date_value(field_key, value, rules)
    if ft == CustomFieldType.ENUM:
        return _validate_enum_value(field_key, value, rules)
    raise CustomFieldValidationError(f"Unsupported custom field type: {field_type!r}")


def validate_default_value(
    field_key: str,
    field_type: CustomFieldType | str,
    default_value: Any,
    rules: dict[str, Any],
) -> Any:
    """Validate and normalize default_value if provided."""
    if default_value is None:
        return None
    return validate_and_normalize_field_value(field_key, field_type, default_value, rules)


# ---------------------------------------------------------------------------
# API DTOs
# ---------------------------------------------------------------------------


class CustomFieldDefinitionCreate(BaseModel):
    """Payload for creating a new custom field definition."""

    model_config = ConfigDict(extra="forbid")

    entity_type: CustomFieldEntityType
    field_key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    field_type: CustomFieldType
    is_required: bool = False
    default_value: Any = None
    validation_rules: dict[str, Any] = Field(default_factory=dict)

    @field_validator("field_key")
    @classmethod
    def validate_field_key_format(cls, v: str) -> str:
        if not FIELD_KEY_PATTERN.fullmatch(v):
            raise ValueError(
                f"field_key {v!r} is invalid: must match '^[a-z][a-z0-9_]{{0,63}}$' "
                "(lowercase letters, numbers, underscores, starting with letter)"
            )
        return v

    @field_validator("label")
    @classmethod
    def validate_label_nonempty(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("label cannot be whitespace only")
        return trimmed

    @model_validator(mode="after")
    def validate_rules_and_default(self) -> CustomFieldDefinitionCreate:
        normalized_rules = validate_and_normalize_validation_rules(
            self.field_type, self.validation_rules
        )
        self.validation_rules = normalized_rules
        if self.default_value is not None:
            self.default_value = validate_default_value(
                self.field_key, self.field_type, self.default_value, normalized_rules
            )
        return self


class CustomFieldDefinitionUpdate(BaseModel):
    """Payload for updating an existing custom field definition."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_required: bool | None = None
    is_active: bool | None = None
    default_value: Any | None = None
    validation_rules: dict[str, Any] | None = None

    @field_validator("label")
    @classmethod
    def validate_label_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("label cannot be whitespace only")
        return trimmed

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> CustomFieldDefinitionUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in update payload")
        for non_null_field in ("label", "is_required", "is_active", "validation_rules"):
            if non_null_field in self.model_fields_set and getattr(self, non_null_field) is None:
                raise ValueError(f"{non_null_field} cannot be null")
        return self


class CustomFieldDefinitionRead(BaseModel):
    """Read representation of a custom field definition."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    entity_type: str
    field_key: str
    label: str
    field_type: str
    is_required: bool
    is_active: bool
    default_value: Any | None = None
    validation_rules: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None
