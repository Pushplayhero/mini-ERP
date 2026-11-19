"""Domain events published by payables (ADR-015).

Only the `event_type` string constants live here — the payload schemas
(`VendorBillIssuedPayload`, etc.) live in `app.modules.ledger.posting`,
next to the posting rules that consume them, matching `receivables.events` /
`sales.events` / `purchase.events`.
"""

from __future__ import annotations

PAYABLES_BILL_ISSUED_EVENT_TYPE = "payables.bill_issued"
PAYABLES_BILL_VOIDED_EVENT_TYPE = "payables.bill_voided"
PAYABLES_PAYMENT_ISSUED_EVENT_TYPE = "payables.payment_issued"
PAYABLES_PAYMENT_VOIDED_EVENT_TYPE = "payables.payment_voided"
