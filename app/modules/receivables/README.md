# receivables — Week 6

Implemented per `docs/adr/ADR-008-receivables.md` (consensus review v5
APPROVED 2026-08-15). Invoices are issued off `SHIPPED` sales orders,
payments are applied to invoices (沖帳), and `GET
/api/v1/receivables/reports/ar-aging` reports current-state AR aging.

See the ADR's "Consensus Revisions" (R1-R17) for the full review history
behind every non-obvious design choice in `models.py`/`service.py` — in
particular the allocation-command idempotency design (R14),
control-account protection in `ledger.service` (R5/R11), and the
`sales_orders.shipped_at` backfill in migration 0008 (R13/R17).
