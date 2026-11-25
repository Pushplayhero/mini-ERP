# Changelog

All notable changes to **mini-erp** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-24 (`v0.3.0-suites`)

Phase 3 ("Business Suites") milestone release. Expands the Phase 1/2 O2C + multi-tenant governance kernel into a closed-loop SME Procurement, Inventory Valuation, and Manufacturing/MRP suite while preserving strict modular-monolith boundaries and double-entry accounting invariants.

Full release notes: [docs/releases/v0.3.0-suites.md](docs/releases/v0.3.0-suites.md)

### Added

- **`masterdata.Supplier` & `purchase` ([ADR-014](docs/adr/ADR-014-purchase-and-supplier-masterdata.md), migration `0015`)**:
  - Supplier masterdata and Purchase Order lifecycle (`PO-{YYYY}-{NNNNNN}`, `draft -> confirmed -> received` + `cancelled`) with confirm-time standard-cost repricing, `purchase.order.validate_confirm` plugin hook, and `WorkflowEntityType.PURCHASE_ORDER` (`confirm`/`cancel`).
  - `purchase.goods_received` outbox event updating `inventory` (`StockMoveType.RECEIPT`) and posting two-stage GR/IR accrual entries (`Dr 1300 Inventory / Cr 2110 Accrued Purchases`).
- **`payables` ([ADR-015](docs/adr/ADR-015-payables.md), migration `0016`)**:
  - Vendor bills (`BILL-{YYYY}-{NNNNNN}`) issued 1:1 from `RECEIVED` POs clearing GR/IR accruals (`Dr 2110 Accrued Purchases / Cr 2000 Accounts Payable`), protected by `uq_vendor_bills_order_live` and `uq_vendor_bills_supplier_invoice_no_live`.
  - Outgoing vendor payments (`APAY-{YYYY}-{NNNNNN}`), idempotent R14 `request_ref`/`request_fingerprint` allocations, `UNION ALL` AP aging report (`/api/v1/payables/reports/ap-aging`), `2000`/`2110` control-account protection, and `app.cli.rebuild_ap_balances`.
- **`costing` ([ADR-016](docs/adr/ADR-016-costing.md), migration `0017`)**:
  - Moving Weighted Average (MWA) inventory subledger (`product_cost_summaries`, append-only `cost_moves`) subscribing to `purchase.goods_received`, `sales.goods_shipped`, and `mrp.*` events (`inventory -> costing -> ledger.posting`).
  - `COST-{YYYY}-{NNNNNN}` adjustments (`LANDED_COST`, `PPV` clearing against `5110 Purchase Price Variance`, `REVALUATION`), `sync-standard-cost`, Valuation Variance report, and `app.cli.rebuild_cost_summaries`.
- **`mrp` ([ADR-017](docs/adr/ADR-017-mrp.md), migration `0018`)**:
  - Multi-level Bills of Materials with `uq_boms_company_product_active`, UoM parity, scrap-factor explosion, and tenant-locked DFS DAG cycle guard (`422`).
  - Work Orders (`WO-{YYYY}-{NNNNNN}`) with `mrp.work_order.validate_confirm` hooks, `WorkflowEntityType.WORK_ORDER` gating, idempotent `request_ref`/`request_fingerprint` material issue/return/production receipt, closed-loop **`1310 Work in Process`** control-account posting, zero-residue WIP absorption / yield-loss short close, Kahn topological MRP net-requirements engine, and `app.cli.rebuild_mrp_balances`.
- **Phase 3 Demo & Hardening**:
  - Closed-loop interactive showcase CLI `app.cli.demo_suites` (`make demo-suites`, verified by `tests/e2e/test_demo_suites_smoke.py`).
  - Phase 2 platform hardening checkpoint (`FOR UPDATE` + `populate_existing=True` locks on session/webhook/workflow mutations, `extra="forbid"` auth schemas, `_MAX_REGEX_LENGTH = 500`, CSV cell/key sanitization, Act 5 integration demo in `app.cli.demo_platform`).

### Changed

- Expanded catalog to **90 permissions**, **131** classified `APIRoute`s (`4 Public / 2 Authenticated / 125 RequiresPermission`), **15** `CustomFieldEntityType` entities, **9** `WorkflowEntityType` entities (`12` transitions), **16** `BatchEntityType` export adapters, and `KERNEL_CONTROL_ACCOUNT_CODES = {"1100", "1310", "2000", "2110"}`.
- Reconciled `VULN-06` in `docs/security/threat-model.md` at the Phase 3 checkpoint and aligned D.5 Security Gate inventory to **316 tracked files**.
- Bumped package and FastAPI app version from `0.2.0` to `0.3.0`.

## [0.2.0] - 2026-11-18 (`v0.2.0-platform`)

Phase 2 ("Platform") milestone release. Transforms the Phase 1 Order-to-Cash kernel into an authenticated, multi-tenant extensible platform with zero compromises on accounting precision, concurrency safety, or module boundary integrity.

Full release notes: [docs/releases/v0.2.0-platform.md](docs/releases/v0.2.0-platform.md)

### Added

- **`platform.permissions` ([ADR-009](docs/adr/ADR-009-platform-permissions.md), migrations `0009`–`0010`)**:
  - Database-backed opaque bearer sessions (`sessions` table storing SHA-256 token digests) with `/auth/login`, `/auth/switch-company`, and `/auth/logout`, Argon2 password hashing (`pwdlib`), and active-tenant `ActorContext` resolution.
  - 55 fine-grained permissions (`module.resource.action` seeded across `0010`–`0014`), tenant-scoped role assignments (`user_company_roles`), and fail-closed `Public()` / `Authenticated()` / `RequiresPermission(code)` route classification enforcement at app startup (`_classify_routes_or_crash`).
  - Operator CLIs: `app.cli.bootstrap_admin`, `app.cli.manage_companies`, `app.cli.manage_reference_data`.
- **`platform.plugins` ([ADR-010](docs/adr/ADR-010-platform-plugins.md), migration `0011`)**:
  - Per-tenant plugin enablement & JSONB configuration persistence (`company_plugins` table, `/api/v1/plugins`, `/api/v1/plugins/{plugin_name}`).
  - Declarative `PluginManifest` discovery with topological dependency validation and fail-closed runtime tenant gating (`is_enabled`).
  - Refactored `credit_limit` built-in plugin supporting 6-decimal-place Banker's Rounding `grace_amount`.
  - Operator CLI: `app.cli.manage_plugins`.
- **`platform.customfields` ([ADR-011](docs/adr/ADR-011-platform-customfields.md), migration `0012`)**:
  - Per-tenant runtime metadata schemas across 8 business entities (`TEXT`, `INTEGER`, `DECIMAL`, `BOOLEAN`, `DATE`, `ENUM`) via `/api/v1/custom-fields`.
  - Default value population, 6-decimal-place Banker's Rounding, `min_value`/`max_value`/`min_length`/`max_length`/`regex`/`min_date`/`max_date`/`choices` validation, fail-closed unknown key rejection, and soft deactivation preserving historical `custom_data`.
  - Operator CLI: `app.cli.manage_custom_fields`.
- **`platform.workflow` ([ADR-012](docs/adr/ADR-012-platform-workflow.md), migration `0013`)**:
  - Multi-level, amount-threshold-routed approval workflows (`/api/v1/workflows/definitions`, `/api/v1/workflows/instances`) across `sales_order.confirm`, `sales_order.cancel`, `invoice.void`, `payment.void`, and `journal_entry.reverse`.
  - Strict Separation of Duties (`allow_self_approval=False`), step-level `required_permission` RBAC checks, `SELECT ... FOR UPDATE` serialization, policy-tightening parity checks, and atomic single-use `APPROVED -> CONSUMED` anti-replay lock (`uq_workflow_instance_active`).
  - Operator CLI: `app.cli.manage_workflows`.
- **`platform.integration` ([ADR-013](docs/adr/ADR-013-platform-integration.md), migration `0014`)**:
  - Outbound webhooks (`/api/v1/integrations/webhooks`, `/api/v1/integrations/deliveries`) with HMAC-SHA256 `X-MiniERP-Signature` signing, dual-secret overlap rotation (`signing_secret` + `previous_signing_secret`), `ROW_NUMBER()` fair cross-tenant `SKIP LOCKED` delivery claims, exponential backoff retries, DLQ `DEAD` routing, asynchronous outbox worker (`app.cli.run_outbox_worker`), and IP-pinned DNS-rebinding-proof SSRF egress defense (`_PinnedIPNetworkBackend`).
  - CSV/JSON batch import and export engine (`/api/v1/integrations/imports`, `/api/v1/integrations/exports`, `/api/v1/integrations/jobs`) with per-row savepoint isolation and `dry_run` for `customer`, `product`, and `account` imports, plus 8 entity export adapters with hardened OWASP CSV formula-injection neutralization and `app.cli.manage_integrations`.
- **Phase 2 Demo & Release Tooling**:
  - One-click interactive platform showcase CLI `app.cli.demo_platform` (`make demo-platform`) demonstrating `credit_limit` dynamic toggle, `customfields` validation & Banker's Rounding, 2-step workflow with SoD/RBAC `403` enforcement, and O2C ledger settlement while cleanly restoring temporary guardrails.
- **Security Gate & Quality Contracts**:
  - Completed D.5 hybrid tiered security closure (`scripts/d5_security_gate.py`, `docs/security/d5-shards.json`, `docs/security/scan-policy.md`).
  - Enforced 14 `import-linter` architectural boundary contracts (including platform isolation and no bare package root imports).

### Changed

- **BREAKING**: All `/api/v1/*` business endpoints (except explicit `Public()` health/reference reads) now enforce Bearer session authentication (`401`) and explicit RBAC permission checks (`403`).
- **BREAKING**: Global reference data mutations (`/uom`, `/uom-conversions`, `/currencies`, `/exchange-rates`) and tenant creation (`POST /companies`) removed from HTTP surface and moved exclusively to operator CLIs (`manage_reference_data`, `manage_companies`).
- **BREAKING**: Configuring active required custom fields or active approval workflows on a tenant causes non-conforming document creates/transitions to reject with `422 Unprocessable Entity` or `409 Conflict`.
- Bumped package and FastAPI app version from `0.1.0` to `0.2.0`.

## [0.1.0] - 2026-08-22 (`v0.1.0`)

Phase 1 ("Kernel") milestone release: multi-company double-entry accounting kernel (`masterdata`, `ledger`, `sales`, `inventory`, `receivables`), transactional outbox event bus, `NUMERIC(20, 6)` + `ROUND_HALF_EVEN` consistency, Hypothesis property-based trial-balance proofs, and `make demo` O2C walkthrough.
