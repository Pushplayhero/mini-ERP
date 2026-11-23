# Phase 3 ("Business Suites") — Architecture Overview

**Status:** Accepted  
**Date:** 2026-11-18  
**Baseline Release:** `v0.2.0-platform` (Phase 2 Platform Complete)  
**Master Plan Reference:** `docs/open-erp-master-plan.md` §3 (`purchase`, `payables`, `mrp`, `costing`)

---

## 1. Strategic Direction: Suites Built on top of Platform & Kernel

Phase 1 delivered the Order-to-Cash (O2C) kernel (`masterdata`, `ledger`, `sales`, `inventory`, `receivables`) with strict double-entry invariants and fail-closed tenant isolation. Phase 2 delivered the multi-tenant governance layer (`platform.permissions`, `platform.plugins`, `platform.customfields`, `platform.workflow`, `platform.integration`).

Phase 3 expands `mini-erp` from a single O2C revenue loop into an integrated **SME Manufacturing & Distribution ERP Suite**, delivered incrementally per domain module:

| Milestone | Scope | Key Capabilities | Architecture Basis |
| :--- | :--- | :--- | :--- |
| **Week 14 (ADR-014)** | `masterdata.Supplier` + `purchase` | Supplier masterdata, PO lifecycle (`draft -> confirmed -> received`), confirm-time standard-cost repricing, Goods Receipt (`purchase.goods_received`), Inventory intake (`StockMoveType.RECEIPT`), GR/IR accrued purchases posting (`Dr 1300 / Cr 2110`) | [ADR-014](ADR-014-purchase-and-supplier-masterdata.md) |
| **Week 15 (ADR-015)** | `payables` | Supplier Invoices / Vendor Bills (1:1 from `RECEIVED` POs clearing `2110 Accrued Purchases` → `2000 Accounts Payable`), Outgoing Vendor Payments, Idempotent Subledger Allocations, `2000`/`2110` Control Account Protection, AP Aging & `rebuild_ap_balances` | [ADR-015](ADR-015-payables.md) |
| **Week 16 (ADR-016)** | `costing` | Moving Weighted Average inventory valuation subledger, Landed Cost capitalization (`Dr 1300 / Cr 2110`), Purchase Price Variance PPV clearing (`5110` <-> `2110`), Revaluation, Standard Cost sync, Valuation Variance report & `rebuild_cost_summaries` | [ADR-016](ADR-016-costing.md) |
| **Week 17+ (ADR-017)** | `mrp` | Multi-level Bills of Materials (BOM), Work Orders, Material Issue / Production Receipt, WIP ledger posting, and MRP net-requirements planning | Planned |

---

## 2. Two-Stage P2P Accounting Model (GR/IR Accrual Clearing)

To preserve strict auditability between physical warehouse intake (`inventory`) and financial vendor liability (`payables`), Procure-to-Pay (P2P) adopts a **two-stage GR/IR (Goods Receipt / Invoice Receipt) clearing model**:

```
Stage 1 — Physical Goods Receipt (Week 14 / ADR-014: POST /api/v1/purchase-orders/{id}/receive)
  - Event: purchase.goods_received
  - Subscriber #1 (inventory): +qty StockMove(move_type=RECEIPT), stock_summary.on_hand += qty
  - Subscriber #2 (ledger.posting):
      Dr 1300 Inventory             <po_total>
          Cr 2110 Accrued Purchases <po_total>   (GR/IR Clearing Control Account)

Stage 2 — Supplier Invoice & Outgoing Payment (Week 15 / ADR-015: payables)
  - Event: payables.bill_issued
      Dr 2110 Accrued Purchases     <grir_amount>   (+/- Purchase Price Variance in Costing)
          Cr 2000 Accounts Payable  <bill_total>
  - Event: payables.payment_issued
      Dr 2000 Accounts Payable      <payment_amount>
          Cr 1000 Cash              <payment_amount>
```

### Standard Chart-of-Accounts Expansion
Starting in `ADR-014` (`seed_demo.py` and `ledger.posting`), the kernel standard chart expands with two liability codes:
- **`2000` Accounts Payable** (`LIABILITY`) — Vendor payable subledger control account (for `payables` in Week 15).
- **`2110` Accrued Purchases** (`LIABILITY`) — Goods Receipt Not Invoiced (GRNI / GR/IR clearing) account credited upon `purchase.goods_received` and cleared upon supplier invoicing.

---

## 3. Architectural Invariants Preserved Across Phase 3

1. **Modular Monolith & `import-linter` Physics**: Every Phase 3 business module (`purchase`, `payables`, `costing`, `mrp`) is a top-level package under `app/modules/`. Business modules MUST NEVER import each other or `app.platform.*` or `app.plugins.*`. Cross-module queries use lightweight `sqlalchemy.table()` Core table references with explicit `company_id` predicates, and cross-module writes run via `app.core.events` inside the caller's active database transaction.
2. **Core Port Reuse (`app/core/*`)**: Every Phase 3 module natively integrates with Phase 2 platform capabilities through core ports wired in `app/main.py`:
   - `app.core.tenancy`: `TenantScopedMixin` fail-closed ORM isolation + explicit `require_current_company_id()` stamping.
   - `app.core.authorization`: Service-layer `require_permission(...)` + router-layer `RequiresPermission(...)` route classification.
   - `app.core.customfields`: `validate_and_normalize_custom_data(...)` across new master/document entities (`supplier`, `purchase_order`, etc.).
   - `app.core.workflow`: `verify_workflow_approval(...)` and authoritative Core-table resolution for privileged document transitions.
   - `app.core.integration`: Batch import/export provider registration and outbox webhook publishing.
   - `app.core.hooks`: Declarative hook points (`purchase.order.validate_confirm`) allowing tenant-governed plugins to enforce custom business policies without modifying module code.
3. **Decimal Financial Precision**: Every monetary quantity, unit cost, and total uses `NUMERIC(20, 6)` and Pydantic-layer `ROUND_HALF_EVEN` Banker's Rounding, with TWD-only enforcement until multi-currency ledger settlement is explicitly introduced.
