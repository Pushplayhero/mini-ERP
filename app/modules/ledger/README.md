# ledger — Week 2+

Empty shell for Week 1. Per `mini-erp-architecture.md` §7 this module lands
in Week 2 (chart of accounts usage, journal entries, accounting periods,
trial balance API) and Week 3 (the declarative posting engine in
`core/posting.py` + the in-process event bus in `core/events.py`, both of
which live in `app/core/` — see master-plan §2.6).

Note: `accounts` (chart of accounts) already exists as a table in
`masterdata` this week (it's reference master data, not a ledger
transaction), but journal entries / periods / trial balance are not
implemented yet.

Not implemented yet: `router.py`, `service.py`, `models.py`, `schemas.py`,
`events.py`.
