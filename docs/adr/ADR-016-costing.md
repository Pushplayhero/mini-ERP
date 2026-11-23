# ADR-016: Costing — Moving Weighted Average, Landed Cost, PPV Clearing & Revaluation (Phase 3 Week 16)

**Status:** Accepted
**Date:** 2026-11-23
**Deciders:** Project Scope Owner
**Phase / Milestone:** Phase 3 ("Business Suites") — Week 16 (Inventory Valuation & Variance Clearing)

---

## Context

Phase 1 ([ADR-007](ADR-007-inventory-and-shipping.md)) and Week 14 ([ADR-014](ADR-014-purchase-and-supplier-masterdata.md)) established append-only stock movement tracking (`stock_moves` / `stock_summary`) and two-stage GR/IR purchase accrual (`Dr 1300 Inventory / Cr 2110 Accrued Purchases` at purchase cost), while Week 15 ([ADR-015](ADR-015-payables.md)) delivered 1:1 GR/IR clearing into `2000 Accounts Payable`. However, `mini-erp` still lacked:
1. A perpetual **Moving Weighted Average (MWA)** valuation subledger tracking unit cost and inventory value across multiple purchase receipts at differing costs.
2. **Landed Cost** capitalization (freight, duties, broker fees) allocated to received inventory lots.
3. **Purchase Price Variance (PPV)** clearing between `2110 Accrued Purchases` and a dedicated variance expense account (`5110 Purchase Price Variance`) without modifying Week 15's immutable `vendor_bills` schema.
4. **Inventory Revaluation** and a variance report comparing subledger MWA valuation against `products.standard_cost`.

Week 16 introduces `app.modules.costing` while maintaining strict `import-linter` modular-monolith independence (`core`, `masterdata`, `sales`, `purchase`, `inventory`, `receivables`, `payables`, `ledger`), `NUMERIC(20, 6)` + `ROUND_HALF_EVEN` Banker's Rounding, and fail-closed multi-tenancy.

---

## Decisions

### Decision 1 — Moving Weighted Average Subledger & Append-Only `cost_moves`
- **`product_cost_summaries`**: Composite primary key `(company_id, product_id)`, maintaining `tracked_qty`, `total_cost_value`, and `moving_avg_cost` under `SELECT ... FOR UPDATE` and SHARED per-company advisory lock (`app.core.locking`), rebuildable anytime from append-only `cost_moves` via `app.cli.rebuild_cost_summaries`.
- **`cost_moves`**: Append-only valuation audit log (`TenantScopedMixin`, `RECEIPT`, `SHIPMENT`, `LANDED_COST`, `PPV`, `REVALUATION`) recording deltas and post-move snapshots, guarded by partial unique index `uq_cost_moves_source ON (company_id, source_type, source_id, product_id) WHERE source_type IS NOT NULL` for replay idempotency.
- **Standard-Cost Smooth Fallback on Shipment**:
  - `purchase.goods_received` increments `tracked_qty` and `total_cost_value` at confirmed PO line cost and recomputes `moving_avg_cost = round_half_even(total_cost_value / tracked_qty)`.
  - `sales.goods_shipped` relieves `tracked_qty` and `total_cost_value` at current `moving_avg_cost` when tracked PO inventory exists (closing completely to `0` value when `tracked_qty` hits `0` to leave zero rounding residue); for Phase 1 manual-adjustment stock with `tracked_qty == 0`, it emits a zero-delta audit `CostMove` recording `standard_cost`, keeping all existing O2C flows backward compatible.

### Decision 2 — Independent Cost Adjustment Documents & `5110 Purchase Price Variance`
- **`5110 Purchase Price Variance`** (`AccountType.EXPENSE`) joins the standard chart of accounts in `app/cli/seed_demo.py` and `app/modules/ledger/posting.py`.
- **`cost_adjustments`** (`COST-{YYYY}-{NNNNNN}` via `costing_sequences`, `POSTED` / `VOIDED`):
  - **`LANDED_COST`** (`amount > 0`, requires `RECEIVED` PO containing `product_id` and `tracked_qty > 0`): increases `total_cost_value` and `moving_avg_cost`, posting **`Dr 1300 Inventory / Cr 2110 Accrued Purchases`** (void reverses `Dr 2110 / Cr 1300`, requiring remaining `tracked_qty > 0` and `total_cost_value >= amount`).
  - **`PPV`** (`amount != 0`, requires `RECEIVED` PO containing `product_id`): clears supplier invoice vs PO price variance without distorting inventory unit cost:
    - Unfavorable (`amount > 0`): **`Dr 5110 Purchase Price Variance / Cr 2110 Accrued Purchases`**.
    - Favorable (`amount < 0`): **`Dr 2110 Accrued Purchases / Cr 5110 Purchase Price Variance`**.
  - **`REVALUATION`** (`amount != 0`, requires `tracked_qty > 0` and `total_cost_value + amount >= 0`): adjusts tracked inventory value and MWA unit cost, posting **`Dr 1300 / Cr 5110`** (upward) or **`Dr 5110 / Cr 1300`** (downward).
- **Standard Cost Sync**: `POST /api/v1/costing/summaries/{product_id}/sync-standard-cost` atomically updates `products.standard_cost = summary.moving_avg_cost` under lock.

### Decision 3 — Phase 2 Governance, Valuation Report & `rebuild_cost_summaries`
- **Migration `0017_costing.py` (`73 -> 79` permissions)**: Adds `costing.{summary.read, move.read, adjustment.create, adjustment.read, adjustment.void, report.read}` across 8 new endpoints (`/api/v1/costing/*`, total routes `107 -> 115`).
- **Platform Ports**: Registers `CustomFieldEntityType.COST_ADJUSTMENT` (`12 -> 13`), `WorkflowEntityType.COST_ADJUSTMENT` `void` transition (`7 -> 8` entities / `9 -> 10` transitions), and `BatchEntityType.{COST_ADJUSTMENT, PRODUCT_COST_SUMMARY}` export adapters (`12 -> 14`).
- **Valuation Report & Rebuild CLI**: `GET /api/v1/costing/reports/valuation-variance` compares MWA valuation vs `products.standard_cost`; `app.cli.rebuild_cost_summaries` deterministically rebuilds `product_cost_summaries` from `cost_moves` under EXCLUSIVE company advisory lock.

---

## Consequences
- Completes inventory cost accounting for procurement, landed costs, PPV clearing, and revaluation with full double-entry traceability into `1300`, `2110`, and `5110`.
- Prepares clean inventory valuation primitives for Phase 3 Week 17+ (`app.modules.mrp` work orders and BOM cost roll-ups).
