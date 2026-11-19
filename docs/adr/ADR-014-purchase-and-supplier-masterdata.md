# ADR-014: Supplier Masterdata & `purchase` Module (Phase 3 Week 14)

**Status:** Accepted  
**Date:** 2026-11-18  
**Deciders:** Project Scope Owner  
**Phase / Milestone:** Phase 3 ("Business Suites") — Week 14 (Procure-to-Pay Stage 1)  

---

## Context

`mini-erp` v0.2.0-platform completed the Phase 1 Order-to-Cash (O2C) kernel (`sales`, `inventory`, `receivables`, `ledger`, `masterdata`) and Phase 2 Multi-Tenant Platform governance (`permissions`, `plugins`, `customfields`, `workflow`, `integration`). However, stock replenishment until now relied exclusively on manual inventory adjustments (`POST /api/v1/inventory/adjustments`), which carry no vendor provenance, no purchase order approval trail, and no accrued liability accounting entry.

Week 14 introduces the first half of the Procure-to-Pay (P2P) suite:
1. **`Supplier` masterdata** in `masterdata` (`suppliers` table & `/api/v1/suppliers` API).
2. **`app.modules.purchase`** (`purchase_orders`, `purchase_order_lines`, `purchase_sequences`, `/api/v1/purchase-orders` API) managing the full purchase requisition-to-receipt lifecycle (`draft -> confirmed -> received`, `draft|confirmed -> cancelled`).
3. **Two-stage GR/IR Goods Receipt** (`POST /api/v1/purchase-orders/{id}/receive`) integrating synchronously with `inventory` (`StockMoveType.RECEIPT`) and `ledger.posting` (`Dr 1300 Inventory / Cr 2110 Accrued Purchases`).

Fixed constraints carried forward from ADR-001 through ADR-013:
- Strict modular-monolith independence enforced by `import-linter` (`purchase` cannot import `masterdata`, `inventory`, `ledger`, `sales`, `receivables`, `app.platform.*`, or `app.plugins.*`).
- Fail-closed multi-tenancy (`TenantScopedMixin`, `require_current_company_id()`).
- Row locking (`SELECT ... FOR UPDATE`) prior to state checks and sequence allocation.
- `NUMERIC(20, 6)` + `ROUND_HALF_EVEN` Banker's Rounding and TWD functional currency discipline.
- Full integration with Phase 2 `permissions`, `hooks`/`plugins`, `customfields`, `workflow`, and `integration`.

---

## Decisions

### Decision 1 — `Supplier` Masterdata Lives in `masterdata`
`Supplier` mirrors `Customer` inside `app/modules/masterdata/`:
- **Table `suppliers`**: Inherits `TenantScopedMixin`, `TimestampAuditMixin`, and `CustomDataMixin`. Columns: `id`, `company_id`, `code` (`UNIQUE(company_id, code)` via `uq_suppliers_company_code`), `name`, `currency_code` (FK `currencies.code`), `payment_terms_days` (`CHECK (payment_terms_days BETWEEN 0 AND 365)`, server default `30`), `is_active`, and `custom_data JSONB`.
- **No commercial credit limit column**: Unlike customers (where `credit_limit` caps outbound exposure), vendor credit terms are not capped at PO confirmation in Week 14; vendor billing due dates will read `payment_terms_days` in Week 15 (`payables`).
- **TWD guard**: `SupplierCreate` and `SupplierUpdate` enforce `currency_code == "TWD"` consistent with `CustomerCreate` and Phase 1/2 ledger constraints.
- **Endpoints & Permissions**: `/api/v1/suppliers` (`POST`, `GET`) and `/api/v1/suppliers/{supplier_id}` (`GET`, `PATCH`, `DELETE`) guarded by `masterdata.supplier.create|read|update|delete`.

### Decision 2 — `app.modules.purchase` Order Lifecycle, Unit Cost Repricing & Hook Point
`purchase` mirrors `sales` structurally while remaining completely independent:
- **Tables**:
  - `purchase_sequences`: composite PK `(company_id, year)` -> `next_no`, allocating `PO-{YYYY}-{NNNNNN}` under `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE`. Gaplessness is not required for order numbers (same policy as `sales_sequences`).
  - `purchase_orders`: `status` enum (`DRAFT`, `CONFIRMED`, `CANCELLED`, `RECEIVED`), `total >= 0`, `snapshot_supplier_code`, `snapshot_supplier_name`, `confirmed_at`, `cancelled_at`, `received_at`, `custom_data JSONB`. Enforces DB `CHECK` constraints `ck_purchase_orders_confirmed_has_snapshot` (`status::text NOT IN ('CONFIRMED', 'RECEIVED') OR (snapshot_supplier_code IS NOT NULL AND snapshot_supplier_name IS NOT NULL)`) and `ck_purchase_orders_received_has_received_at` (`status::text != 'RECEIVED' OR received_at IS NOT NULL`).
  - `purchase_order_lines`: `qty > 0`, `unit_cost >= 0`, `amount >= 0`, `unit_cost_is_override BOOLEAN`, `snapshot_sku`, `snapshot_product_name`.
- **Unit cost defaulting and confirm-time repricing**:
  - At draft create/update, omitting `unit_cost` defaults to `product.standard_cost` and sets `unit_cost_is_override = False`; specifying `unit_cost` sets `unit_cost_is_override = True`.
  - At `confirm_order`, non-override lines re-resolve against the product's *current* `standard_cost` and recompute `amount = round_half_even(qty * unit_cost)` and `order.total = round_half_even(sum(amount))`. `order.total <= 0` is rejected (`422 DomainValidationError`), supplier & product snapshots are frozen, and `confirm_order` invokes:
    - Hook point `purchase.order.validate_confirm` (`"1.0.0"`) with `HookContext(company_id, order_id, order_no, customer_id=supplier_id, total, supplier_id=supplier_id)` (preserving existing non-null `HookContext.customer_id` compatibility while adding explicit `supplier_id`).
    - Workflow gate `verify_workflow_approval(..., entity_type="purchase_order", entity_id=order.id, transition="confirm", amount=order.total)`.
- **Publishes `purchase.order_confirmed`**: Schema `PurchaseOrderConfirmedPayload` (`company_id`, `source_id`, `order_no`, `supplier_id`, `total`), registered with zero subscribers for outbox audit & webhook delivery.

### Decision 3 — Two-Stage GR/IR Goods Receipt (`receive_order`)
`POST /api/v1/purchase-orders/{order_id}/receive` (`purchase.service.receive_order`, permission `purchase.order.receive`):
1. Locks `PurchaseOrder` row with `SELECT ... FOR UPDATE`; requires `status == CONFIRMED` (`409 ConflictError` otherwise).
2. Snapshots each line (`product_id`, `qty`, `unit_cost`, line `amount`) using the **frozen PO line cost** (`line.unit_cost` and `line.amount`) so `total_cost == order.total`.
   - *Design distinction vs `sales.ship_order`*: Whereas sales shipments read `product.standard_cost` for COGS valuation, purchase receipts recognize inventory at the confirmed PO purchase cost (`order.total`), matching what `2110 Accrued Purchases` expects when cleared against vendor invoices in Week 15.
3. Publishes `purchase.goods_received` inside the active transaction with two deterministic subscribers wired in `app/main.py`:
   - **Subscriber #1 (`inventory.service.handle_goods_received`)**: Aggregates `qty` per `product_id` (preventing `uq_stock_moves_source` collisions on repeated products), locks `(company_id, product_id)` in ascending `product_id` order (`get_or_lock_summary` + SHARED advisory lock), inserts `StockMove(move_type=StockMoveType.RECEIPT, qty_delta=+qty, source_type="purchase.goods_received", source_id=order.id)`, and increments `stock_summary.on_hand`, wrapped in one `SAVEPOINT` for replay idempotency.
   - **Subscriber #2 (`ledger.posting` rule for `purchase.goods_received`)**: Posts a balanced journal entry `Dr 1300 Inventory / Cr 2110 Accrued Purchases` for `total_cost` (skipping cleanly when `total_cost == 0`, rolling back only the inner `SAVEPOINT` on duplicate `(company_id, source_type, source_id)` replay), deriving `entry_date` from `received_at.date()`. If `1300` or `2110` is missing, `AccountResolutionError` aborts the entire receipt transaction atomically.
4. Transitions `order.status = PurchaseOrderStatus.RECEIVED` and stamps `order.received_at`. Once `RECEIVED`, `cancel_order` returns `409 ConflictError`.

### Decision 4 — Phase 2 Platform Governance & Migration `0015` Scope
- **Permissions Catalog (`55 -> 65`)**: Migration `0015_purchase_and_suppliers.py` seeds 10 permissions (`masterdata.supplier.*` x4, `purchase.order.*` x6) and grants all 10 to `admin`.
- **Custom Fields (`8 -> 10` entity types)**: `CustomFieldEntityType` adds `SUPPLIER = "supplier"` and `PURCHASE_ORDER = "purchase_order"`. `custom_field_definitions` CHECK constraint `ck_custom_field_entity_type_valid` is updated in migration `0015`.
- **Workflow (`4 -> 6` entity transitions)**: `WorkflowEntityType` adds `PURCHASE_ORDER = "purchase_order"` supporting `("purchase_order", "confirm")` and `("purchase_order", "cancel")`. `resolve_workflow_target_unchecked` locks POs via Core tables, recomputes confirm totals using `products.standard_cost` for non-override lines, and validates cancellability.
- **Integration (`8 -> 10` export entities, `3 -> 4` import entities)**: `BatchEntityType` adds `SUPPLIER` (import & export) and `PURCHASE_ORDER` (export).

### Decision 5 — Database Schema & `ADD VALUE 'RECEIPT'` Transaction Discipline
Migration `0015` appends `'RECEIPT'` to `stock_move_type` using the guarded `DO $$ IF NOT EXISTS ... ALTER TYPE stock_move_type ADD VALUE 'RECEIPT' ... END$$;` block established in migration `0005` R3, placed at the end of `upgrade()` with zero subsequent references to `'RECEIPT'` inside `0015`.

---

## Consequences
- `mini-erp` now supports real procurement-driven inventory replenishment with double-entry GR/IR liability accrual, eliminating manual-adjustment-only stock intake while preparing clean clearing accounts (`2110`) for Week 15 (`payables`).
- All 14 `import-linter` contracts remain enforced cleanly with `"app.modules.purchase"` isolated as a peer business module.
