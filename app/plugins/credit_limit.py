"""credit_limit — the Week 4 demonstration plugin (ADR-006, ADR-008, ADR-010).

Migrated to a formal `PluginManifest` under ADR-010 (Phase 2, Week 10 Slice C):
- Declares `PLUGIN_MANIFEST` with `default_enabled=True`
  and `config_schema=CreditLimitPluginConfig`.
- Uses only public service ports (`masterdata_service.lock_customer_credit_limit_unchecked`,
  `receivables_service.get_customer_exposure_unchecked`) — zero reach-in to internal ORM models,
  enforced in CI by import-linter.
- Per-tenant configurable `grace_amount` (with backward-compatible `grace_allowance` alias)
  governed under `NUMERIC(20, 6)` Banker's Rounding invariants.

Registers on `app.modules.sales.service.SALES_ORDER_VALIDATE_CONFIRM`: before a
sales order is allowed to move `draft -> confirmed`, checks that this
customer's current exposure plus the order now confirming stays within
`customer.credit_limit + config.grace_amount`.

**Exposure formula (ADR-008 Decision 6, ADR-010 §5.2)**:

```
exposure = Σ total       over orders  status IN (CONFIRMED, SHIPPED)
                                      AND NOT EXISTS (non-voided invoice for order)
         + Σ (total - settled_amount) over non-voided invoices
         + total of the order now confirming
```

**`credit_limit == 0` means "do not check"** — an explicit, documented
sentinel (a real zero-credit customer needs a small positive limit, or a
Phase 2 customer-hold flag, instead).

**TOCTOU**: `SELECT ... FOR UPDATE` on the customer row, taken before summing,
serializes concurrent confirms for the same customer (ADR-010 §5.1 / masterdata_service).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CreditLimitExceededError
from app.core.hooks import HookContext
from app.core.plugins import (
    HookBinding,
    PluginManifest,
    get_current_plugin_config,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.receivables import service as receivables_service
from app.modules.sales import service as sales_service

_MONEY_QUANTUM = Decimal("0.000001")
_MAX_NUMERIC_20_6 = Decimal("100000000000000")  # 10^14


def _round_half_even_6dp(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


class CreditLimitPluginConfig(BaseModel):
    """Per-company configuration for the `credit_limit` plugin (ADR-010 §5.2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    grace_amount: Decimal = Field(
        default=Decimal("0.000000"),
        validation_alias=AliasChoices("grace_amount", "grace_allowance"),
    )

    @field_validator("grace_amount", mode="before")
    @classmethod
    def _validate_grace_amount(cls, v: Any) -> Decimal:
        if isinstance(v, bool | float):
            raise ValueError("grace_amount must be a Decimal, str, or int (float/bool not allowed)")
        if isinstance(v, Decimal):
            raw = v
        elif isinstance(v, int | str):
            s = str(v).strip()
            if not s or len(s) > 64:
                raise ValueError(f"Invalid decimal value for grace_amount: {v!r}")
            try:
                raw = Decimal(s)
            except InvalidOperation as exc:
                raise ValueError(f"Invalid decimal value for grace_amount: {v!r}") from exc
        else:
            raise ValueError(f"Unsupported type for grace_amount: {type(v).__name__}")

        if not raw.is_finite():
            raise ValueError("grace_amount must be a finite decimal")
        if raw < Decimal("0"):
            raise ValueError("grace_amount must be >= 0")
        if raw >= _MAX_NUMERIC_20_6:
            raise ValueError("grace_amount exceeds NUMERIC(20, 6) maximum magnitude")
        try:
            rounded = _round_half_even_6dp(raw)
        except InvalidOperation as exc:
            raise ValueError(
                f"grace_amount cannot be quantized to 6 decimal places: {v!r}"
            ) from exc
        if rounded < Decimal("0.000000"):
            raise ValueError("grace_amount must be >= 0")
        if rounded >= _MAX_NUMERIC_20_6:
            raise ValueError("grace_amount exceeds NUMERIC(20, 6) maximum magnitude")
        return rounded


async def check_credit_limit(session: AsyncSession, context: HookContext) -> None:
    """Hook handler for `sales.order.validate_confirm` (ADR-006, ADR-008 Decision 6, ADR-010 §5).

    Raises `CreditLimitExceededError` (a `ConflictError` subclass -> HTTP 409)
    if confirming this order would push the customer's exposure past its
    `credit_limit` plus optional tenant `grace_amount`. Returns (no-op) if the
    limit is the "unchecked" sentinel (`0`) or the order is within limit.
    """
    credit_limit_amount = await masterdata_service.lock_customer_credit_limit_unchecked(
        session, context.customer_id
    )
    if credit_limit_amount == 0:
        return

    cfg = get_current_plugin_config(CreditLimitPluginConfig)
    exposure_snapshot = await receivables_service.get_customer_exposure_unchecked(
        session, context.customer_id
    )
    exposure = (
        exposure_snapshot.uninvoiced_orders_total
        + exposure_snapshot.open_invoices_total
        + context.total
    )

    if exposure > credit_limit_amount + cfg.grace_amount:
        raise CreditLimitExceededError("Sales order exceeds the customer's credit limit")


PLUGIN_MANIFEST = PluginManifest(
    name="credit_limit",
    version="1.0.0",
    title="Customer Credit Limit Guard",
    description=(
        "Blocks sales order confirmation when customer exposure exceeds "
        "credit_limit plus optional tenant grace_amount."
    ),
    default_enabled=True,
    requires=(),
    config_schema=CreditLimitPluginConfig,
    hooks=(
        HookBinding(
            hook_name=sales_service.SALES_ORDER_VALIDATE_CONFIRM,
            required_version="1.0.0",
            handler=check_credit_limit,
        ),
    ),
    events=(),
)
