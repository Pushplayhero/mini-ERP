"""Domain events published by inventory.

inventory is a pure *subscriber* this week (ADR-007 Decision 1): it reacts
to `sales.goods_shipped` (see `service.handle_goods_shipped`, wired onto the
bus in `app.main`, ahead of ledger's posting handler — "move the goods, then
account for them") but does not itself publish any domain event yet — there
is no consumer that would want "stock moved" as a fact independent of the
shipment/adjustment that already describes it. This file exists as a
structural placeholder so the module shape matches the architecture
template in mini-erp-architecture.md §2, mirroring `masterdata/events.py`
and `ledger/events.py`'s identical placeholder role.
"""

from __future__ import annotations
