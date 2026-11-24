# ADR-017: MRP — Multi-Level BOM, Work Orders, `1310` WIP Subledger & Net-Requirements Planning (Phase 3 Week 17)

**Status:** Accepted
**Date:** 2026-11-24
**Deciders:** Project Scope Owner
**Phase / Milestone:** Phase 3 ("Business Suites") — Week 17 (Manufacturing & MRP)

---

## Context

Phase 3 Weeks 14–16 delivered Procure-to-Pay (`masterdata.Supplier`, `app.modules.purchase`, `app.modules.payables`) and Inventory Valuation (`app.modules.costing` Moving Weighted Average, Landed Cost, PPV, and Revaluation). To serve Taiwanese SME manufacturing workflows, `mini-erp` also requires:
1. **Multi-Level Bills of Materials (BOM)** supporting versioned recipes, scrap allowances, cycle prevention, and multi-level cost roll-up.
2. **Work Order Execution** with closed-loop **`1310 Work in Process` (WIP)** subledger accounting across material issues, material returns, partial receipts, and yield-loss short closes.
3. **Topological Multi-Level MRP Net-Requirements Planning** across shared sub-assemblies and raw materials without double-counting available inventory or open supply.

Week 17 introduces `app.modules.mrp` while preserving strict `import-linter` modular-monolith independence across all 9 business modules (`masterdata`, `sales`, `purchase`, `inventory`, `costing`, `mrp`, `receivables`, `payables`, `ledger`), `NUMERIC(20, 6)` + `ROUND_HALF_EVEN` Banker's Rounding, and fail-closed multi-tenancy.

---

## Decisions

### Decision 1 — Multi-Level BOM with Single-Active Partial Index & Concurrent Cycle Guard
- **`boms` & `bom_lines`**: Multiple historical/draft BOM codes per product are permitted (`uq_boms_company_code`), while partial unique index `uq_boms_company_product_active ON boms (company_id, product_id) WHERE is_active = true` guarantees at most **one active BOM** per product.
- **DAG Cycle Prevention**: BOM mutations acquire a tenant-root row lock (`SELECT id FROM companies WHERE id = :cid FOR UPDATE`) and run bounded multi-level DFS cycle detection (up to depth `20`), preventing direct (`A -> A`), indirect (`A -> B -> C -> A`), and concurrent write-skew cycles.
- **UoM Alignment**: BOM headers and lines enforce matching `products.uom_id` so physical stock moves and MWA valuations stay dimensionally consistent.

### Decision 2 — Work Order Lifecycle, Idempotent Shop-Floor Execution & `1310` WIP Protection
- **`1310 Work in Process`** (`AccountType.ASSET`) joins `app.cli.seed_demo._STANDARD_CHART`, `app.modules.ledger.posting`, and **`KERNEL_CONTROL_ACCOUNT_CODES`** (`{"1100", "1310", "2000", "2110"}`) so manual journal entries and manual reversals cannot distort WIP control balances.
- **Work Order Execution (`WO-{YYYY}-{NNNNNN}`)**:
  - Transitions `draft -> confirmed -> in_progress -> completed` (+ `cancelled`).
  - `confirm` snapshots SKUs/estimated costs, invokes plugin hook `mrp.work_order.validate_confirm`, and passes through `WorkflowEntityType.WORK_ORDER` (`confirm`/`cancel`).
  - `issue-materials`, `return-materials`, and `receive-production` record append-only `work_order_transactions` keyed by `uq_work_order_tx_wo_request_ref` with SHA-256 `request_fingerprint` validation.
  - **`mrp.material_issued`** (`Dr 1310 / Cr 1300`), **`mrp.material_returned`** (`Dr 1300 / Cr 1310`), and **`mrp.production_received`** (`Dr 1300 / Cr 1310`) run in deterministic subscriber order (`inventory` -> `costing` -> `ledger.posting`).
- **Zero-Residue WIP Absorption & Short Close**: Partial production receipts absorb proportional `wip_balance`, while final receipts (or `mark_completed=True` normal yield-loss short closes) absorb **100% of remaining `wip_balance`** and capitalize exact `total_cost` into the finished good's MWA `ProductCostSummary`.

### Decision 3 — Topological MRP Net-Requirements Planning & Platform Governance
- **Topological Sweep (`POST /api/v1/mrp/planning/net-requirements`)**: Traverses the active BOM DAG in Kahn topological order, summing shared diamond-graph raw-material demands before subtracting `stock_summary.on_hand`, confirmed POs, and open WO output (net of open WO unissued component commitments), classifying shortages into `"manufacture"`, `"purchase"`, or `"none"`.
- **Migration `0018_mrp.py` (`79 -> 90` permissions)**: Adds 11 `mrp.*` permissions across `16` new endpoints (`/api/v1/mrp/*`, total application `APIRoute`s `115 -> 131` = `4 Public / 2 Authenticated / 125 RequiresPermission`), extends `CustomFieldEntityType` (`13 -> 15`), `WorkflowEntityType` (`8 -> 9` entities / `10 -> 12` transitions), `BatchEntityType` (`14 -> 16`), and provides `app.cli.rebuild_mrp_balances`.

---

## Consequences
- Closes the core Phase 3 SME manufacturing and inventory valuation loop (`P2P -> Costing -> Multi-Level MRP -> O2C`) with complete double-entry ledger reconciliation across `1300` Inventory and `1310` Work in Process.
- Readies `mini-erp` for **Phase 4 (Taiwan Localization)**: `tw.einvoice`, `tw.tax`, and `tw.reports` implemented cleanly as external plugins over the hardened Phase 2/3 platform kernel.
