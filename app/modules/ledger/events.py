"""Domain events published by ledger.

Week 2 scope note: ledger is the *terminus* of a posting, not a source of
further downstream events. Business modules (`sales`/`inventory`/
`receivables`) will publish domain events describing what happened
(`goods_shipped`, `invoice_issued`, ...); Week 3's posting engine
(`core/posting.py` + `core/events.py`, per mini-erp-architecture.md §4/§7)
subscribes to those and turns them into journal entries via this module's
`service.create_journal_entry`. Nothing consumes a journal entry being
created — there is no "journal_entry_posted" event in Phase 1 — so this
module intentionally does not publish anything yet.

This file exists as a structural placeholder so the module shape matches
the architecture template in mini-erp-architecture.md §2, mirroring
`masterdata/events.py`.
"""

from __future__ import annotations
