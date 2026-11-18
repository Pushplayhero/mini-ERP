# Changelog

All notable changes to **mini-erp** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
