"""Domain events published by mrp (ADR-017).

- `mrp.work_order_confirmed`: published when a draft work order confirms; carries
  zero internal subscribers (outbox audit & webhook delivery only).
- `mrp.material_issued`, `mrp.material_returned`, `mrp.production_received`:
  published during shop-floor execution; subscribed deterministically in `app.main`
  by `inventory` first -> `costing` second -> `ledger.posting` third. Their
  posting payload schemas live in `app.modules.ledger.posting`, matching
  `GoodsShippedPayload` and `GoodsReceivedPayload`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

MRP_WORK_ORDER_CONFIRMED_EVENT_TYPE = "mrp.work_order_confirmed"
MRP_MATERIAL_ISSUED_EVENT_TYPE = "mrp.material_issued"
MRP_MATERIAL_RETURNED_EVENT_TYPE = "mrp.material_returned"
MRP_PRODUCTION_RECEIVED_EVENT_TYPE = "mrp.production_received"


class WorkOrderConfirmedPayload(BaseModel):
    """Payload contract for `mrp.work_order_confirmed`."""

    company_id: uuid.UUID
    source_id: uuid.UUID
    wo_no: str
    product_id: uuid.UUID
    planned_qty: Decimal
    estimated_total: Decimal
