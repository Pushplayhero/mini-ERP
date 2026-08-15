"""Domain events published by receivables (ADR-008).

Only the `event_type` string constants live here — the payload schemas
(`InvoiceIssuedPayload`, etc.) are defined in `app.modules.ledger.posting`,
next to the posting rules that consume them, following the exact asymmetry
`sales.events`/`ledger.posting.GoodsShippedPayload` already established
(see that module's docstring for the full rationale). `service.py` never
imports those schema classes; it publishes plain `dict`s shaped to match
whatever `app.main` registered for a given `event_type` string — the
independence boundary holds because the *string* is the only thing both
modules need to agree on, never a shared Python type.

Registration (`app.core.events.register_event`) and subscription
(`app.core.events.subscribe`, binding each event to
`ledger.posting.make_posting_handler`) both happen in `app.main`, alongside
every other module's event wiring.
"""

from __future__ import annotations

RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE = "receivables.invoice_issued"
RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE = "receivables.invoice_voided"
RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE = "receivables.payment_received"
RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE = "receivables.payment_voided"
