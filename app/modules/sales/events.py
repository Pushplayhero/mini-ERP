"""Domain events published by sales (ADR-006 Decision 4, ADR-007).

`sales.order_confirmed` was, in Week 4, deliberately the first event with
**no posting subscriber** — orders stopped at `confirmed`, so there was
nothing yet that should turn a confirmed order into a journal entry.
`app.core.events.publish` requires the event_type to be *registered* (so
publishing without a subscriber still validates the schema and writes an
`outbox` row), but `app.core.events.subscribe` is never called for it here —
proving "registered with zero subscribers" is a valid, silent configuration
(ADR-006 Decision 4).

Registration (`app.core.events.register_event`) happens in `app.main`,
alongside every other module's event wiring — this file only owns the
`event_type` string constant and (for `sales.order_confirmed`) the payload
schema.

`SALES_GOODS_SHIPPED_EVENT_TYPE` (ADR-007, Week 5): the first event through
the bus with **two** subscribers (`inventory.service.handle_goods_shipped`,
`ledger.posting`'s posting handler, subscribed in that order in `app.main`
— "move the goods, then account for them"). Only the `event_type` string
constant lives here — its payload schema (`GoodsShippedPayload`) is defined
in `app.modules.ledger.posting` instead of alongside this constant; see
that module's docstring for why (short version: the posting rule that
consumes the payload needs its shape at rule-definition time, the same
reason `test.synthetic_sale`'s payload schema used to live there). `sales
.service.ship_order` publishes a plain `dict` shaped to match it — it never
imports that class, keeping the independence boundary intact: only this
*string* needs to be shared between the two modules, and both get it from
this one constant.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

SALES_ORDER_CONFIRMED_EVENT_TYPE = "sales.order_confirmed"
SALES_GOODS_SHIPPED_EVENT_TYPE = "sales.goods_shipped"


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
