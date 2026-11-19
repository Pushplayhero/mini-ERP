"""Domain events published by purchase (ADR-014).

- `purchase.order_confirmed`: published when a draft PO confirms; carries zero
  subscribers (outbox audit & webhook delivery only).
- `purchase.goods_received`: published when a confirmed PO receives goods; subscribed
  deterministically in `app.main` by `inventory.service.handle_goods_received` first
  (moves goods into stock via `StockMoveType.RECEIPT`) and `ledger.posting`'s rule
  second (`Dr 1300 Inventory / Cr 2110 Accrued Purchases`). The `GoodsReceivedPayload`
  schema lives in `app.modules.ledger.posting` next to the rule that consumes it,
  matching `GoodsShippedPayload`'s boundary convention.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

PURCHASE_ORDER_CONFIRMED_EVENT_TYPE = "purchase.order_confirmed"
PURCHASE_GOODS_RECEIVED_EVENT_TYPE = "purchase.goods_received"


class PurchaseOrderConfirmedPayload(BaseModel):
    """Payload contract for `purchase.order_confirmed`."""

    company_id: uuid.UUID
    source_id: uuid.UUID
    order_no: str
    supplier_id: uuid.UUID
    total: Decimal
