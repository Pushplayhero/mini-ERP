# ADR-015: Payables — Vendor Bills, Outgoing Payments & AP Aging (Phase 3 Week 15)

**Status:** Accepted
**Date:** 2026-11-19
**Deciders:** Project Scope Owner
**Phase / Milestone:** Phase 3 ("Business Suites") — Week 15 (Procure-to-Pay Stage 2)

---

## Context

Week 14 ([ADR-014](ADR-014-purchase-and-supplier-masterdata.md)) delivered supplier masterdata (`masterdata.Supplier`), the purchase order lifecycle (`app.modules.purchase`), warehouse receipt intake (`StockMoveType.RECEIPT`), and Stage 1 of the two-stage GR/IR accrual (`Dr 1300 Inventory / Cr 2110 Accrued Purchases` on `purchase.goods_received`). However, `mini-erp` still lacked vendor billing, GR/IR clearing into Accounts Payable, outgoing supplier payments, AP subledger allocation, and AP aging reports.

Week 15 delivers `app.modules.payables`, completing the Procure-to-Pay (P2P) financial loop while mirroring the proven reliability and concurrency architecture established for O2C receivables in [ADR-008](ADR-008-receivables.md):
- Strictly modular-monolith independent (`import-linter` forbids `payables` from importing `purchase`, `masterdata`, `ledger`, `receivables`, `inventory`, `sales`, `app.platform.*`, or `app.plugins.*`). Cross-module lookups use lightweight `sqlalchemy.table()` Core references with explicit `company_id` predicates.
- Flush-only core service functions paired with router-owned commit + `IntegrityError -> 409` translation and post-commit re-selection (`load_*_unchecked`) to prevent `MissingGreenlet` on expired `TimestampAuditMixin.updated_at`.
- `NUMERIC(20, 6)` + Pydantic `ROUND_HALF_EVEN` Banker's Rounding and fail-closed `TWD` functional currency enforcement.

---

## Decisions

### Decision 1 — Vendor Bills Issue 1:1 from `RECEIVED` POs with Exact GR/IR Clearing
- **Table `vendor_bills`**: Inherits `TenantScopedMixin`, `TimestampAuditMixin`, and `CustomDataMixin`. Columns: `id`, `company_id`, `bill_no` (`BILL-{YYYY}-{NNNNNN}` via `payables_sequences`), `order_id` (FK `purchase_orders.id`), `supplier_id` (FK `suppliers.id`), `supplier_invoice_no VARCHAR(128) NULL`, `status` (`OPEN`, `PARTIAL`, `PAID`, `VOIDED`), `currency_code` (FK `currencies.code`), `order_received_at`, `bill_date`, `due_date`, `total`, `settled_amount`, `snapshot_supplier_code`, `snapshot_supplier_name`, `voided_at`, `custom_data JSONB`.
- **Exact 1:1 PO Amount Matching (PPV Deferred to Week 16 `costing`)**:
  - A vendor bill can ONLY be issued from a `PurchaseOrder` whose `status == RECEIVED` (and `total > 0` / `received_at IS NOT NULL`).
  - `bill.total`, `currency_code`, `supplier_id`, and supplier snapshots are copied server-side directly from the immutable `RECEIVED` PO. There is no `vendor_bill_lines` table or Purchase Price Variance (PPV / `5110`) account in Week 15; `payables.bill_issued` clears `2110 Accrued Purchases` 1:1 into `2000 Accounts Payable` (`Dr 2110 / Cr 2000`). Any unit cost variance or landed cost allocation belongs to Week 16 (`costing`).
- **Optional Unique `supplier_invoice_no`**:
  - Client-supplied `supplier_invoice_no` is trimmed in `VendorBillCreate` (blank/whitespace-only normalizes to `None`).
  - Backstopped by DB CHECK `supplier_invoice_no IS NULL OR length(btrim(supplier_invoice_no)) > 0` and partial unique index `uq_vendor_bills_supplier_invoice_no_live ON (company_id, supplier_id, supplier_invoice_no) WHERE status != 'VOIDED' AND supplier_invoice_no IS NOT NULL`.
  - A voided bill releases both `uq_vendor_bills_order_live` and `uq_vendor_bills_supplier_invoice_no_live`, permitting safe re-billing or re-entry after correction.
- **UTC Deterministic Date Boundaries**:
  - `bill_date` defaults to `datetime.now(timezone.utc).date()` and must satisfy `bill_date >= order_received_at.astimezone(timezone.utc).date()`.
  - DB enforces `CHECK (bill_date >= (order_received_at AT TIME ZONE 'UTC')::date)` so PostgreSQL session timezone and application UTC date semantics always agree.
  - `due_date` defaults to `bill_date + suppliers.payment_terms_days` (`CHECK (due_date >= bill_date)`), reading `payment_terms_days` at billing time using the exact documented pattern established in ADR-008 Decision 1 / R9.

### Decision 2 — Supplier Payments & Command-Level Idempotent Allocations
- **Tables**:
  - `vendor_payments`: `payment_no` (`APAY-{YYYY}-{NNNNNN}`), `supplier_id`, `status` (`ISSUED`, `VOIDED`), `external_ref` (`UNIQUE(company_id, external_ref)` retry-idempotency key returning 409 identifying existing payment `id` and `payment_no`), `currency_code`, `amount > 0`, `allocated_amount` (`0 <= allocated_amount <= amount`), `paid_at`, `voided_at`, `custom_data`.
  - `vendor_payment_allocation_commands` & `vendor_payment_allocations`: Mirrors ADR-008 R14. `UNIQUE(company_id, payment_id, request_ref)` stores a SHA-256 `request_fingerprint` over normalized `sorted((bill_id, amount))`. Exact retry returns 200 idempotently; mismatched payload with a reused `request_ref` fails with `409 ConflictError`.
- **Events**:
  - `POST /api/v1/payables/payments` publishes `payables.payment_issued` → `Dr 2000 Accounts Payable / Cr 1000 Cash` (`source_id = payment.id`).
  - Allocations are subledger-only (`vendor_payment_allocations`) and post no journal entries. Inline allocations on `POST /payments` require `payables.payment.allocate` and reuse `external_ref` as `request_ref`.

### Decision 3 — Maintained Balances, Deadlock-Free Locking & `rebuild_ap_balances`
- `VendorBill.settled_amount` and `VendorPayment.allocated_amount` are transactionally maintained under `SELECT ... FOR UPDATE` with exhaustive 4-state DB CHECK backstops (`OPEN` / `PARTIAL` / `PAID` / `VOIDED`).
- Normative lock order in `allocate_payment_core`: lock `VendorPayment` first (`FOR UPDATE`), then lock all target `VendorBill` rows sorted ascending by `id` (`ORDER BY id FOR UPDATE`).
- `app/cli/rebuild_ap_balances.py` mirrors `rebuild_ar_balances.py`, locking `VendorPayment`s then `VendorBill`s per company in one transaction before recomputing `allocated_amount`, `settled_amount`, and non-voided `VendorBill.status` from `SUM(vendor_payment_allocations.amount)`.

### Decision 4 — Void Corrections & Expanded Control-Account Protection (`2000`, `2110`)
- Voiding a bill (`POST /api/v1/payables/bills/{id}/void`) or payment (`POST /api/v1/payables/payments/{id}/void`) locks the row `FOR UPDATE`, requires `settled_amount == 0` / `allocated_amount == 0`, passes `verify_workflow_approval`, marks `VOIDED` + `voided_at`, and publishes `payables.bill_voided` (`Dr 2000 / Cr 2110`) or `payables.payment_voided` (`Dr 1000 / Cr 2000`) with `event_date = voided_at.date()`.
- **Control Accounts**: `ledger.service.KERNEL_CONTROL_ACCOUNT_CODES` (and the synchronized guard in `platform.workflow.service`) expands from `{"1100"}` to **`{"1100", "2000", "2110"}`**. Manual journal entries (`source_type IS NULL`) and manual reversals (`POST /ledger/journal-entries/{id}/reverse`) touching `1100`, `2000`, or `2110` are rejected (`422 DomainValidationError`), ensuring subledger-to-ledger tie-out invariants cannot be broken by ad-hoc entries.

### Decision 5 — Current-State AP Aging, Tie-Out Invariants & Phase 2 Platform Governance
- `GET /api/v1/payables/reports/ap-aging` runs a single compound `UNION ALL` + conditional aggregation query (`current`, `days_1_30`, `days_31_60`, `days_61_90`, `days_90_plus`, `unapplied_payments`, `net_total`), ensuring one READ COMMITTED snapshot and preserving suppliers who only hold unapplied payments (`net_total < 0`).
- Dual ledger invariants proved end-to-end and via Hypothesis property testing:
  1. `Σ(open bill balances) - Σ(unapplied vendor payments) == Credit(2000) - Debit(2000)`.
  2. `Σ(RECEIVED PO totals without non-voided VendorBill) == Credit(2110) - Debit(2110)`.
- **Phase 2 & Migration `0016`**:
  - Catalog permissions expanded `65 -> 73` (`payables.bill.{create,read,void}`, `payables.payment.{create,read,void,allocate}`, `payables.report.read`).
  - `CustomFieldEntityType` expanded `10 -> 12` (`vendor_bill`, `vendor_payment`).
  - `WorkflowEntityType` & `VALID_ENTITY_TRANSITIONS` expanded `7 -> 9` (`("vendor_bill", "void")`, `("vendor_payment", "void")`).
  - `BatchEntityType` export expanded `10 -> 12` (`vendor_bill`, `vendor_payment`).

---

## Consequences
- Completes the end-to-end P2P cycle (`purchase` -> `inventory` -> `payables` -> `ledger`) while keeping `2000` and `2110` reconcilable by construction.
- Leaves a clean interface for Week 16 (`costing`) to extend variance/landed-cost rules without changing Week 15's subledger schema.
