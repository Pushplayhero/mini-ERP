# `app.modules.mrp` (ADR-017 / Phase 3 Week 17)

Implements **Multi-Level Bills of Materials (BOM)**, **Work Order Execution** with **1310 Work in Process (WIP)** closed-loop costing, and **Topological MRP Net-Requirements Planning** on top of the Phase 1–3 modular-monolith architecture.

## Design Highlights

- **Multi-Level BOMs & Acyclic DAG Guarantee (`boms`, `bom_lines`)**:
  - Supports multiple historical/inactive BOM versions per product with partial unique index `uq_boms_company_product_active` enforcing at most **one** `is_active = true` BOM per `(company_id, product_id)`.
  - Mutations serialize per company (`SELECT ... FOR UPDATE` on `companies`) and run multi-level DFS cycle detection (up to depth 20) to block direct or indirect circular dependencies even under concurrency.
- **Work Order Execution & Shop-Floor Idempotency (`work_orders`, `work_order_lines`, `work_order_transactions`)**:
  - Gapless numbering `WO-{YYYY}-{NNNNNN}` via `mrp_sequences`, lifecycle `draft -> confirmed -> in_progress -> completed` (+ `cancelled`).
  - `confirm` invokes plugin hook `mrp.work_order.validate_confirm` and `WorkflowTransition.CONFIRM`; `cancel` enforces zero open WIP/issued balances and `WorkflowTransition.CANCEL`.
  - Shop-floor endpoints (`issue-materials`, `return-materials`, `receive-production`) require `request_ref` with SHA-256 `request_fingerprint` replay detection (`uq_work_order_tx_wo_request_ref`).
- **Closed-Loop `1310 Work in Process` Accounting**:
  - Events (`mrp.material_issued`, `mrp.material_returned`, `mrp.production_received`) publish through `app.core.events` and run deterministically in order: `inventory` -> `costing` (MWA) -> `ledger.posting` (`1300 Inventory` <-> `1310 Work in Process`).
  - `1310 Work in Process` is protected inside `KERNEL_CONTROL_ACCOUNT_CODES`.
  - Supports partial production receipts and yield-loss short close (`mark_completed=True`) with exact remaining WIP absorption (`wip_balance == 0` at `COMPLETED`).
- **Topological MRP Net-Requirements Engine**:
  - Processes the active BOM DAG in topological order, netting `stock_summary.on_hand`, confirmed POs, open WO scheduled output, and open WO unissued component allocations across shared multi-level components without double-counting stock.
