# Security policy

## Slice D.5 baseline

The current authentication and authorization threat model is documented in
[`docs/security/threat-model.md`](docs/security/threat-model.md), and the
report-only scanner rules are in
[`docs/security/scan-policy.md`](docs/security/scan-policy.md). These documents
describe the Phase 1 trust boundaries and evidence requirements; they do not
claim that the D.5 scan has passed. The existing trusted-plugin policy and
reporting guidance below remain authoritative.

## Plugin trust model (master-plan §10.3, ADR-006, ADR-010)

mini-erp's plugin mechanism (`app/plugins/`, `app.core.hooks`,
`app.core.events`, `app.platform.plugins`) has **no sandbox**. This is a
deliberate, documented design choice, not an oversight:

- A plugin (e.g. `app/plugins/credit_limit.py`) is **admin-installed,
  trusted, in-process Python code** — the same trust model Odoo uses for its
  addon ecosystem. Installing a plugin means granting it the same privileges
  the application process itself has: full database access (via the same
  `AsyncSession` the triggering request is using), the ability to raise
  exceptions that abort transactions, and — while CI enforces "public surface
  only" via `import-linter` boundaries (ADR-010) so plugins only import public
  `service`/`schemas` layers in `app.core` and `app.modules.*` — Python itself
  provides no runtime import or process sandbox.
- **Hooks execute inside the business transaction that triggered them.** A
  hook handler registered on `sales.order.validate_confirm` runs with the same
  database session, the same tenancy context, and the same transaction
  boundary as the `confirm` request itself. A hook that raises aborts that
  whole transaction (fail-closed, `app.core.hooks.run` never swallows a
  handler's exception) — this is intentional: an incorrectly-written plugin
  should visibly break the operation it hooked into, not silently corrupt or
  half-apply it.
- **Do not install untrusted plugin code.** There is no code-signing, no
  plugin-level OS permission sandbox, and no resource/CPU/memory sandboxing. A
  malicious or buggy plugin can read/write any data the installing company's
  database user can reach, and can block or corrupt any transaction it hooks
  into. Treat installing a plugin with the same care as installing a Python
  package with `pip install` from an untrusted source — because that is,
  functionally, what it is.
- **Hook-point APIs follow semver.** `app.core.hooks`' registered hook point
  names (e.g. `sales.order.validate_confirm`) and the shape of `HookContext`
  passed to handlers are public API surface for plugin authors; breaking
  changes to either are treated as breaking releases.

## What Phase 2, Phase 3 & Staging Hardening implemented (ADR-009 through ADR-018)

- **Identity & session hardening (ADR-009, ADR-018)**: Bearer session auth enforces `extra="forbid"` on `LoginRequest`/`SwitchCompanyRequest`, locks session rows (`FOR UPDATE` + `populate_existing=True`) on `switch_company`/`rotate_session`/`logout`/`logout_all`, enforces idle timeout (`MINI_ERP_SESSION_IDLE_TIMEOUT_MINUTES`, throttled `last_active_at` touches), per-user active session concurrency caps (`MINI_ERP_SESSION_MAX_ACTIVE_PER_USER`), `ip_address`/`user_agent` capture, atomic `/auth/rotate` preserving absolute `expires_at`, `/auth/logout-all` panic revocation, `Cache-Control: no-store` / `Pragma: no-cache` on `/auth/*`, operator cleanup CLI (`app/cli/manage_sessions.py`), and re-verifies session expiry and active user status (`403 UserDisabledError` / `401 InvalidSessionError`) across **133** application `APIRoute`s (`4 Public / 4 Authenticated / 125 RequiresPermission / 0 invalid`) and 90 seeded catalog permissions.
- **Plugin governance (ADR-010)**: Phase 2 added a deterministic Kahn topological plugin loader (with SemVer range compatibility, entry-point discovery, and dependency declarations), per-company tenant enablement and JSONB configuration governance (`/api/v1/plugins`, `company_plugins`), and CI-enforced import-linter contracts ensuring plugins touch only public service/schemas layers rather than internal ORM models or routers. Untrusted code sandboxing remains a deliberate non-goal (plugins are trusted in-process code). See `app/plugins/README.md` for details.
- **Custom fields defense-in-depth (ADR-011)**: Tenant-governed custom fields across 15 primary business entities (`Customer`, `Supplier`, `Product`, `Account`, `AccountingPeriod`, `JournalEntry`, `SalesOrder`, `PurchaseOrder`, `Invoice`, `Payment`, `VendorBill`, `VendorPayment`, `CostAdjustment`, `Bom`, `WorkOrder`) enforce fail-closed runtime validation (`extra="forbid"` rejects unknown keys with 422), 500-char regex length caps (`_MAX_REGEX_LENGTH = 500`), PostgreSQL DDL CHECK constraints on definition schema, tenant root row serialization (`lock_company_unchecked` + `FOR UPDATE` `populate_existing=True` on definition updates/deactivations) eliminating TOCTOU race conditions and write-skew, Banker's Rounding (`ROUND_HALF_EVEN`) to `NUMERIC(20, 6)` precision, and dependency inversion via `app.core.customfields` preventing business modules from importing platform implementations. Automated journal entry postings remain fail-safe and unblocked by manual custom field definitions.
- **Workflow approval gate and Separation of Duties (ADR-012)**: Critical state transitions (12 transitions across 9 entity types: `sales_order.{confirm,cancel}`, `purchase_order.{confirm,cancel}`, `work_order.{confirm,cancel}`, `invoice.void`, `payment.void`, `vendor_bill.void`, `vendor_payment.void`, `cost_adjustment.void`, `journal_entry.reverse`) are guarded atomically inside the business transition transaction via `app.core.workflow` (`WorkflowApprovalGate` & `WorkflowTargetResolver`). Tenant-scoped definitions (`workflow_definitions`, `workflow_steps`) serialize mutations via `lock_company_unchecked`, serialize runtime submission and gate verification reads under `FOR SHARE` (`populate_existing=True`), cancel active instances on definition deactivation (and reject approvals/rejections of inactive definitions), evaluate amount-threshold routing (`steps_snapshot`) using an authoritative server-side Core table resolver (never client-supplied amounts), enforce step-level RBAC and strict Separation of Duties (`allow_self_approval=False` by default — requesters cannot approve or reject their own submissions), serialize concurrent approvals and submissions via `SELECT ... FOR UPDATE` (`populate_existing=True`) and the partial unique index `uq_workflow_instance_active`, reject post-approval tampering (`target_amount` mismatch, zero-step or multi-step/permission policy tightening, or `allow_self_approval` SoD tightening raises `409 Conflict`), and atomically mark instances `CONSUMED` inside the business transaction to prevent replay attacks.
- **Integration engine, anti-SSRF & batch security (ADR-013)**: The final Phase 2 platform module implements tenant webhooks (`webhook_endpoints`, `webhook_deliveries`, `integration_outbox_receipts`) and transactional batch import/export (`integration_batch_jobs`) across 13 `/api/v1/integrations` routes guarded by `platform.integration.{read,manage,import}` (with matching service-layer `require_permission` checks, `FOR UPDATE` row locking and `IntegrityError -> ConflictError` translation on webhook mutations, soft-deactivation `DELETE` cancelling queued `PENDING` deliveries, and active-endpoint verification on `retry_webhook_delivery`). Webhook destinations undergo strict scheme/userinfo/port allowlist validation (`443` default, optional `80`) and DNS filtering (blocking private RFC 1918, loopback, CGNAT `100.64.0.0/10`, Alibaba `100.100.100.200`, cloud metadata `169.254.169.254` / `fd00:ec2::254`, benchmark `198.18.0.0/15`, and IPv4-mapped IPv6), followed by IP-pinned TCP connections (`_PinnedIPNetworkBackend` / `_PinnedAsyncHTTPTransport` with `trust_env=False`, `follow_redirects=False`, and zero secondary DNS lookups) to close DNS-rebinding TOCTOU windows. Outbound dispatches send `Accept-Encoding: identity`, read raw bytes via `aiter_raw()` up to `webhook_max_response_bytes` under an explicit `asyncio.timeout`, emit HMAC-SHA256 `X-Webhook-Signature: t=...,v1=...` headers supporting dual-secret rotation overlap (`previous_signing_secret`, `previous_secret_expires_at`), reject duplicate `t=` keys and negative tolerances, and re-acquire `FOR UPDATE` (`populate_existing=True`) post-HTTP to prevent late-worker stomps or retries against in-flight deactivated endpoints. Worker claiming combines `FOR UPDATE OF outbox SKIP LOCKED` fan-out (with per-row savepoint poison-pill isolation and idempotent inserts) with cross-tenant round-robin fair delivery claiming (`ROW_NUMBER() OVER (PARTITION BY company_id ...)` + `FOR UPDATE OF webhook_deliveries SKIP LOCKED`) capped at `max_concurrency`. Batch imports enforce 1 MB / 2,000-row bounds, per-row savepoints, empty `custom_data.` key rejection, empty optional CSV cell fallback to schema defaults, `created_by`/`updated_by` actor stamping, literal text preservation, and `validate_and_normalize_custom_data` across 4 importable masterdata entities (`customer`, `supplier`, `product`, `account`); CSV exports across 16 registered entity adapters default to `include_bom=False` and apply hardened `neutralize_formula` escaping (`=`, `+`, `-`, `@`, `|`, `;`, control/invisible chars, quote wrappers, non-finite numbers). Signing secrets are encrypted at rest (`enc:v1:` `MultiFernet` per ADR-018); see [`docs/security/threat-model.md`](docs/security/threat-model.md) for the resolved `VULN-06` record.
- **Phase 3 financial control accounts, WIP & MRP concurrency controls (ADR-014 through ADR-017)**: Procurement, payables, costing, and manufacturing protect **`1310` Work in Process**, **`2000` Accounts Payable**, and **`2110` Accrued Purchases** alongside **`1100` Accounts Receivable** inside `KERNEL_CONTROL_ACCOUNT_CODES` (rejecting manual journal entries and manual reversals across both `ledger.service` and `WorkflowTargetResolver`), enforce partial unique live billing indexes (`uq_vendor_bills_order_live`, `uq_vendor_bills_supplier_invoice_no_live`) and `request_ref` + SHA-256 `request_fingerprint` anti-replay protection across AP allocations and shop-floor `work_order_transactions`, serialize multi-level BOM mutations under tenant-root row locks (`lock_company_unchecked`) with bounded DFS DAG cycle guards (`_assert_no_bom_cycles`) and `uq_boms_company_product_active`, guard already-consumed material returns (`consumed_min`), unify `acquire_company_advisory_lock` lock ordering (`exclusive=False` during runtime moves vs `exclusive=True` in rebuild CLIs), and handle source-keyed stock movement outbox replays idempotently even when on-hand balances have already depleted to zero.
- **Pre-Phase 4 Staging Hardening (ADR-018)**:
  - **VULN-06 Encryption at Rest (`app/core/crypto.py`)**: Symmetric authenticated encryption (`enc:v1:` `MultiFernet` keyed by `MINI_ERP_SECRET_ENCRYPTION_KEYS`) protects `webhook_endpoints.signing_secret` and `previous_signing_secret` (`VARCHAR(512)`), supporting zero-downtime key rotation and batch CLI re-encryption (`app/cli/manage_integrations.py webhooks reencrypt-secrets`); non-dev/test startups fail closed when encryption keys are unconfigured.
  - **PostgreSQL Row-Level Security (`mini_erp_app`)**: Migration `0019_staging_hardening.py` creates least-privilege role `mini_erp_app` (`NOLOGIN NOSUPERUSER NOBYPASSRLS`) and enables + forces `tenant_isolation_policy` across all **44 single-tenant tables** (35 `TenantScopedMixin` tables + 7 `*_sequences` tables + `stock_summary` + `product_cost_summaries`); `app/core/db.py` (`MINI_ERP_DATABASE_ENFORCE_RLS=True`) dynamically synchronizes `SET LOCAL ROLE mini_erp_app` and transaction-local `app.current_company_id` across `AsyncSession` transactions and nested `SAVEPOINT` rollbacks while clearing connection-record RLS metadata at root commit/rollback/pool checkin. For staging/production hardening against `RESET ROLE`, separate migration credentials from runtime credentials (connect API/workers as a dedicated `LOGIN NOSUPERUSER NOBYPASSRLS` non-owner role granted `mini_erp_app`), and ensure any Phase 4 tenant table explicitly declares `ENABLE`/`FORCE ROW LEVEL SECURITY` in its Alembic migration.
  - **Fail-Closed CORS & Deterministic Secret Scan**: CORS is disabled by default (`MINI_ERP_CORS_ALLOWED_ORIGINS=[]`), mounted outermost around auth middleware when enabled, and validated strictly in `validate_runtime_security()` during `create_app()` and `lifespan()` (rejecting `*`, wildcard hostnames, `null`, `http://`, invalid ports, userinfo, and paths/queries outside dev/test). `scripts/secret_scan.py` enforces zero-dependency working-tree + `--git-history` secret scanning in `make check` and CI.

## Reporting a vulnerability

This is a Phase 1 educational/reference kernel, not (yet) a project with a
formal disclosure process or security contact. Do not publish sensitive
security findings, raw scanner output, source excerpts, credentials, or
proofs of concept in a public issue. Send them only to an authorized project
owner through a private security channel; if no private channel is available,
keep the material offline and request one before disclosure. Non-sensitive
documentation issues may still be reported publicly after confirming that no
security detail is exposed.
