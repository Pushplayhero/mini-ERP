"""Domain events published by masterdata.

Week 1 scope note: master-plan §10.4 defines the `outbox` table (created
this week — see `models.OutboxEvent` / migration 0001) and states Phase 1's
event-bus deliverable is "write + replay CLI" with the dispatcher itself
landing in Phase 2. That in-process event bus + outbox-write wiring is
scheduled for the Week 3 deliverable ("過帳引擎 + 事件匯流排") per
mini-erp-architecture.md §7, alongside `core/events.py` and `core/posting.py`.

masterdata has no transactional postings of its own (it's pure reference/
master data — the POSTING_RULES table only reacts to `sales`/`inventory`/
`receivables` events), so this module intentionally does not publish any
events yet. This file exists as a structural placeholder so the module
shape matches the architecture template in mini-erp-architecture.md §2.
"""

from __future__ import annotations
