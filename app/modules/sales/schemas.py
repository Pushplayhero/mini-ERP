"""sales Pydantic v2 DTOs.

Same convention as masterdata/ledger: `*Create`/`*Update` schemas never
accept `company_id`, `order_no`, `status`, `total`, or any `snapshot_*`
field — all server-decided (ADR-006 Decision 3). A client says *what*
(`customer_id`, `lines`) and, optionally, *at what price* (`unit_price`
override per line — see `SalesOrderLineCreate`); it never says *what number
this order gets*, *what state it's in*, or *what the frozen total/snapshot
values are*.

`unit_price` is the one deliberate exception to "server decides everything
about money": ADR-006 P3 calls this "manual price override, so a quote can
be fixed" — omitting it just means "use today's `product.list_price`" at
write time, but (diff-review fix) that resolved value is not final for a
non-override line: `confirm_order` re-resolves it against the product's
*current* `list_price` at confirm time, per ADR-006 Decision 3. See
`service.py`'s module docstring for the fuller quoted-price-freezing
rationale.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.sales.models import SalesOrderStatus

# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


class SalesOrderLineCreate(BaseModel):
    product_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    # Omit to have the service fill it from the product's current
    # `list_price` at write time (create or update) — see module docstring.
    unit_price: Decimal | None = Field(default=None, ge=0)
    # Omit to have the service fill it from the product's own `uom_id`.
    uom_id: uuid.UUID | None = None


class SalesOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    line_no: int
    product_id: uuid.UUID
    qty: Decimal
    uom_id: uuid.UUID
    unit_price: Decimal
    unit_price_is_override: bool
    snapshot_sku: str | None
    snapshot_product_name: str | None
    amount: Decimal
    created_at: datetime


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class SalesOrderCreate(BaseModel):
    customer_id: uuid.UUID
    # A draft may start with zero lines (added later via PATCH) — the "at
    # least one line" rule (ADR-006 R2) only gates `confirm`, not creation.
    lines: list[SalesOrderLineCreate] = Field(default_factory=list)
    custom_data: dict[str, Any] = Field(default_factory=dict)


class SalesOrderUpdate(BaseModel):
    """Draft-only. Both fields optional/independent; omitting a field leaves

    it unchanged. Providing `lines` *replaces the entire line set* (no
    partial line patching — same "replace, don't merge" semantics
    `ledger.service.post_journal_entry` uses for entry lines), and always
    recomputes `total` server-side.
    """

    lines: list[SalesOrderLineCreate] | None = None
    custom_data: dict[str, Any] | None = None


class SalesOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    order_no: str
    customer_id: uuid.UUID
    status: SalesOrderStatus
    currency_code: str
    total: Decimal
    snapshot_customer_code: str | None
    snapshot_customer_name: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    shipped_at: datetime | None
    custom_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    lines: list[SalesOrderLineRead]
