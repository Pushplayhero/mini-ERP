"""Domain events published by sales (ADR-006 Decision 4).

`sales.order_confirmed` is the second real event type through the ADR-004
bus (after Week 3's synthetic `test.synthetic_sale`) and, deliberately, the
first with **no posting subscriber** — Week 4 orders stop at `confirmed`
(shipping/invoicing are Weeks 5-6), so there is nothing yet that should
turn a confirmed order into a journal entry. `app.core.events.publish`
requires the event_type to be *registered* (so publishing without a
subscriber still validates the schema and writes an `outbox` row), but
`app.core.events.subscribe` is never called for it here — proving
"registered with zero subscribers" is a valid, silent configuration (ADR-006
Decision 4), exactly as `POSTING_RULES` lookups in `ledger.posting` only
ever run for event types something actually subscribed to.

Registration (`app.core.events.register_event`) happens in `app.main`,
alongside every other module's event wiring — this file only owns the
`event_type` string constant and the payload schema, mirroring
`ledger.posting`'s ownership of `SYNTHETIC_SALE_EVENT_TYPE`/
`SyntheticSalePayload`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

SALES_ORDER_CONFIRMED_EVENT_TYPE = "sales.order_confirmed"


class SalesOrderConfirmedPayload(BaseModel):
    """Payload for `sales.order_confirmed`.

    `source_id` = the confirmed order's id (ADR-006 P3): keeps the payload
    shape consistent with posting events (`ledger.posting.SyntheticSalePayload`
    also carries a `source_id`), so any future consumer — a real posting
    rule, the replay classifier, a Phase 2 webhook dispatcher — can treat
    "what business record does this event describe" uniformly across event
    types instead of special-casing sales.
    """

    company_id: uuid.UUID
    source_id: uuid.UUID
    order_no: str
    customer_id: uuid.UUID
    total: Decimal
