"""Domain events published by ledger.

Ledger is the *terminus* of a posting, not a source of further downstream
events. Business modules (`sales`/`inventory`/`receivables`) publish domain
events describing what happened (`goods_shipped`, `invoice_issued`, ...);
the posting engine (`app.modules.ledger.posting`, subscribed via
`app.core.events` — see ADR-003/ADR-004, revised from the original
`core/posting.py` sketch in mini-erp-architecture.md §4/§7 for import-linter
reasons documented in ADR-003 Decision 1) subscribes to those and turns them
into journal entries via `service.post_journal_entry`. Nothing consumes a
journal entry being created — there is no "journal_entry_posted" event in
Phase 1 — so this module intentionally does not publish anything yet.

This file exists as a structural placeholder so the module shape matches
the architecture template in mini-erp-architecture.md §2, mirroring
`masterdata/events.py`.
"""

from __future__ import annotations
