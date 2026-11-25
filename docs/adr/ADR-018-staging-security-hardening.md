# ADR-018: Pre-Phase 4 Staging Security Hardening (VULN-06 Encryption, PostgreSQL RLS, Session Hardening & Fail-Closed CORS / Secret Scan)

**Status:** Accepted
**Date:** 2026-11-25
**Deciders:** Project Scope Owner
**Phase / Milestone:** Pre-Phase 4 Staging Hardening

---

## Context

With Phases 1–3 complete (`mini-erp` v0.3.0: Core Ledger, Sales/O2C, Inventory, Receivables, Purchase/P2P, Payables, MWA Costing, Multi-Level MRP, and Platform Services), moving from local development into shared staging and Phase 4 ("Taiwan Localization") requires closing the remaining defense-in-depth gaps identified in the security threat model (`SECURITY.md` / `docs/security/threat-model.md`):

1. **VULN-06 Plaintext Secret Storage**: `webhook_endpoints.signing_secret` and `previous_signing_secret` were stored in plaintext in PostgreSQL, exposing HMAC signing keys to read-only DB dumps, replicas, or SQL injection outside ORM protections.
2. **ORM-Only Tenant Isolation**: `app/core/db.py`'s `do_orm_execute` event protected ORM queries via `with_loader_criteria`, but raw SQL, `text()` queries, background tasks, or future SQL extensions could bypass ORM criteria without database-level Row-Level Security (RLS). Furthermore, running application sessions as the schema-owner/superuser PostgreSQL role would silently bypass RLS policies.
3. **Session Lifecycle Hardening**: Sessions had only an absolute `expires_at` (`session_ttl_minutes=480`) with no idle inactivity timeout, no token rotation endpoint (`/auth/rotate`), no panic global revocation (`/auth/logout-all`), no per-user active session concurrency cap, no HTTP client metadata (`ip_address`, `user_agent`), no `Cache-Control: no-store` enforcement on `/auth/*`, and no operator cleanup CLI.
4. **Startup CORS Guardrails & Deterministic Secret Scanning**: Production/staging deployments needed strict fail-closed validation on runtime encryption keys and CORS origins, paired with a zero-dependency repository/history secret scanner enforced in `make check` and CI.

---

## Decisions

### Decision 1 — VULN-06 Symmetric Authenticated Encryption at Rest (`app/core/crypto.py`)
- **Cipher Format**: Uses `cryptography.fernet.MultiFernet` with versioned ciphertext prefix `enc:v1:`. The first key in `MINI_ERP_SECRET_ENCRYPTION_KEYS` (JSON array or comma-separated Fernet keys) encrypts new ciphertexts; all configured keys decrypt for zero-downtime key rotation.
- **Fail-Closed Startup vs. Dev Fallback**: When `MINI_ERP_SECRET_ENCRYPTION_KEYS` is empty, `staging` / `production` environments fail closed (`SecretEncryptionConfigError` via `validate_runtime_security()` in `create_app()` and `lifespan()`). `development` / `test` environments emit a one-time warning and use an explicit deterministic development-only key derived for local/CI reproducibility.
- **Schema & Backward-Compatible Migration**: Migration `0019_staging_hardening.py` widens `webhook_endpoints.{signing_secret,previous_signing_secret}` from `VARCHAR(255)` to `VARCHAR(512)` (with a fail-safe downgrade check preventing silent ciphertext truncation). `decrypt_secret()` transparently passes through legacy unencrypted values without the `enc:v1:` prefix while `app/cli/manage_integrations.py webhooks reencrypt-secrets (--company-code | --all-companies)` upgrades plaintext or retired-key rows in batch.

### Decision 2 — PostgreSQL Row-Level Security & Least-Privilege Runtime Role (`mini_erp_app`)
- **Database Role & Helper Functions**: Migration `0019_staging_hardening.py` creates `mini_erp_app` (`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`), grants schema/table/sequence DML privileges (revoking `alembic_version`), and installs two SQL helpers:
  - `app_current_company_id() -> uuid`: parses `NULLIF(current_setting('app.current_company_id', true), '')::uuid`.
  - `app_begin_rls_tx(p_company_id text) -> void`: executes `SET LOCAL ROLE mini_erp_app` and transaction-scoped `set_config('app.current_company_id', COALESCE(p_company_id, ''), true)` in a single `asyncpg`-compatible round-trip.
- **Exhaustive 44 / 14 Table Inventory**: `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and `tenant_isolation_policy` (`USING (company_id = app_current_company_id()) WITH CHECK (company_id = app_current_company_id())`) are enforced across all **44 single-tenant tables** (35 `TenantScopedMixin` tables + 7 composite-PK `*_sequences` tables + `stock_summary` + `product_cost_summaries`). The **14 global, auth/entitlement, or cross-tenant queue tables** (`companies`, `currencies`, `exchange_rates`, `uom`, `uom_conversions`, `users`, `roles`, `permissions`, `role_permissions`, `user_company_roles`, `sessions`, `outbox`, `integration_outbox_receipts`, `webhook_deliveries`) intentionally remain non-RLS.
- **Dynamic Transaction & Savepoint Sync (`app/core/db.py`)**: Controlled by `MINI_ERP_DATABASE_ENFORCE_RLS` (default `True`), SQLAlchemy `after_begin`, `before_execute`, `after_transaction_end`, `after_soft_rollback`, `Engine` `commit`/`rollback`, and `Pool` `checkin` listeners bind `mini_erp_app`, dynamically synchronize PostgreSQL's `app.current_company_id` GUC with Python's `company_context` `ContextVar` (including `SAVEPOINT` rollbacks), and purge connection-record RLS metadata at transaction/checkin boundaries so non-ORM Core connections are never affected. `app/platform/integration/worker.py` enters `company_context(company_id)` before `WebhookDelivery` inserts on RLS-protected fan-out reads/writes.

### Decision 3 — Backward-Compatible Session Lifecycle Hardening
- **Schema & Service**: Migration `0019_staging_hardening.py` adds `sessions.last_active_at` (`NOT NULL DEFAULT now()`), `sessions.ip_address` (`VARCHAR(64)`), `sessions.user_agent` (`VARCHAR(255)`), and composite index `ix_sessions_user_id_expires_at`.
  - **Idle Timeout & Throttled Touch**: `resolve_actor()` invalidates sessions idle beyond `MINI_ERP_SESSION_IDLE_TIMEOUT_MINUTES` (default `60`) and refreshes `last_active_at` at most once per `MINI_ERP_SESSION_TOUCH_INTERVAL_SECONDS` (default `60`) via lightweight `UPDATE`.
  - **Per-User Concurrency Cap**: `login()` locks the parent `User` row (`FOR UPDATE`) to serialize concurrent issuance, prunes expired/idle sessions, and evicts least-recently-active excess sessions to stay within `MINI_ERP_SESSION_MAX_ACTIVE_PER_USER` (default `10`).
  - **Endpoints & Headers**: Adds `POST /auth/rotate` (`Authenticated()`, rotates `token_hash` under `FOR UPDATE` while preserving the original absolute `expires_at`) and `POST /auth/logout-all` (`Authenticated()`, revokes every session owned by the user). Existing `POST /auth/switch-company` preserves its HTTP contract. Application `APIRoute` count increases `131 -> 133` (`4 Public / 4 Authenticated / 125 RequiresPermission`). All `/auth/*` HTTP responses enforce `Cache-Control: no-store` and `Pragma: no-cache`.
  - **Operator CLI**: Adds `app/cli/manage_sessions.py` (`purge-expired`, `revoke-user --email`).

### Decision 4 — Fail-Closed CORS & Zero-Dependency Deterministic Secret Scan
- **CORS (`app/main.py` & `app/core/settings.py`)**: `MINI_ERP_CORS_ALLOWED_ORIGINS` defaults to `[]` (CORS disabled, no middleware mounted). When configured, `CORSMiddleware` is mounted outermost (after `authentication_middleware`) so preflight `OPTIONS` requests and `401`/`403` rejections receive proper headers cleanly. Outside `development`/`test`, `validate_runtime_security()` rejects `*`, `null`, wildcard hostnames (`https://*.example.com`), empty/invalid ports, `http://`, embedded userinfo, paths/queries, malformed origins, and `allow_credentials=True` with `*`.
- **Deterministic Secret Scanner (`scripts/secret_scan.py`)**: Zero-dependency Python scanner scanning tracked working-tree files and optionally full `--git-history` (`finditer` across every match per line) for private keys, cloud credentials, Fernet keys, hardcoded env secrets, and plaintext `whsec_` webhook literals, with hash-pinned historical allowlist entries solely for verified legacy test fixtures. Wired into `make secret-scan` / `make check` and `.github/workflows/ci.yml` (`secret-scan` job using 40-char SHA-pinned GitHub Actions with `fetch-depth: 0`).

---

## Consequences & Deployment Checklist
- Closes **VULN-06** with authenticated encryption at rest and zero-downtime key rotation (`app.core.crypto` is ready for Phase 4 Taiwan localization credentials such as tax/e-invoice certificates or API secrets; large X.509/PFX payloads in Phase 4 should use `TEXT` columns rather than `VARCHAR(512)`).
- Adds database-enforced tenant isolation across all 44 single-tenant tables so even raw SQL bugs or future extensions cannot leak or cross-contaminate tenant data.
- **Role Separation in Staging / Production**: Local/CI compose connects as `mini_erp` and enters `mini_erp_app` via `SET LOCAL ROLE`. To neutralize `RESET ROLE` under a hypothetical SQL injection inside a transaction, staging and production deployments should separate DB credentials: execute Alembic migrations as the schema/table owner role, and run the API/workers under a dedicated `LOGIN NOSUPERUSER NOBYPASSRLS` non-owner application role granted `mini_erp_app`.
- **Phase 4 Migration Mandate**: PostgreSQL `ALTER DEFAULT PRIVILEGES` grants DML permissions only; any new single-tenant tables introduced in Phase 4 (`tw.einvoice`, `tw.tax`, `tw.reports`) MUST explicitly enable and force `ROW LEVEL SECURITY` and create `tenant_isolation_policy` in their respective migrations and extend `tests/test_migrations.py`.
- Establishes staging-ready session hygiene, startup security validation, and CI-enforced secret scanning prior to Phase 4 (`tw.einvoice`, `tw.tax`, `tw.reports`).
