# `app.modules.costing` (ADR-016 / Phase 3 Week 16)

Implements **Moving Weighted Average (MWA)** inventory valuation, **Landed Cost** capitalization, **Purchase Price Variance (PPV)** clearing, and **Inventory Revaluation** on top of the Phase 1/3 modular-monolith architecture.

## Design Highlights

- **Subledger Fact + Projection Pattern (`ADR-002`)**:
  - `cost_moves` is an append-only valuation ledger (`RECEIPT`, `SHIPMENT`, `LANDED_COST`, `PPV`, `REVALUATION`) with unique idempotency index `uq_cost_moves_source` on `(company_id, source_type, source_id, product_id)`.
  - `product_cost_summaries` maintains `(company_id, product_id) -> (tracked_qty, total_cost_value, moving_avg_cost)` under `SELECT ... FOR UPDATE` + SHARED per-company advisory lock (`app.core.locking`), and is fully rebuildable via `app.cli.rebuild_cost_summaries`.
- **Standard-Cost Smooth Fallback**:
  - Goods receipts (`purchase.goods_received`) establish moving-average tracked inventory lots at confirmed PO purchase cost.
  - Sales shipments (`sales.goods_shipped`) deduct tracked cost value at `moving_avg_cost` when tracked lots exist, while seamlessly falling back to zero-value-delta audit moves using the shipment's `standard_cost` for legacy Phase 1 manual-adjustment stock (`tracked_qty == 0`).
- **Independent Cost Adjustments (`COST-{YYYY}-{NNNNNN}`)**:
  - `LANDED_COST` (`amount > 0`, requires `RECEIVED` PO): capitalizes freight/duty into inventory (`Dr 1300 Inventory / Cr 2110 Accrued Purchases`).
  - `PPV` (`amount != 0`, requires `RECEIVED` PO): clears favorable/unfavorable price variances against `5110 Purchase Price Variance` and `2110 Accrued Purchases` without distorting inventory unit cost.
  - `REVALUATION` (`amount != 0`): adjusts tracked inventory value (`1300 Inventory` <-> `5110 Purchase Price Variance`).
- **Platform Governance**:
  - Protected by 6 `costing.*` permissions, `CustomFieldEntityType.COST_ADJUSTMENT`, `WorkflowEntityType.COST_ADJUSTMENT` (`void` transition), and `BatchEntityType` export adapters (`cost_adjustment`, `product_cost_summary`).
