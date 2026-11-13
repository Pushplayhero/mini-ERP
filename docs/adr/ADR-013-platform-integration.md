# ADR-013: `platform.integration` — Outbox Webhook Dispatcher, Delivery State Engine, and Batch Data Exchange

**Status:** **ACCEPTED**  
**Date:** 2026-11-13  
**Deciders:** Project Owner, Platform Architecture Gate  
**Depends on:**
- `docs/adr/PHASE2-platform-architecture-overview.md` §2.5, §2.6, §4, §5.2, §5.3, §5.4, §7.2 Q7 (ACCEPTED)
- `docs/open-erp-master-plan.md` §2.6, §3, §10.4
- `docs/adr/ADR-004-event-bus-and-outbox.md` (ACCEPTED)
- `docs/adr/ADR-009-platform-permissions.md` Decisions 4, 7, 9, 10 (ACCEPTED)
- `docs/adr/ADR-010-platform-plugins.md` Decisions 1, 2, 6 (ACCEPTED)
- `docs/adr/ADR-011-platform-customfields.md` Decisions 1, 5, 7 (ACCEPTED)
- `docs/adr/ADR-012-platform-workflow.md` Decisions 1, 8 (ACCEPTED)

> Numbering note: ADR-013 is the fifth and final Phase 2 platform ADR, completing the platform suite (`permissions`, `plugins`, `customfields`, `workflow`, `integration`) per `docs/adr/PHASE2-platform-architecture-overview.md` §4 / §7.2.

---

## Context

In Phase 1, `app.core.events` established the in-process synchronous event bus and transactional outbox pattern (ADR-004). Domain events emitted by business modules (`sales.order_confirmed`, `sales.goods_shipped`, `receivables.invoice_issued`, `receivables.invoice_voided`, `receivables.payment_received`, `receivables.payment_voided`) are written atomically into the `outbox` table in the caller's database transaction.

However, two major integration gaps remained for Phase 2 (`PHASE2-platform-architecture-overview.md` §5.2, §5.3, §7.2):

1. **Outbox delivery vs. internal replay state collision (§5.2):**
   The `outbox.dispatched_at` column is claimed exclusively by internal synchronous dispatch and the replay CLI (`app/cli/replay_outbox.py`). A webhook dispatcher reading `WHERE dispatched_at IS NULL` would collide with internal replay: manual replays would suppress webhooks, and webhook dispatches would mark events as internally replayed. Furthermore, external endpoints require independent per-endpoint delivery attempts, exponential backoff, and dead-letter queue (DLQ) state that a single timestamp on `outbox` cannot represent.
2. **Co-deployed Worker vs. in-process background task (§5.3 & §7.2 Q7):**
   Dispatching external HTTP calls inside FastAPI request lifecycles causes connection exhaustion, un-rollbackable side-effects during aborted transactions, and ambient `ContextVar` leakage. As resolved in §7.2 Q7, external delivery must run in a co-deployed standalone worker process (`app/cli/run_outbox_worker.py`) reading directly from PostgreSQL without introducing a message broker in Phase 2.
3. **Batch data exchange without breaking modular-monolith boundaries:**
   SMEs require batch data onboarding and reporting via CSV/Excel. Importing master data (`customers`, `products`, `accounts`) must reuse existing service validation and custom fields, while exporting transactional documents (`sales_orders`, `invoices`, `payments`, `journal_entries`, `stock_summary`) must not violate `import-linter` modular boundaries.
4. **External network egress and CSV security:**
   Calling external webhook URLs introduces Server-Side Request Forgery (SSRF) risks against internal networks and cloud metadata endpoints. Generating CSV files for spreadsheet software introduces CSV Formula Injection risks.

---

## Decision

### 1. Two-layer outbox consumption: immutable source vs. consumer delivery state

Following `PHASE2-platform-architecture-overview.md` §5.2:
- **`outbox` table**: The immutable event source of truth for domain events. `outbox.dispatched_at` remains reserved exclusively for internal subscriber replay (`app/cli/replay_outbox.py`) and is **never modified** by `platform.integration`.
- **`integration_outbox_receipts`**: Tracks which `outbox` events have been fanned out to external endpoints (`outbox_id UUID PRIMARY KEY REFERENCES outbox(id) ON DELETE CASCADE`). Fanning out uses `NOT EXISTS` + `FOR UPDATE SKIP LOCKED` on `outbox` to prevent duplicate fan-outs and eliminate outbox scan watermark gaps caused by out-of-order transaction commits in PostgreSQL.
- **`webhook_deliveries`**: Stores per-endpoint, per-event delivery tasks with independent lifecycle state:
  `status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'DEAD')`.

---

### 2. Co-deployed worker architecture and lease claiming

`platform.integration` implements a dedicated worker (`app.platform.integration.worker`) and CLI entrypoint (`app/cli/run_outbox_worker.py`):
1. **No message broker in Phase 2 (§7.2 Q7):** Worker queries PostgreSQL directly.
2. **Dedicated service actor:** Runs under `INTEGRATION_WORKER_ACTOR` with explicit `company_context` per company and no inherited HTTP request state.
3. **Two-phase worker loop:**
   - **Fan-out phase:** Selects committed `outbox` events not yet in `integration_outbox_receipts` using `FOR UPDATE OF outbox SKIP LOCKED`, isolates each event inside a per-row savepoint, validates `dict` JSONB payloads cleanly, matches active `webhook_endpoints` subscribed to `event_type` for the event's `company_id`, inserts `webhook_deliveries` (`id=uuid.uuid4()`) with `ON CONFLICT (company_id, endpoint_id, outbox_event_id) DO NOTHING`, records the receipt, and commits immediately.
   - **Claim and dispatch phase:**
     - **Claim (short transaction):** Selects due deliveries (`status = 'PENDING' AND next_attempt_at <= now()` OR `status = 'IN_PROGRESS' AND lease_until < now()`) using cross-tenant round-robin fairness (`ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY next_attempt_at ASC, created_at ASC)`) paired with `SELECT ... FOR UPDATE OF webhook_deliveries SKIP LOCKED`, capped at `min(delivery_batch_size, max_concurrency)`. Updates `status = 'IN_PROGRESS'` and `lease_until = now() + lease_seconds`, then commits to release all database locks.
     - **Dispatch (outside DB locks):** Resolves destination DNS once into `PinnedDestination` and executes asynchronous HTTP POST via `_PinnedIPNetworkBackend` / `_PinnedAsyncHTTPTransport` (`trust_env=False`, `follow_redirects=False`, `Accept-Encoding: identity`, raw streaming byte limit via `aiter_raw()`, and explicit `asyncio.timeout(timeout_sec + 1.0)` bounded within lease duration), emitting single or active-overlap dual-secret HMAC-SHA256 signatures.
     - **Settle (short transaction):** If HTTP status is `2xx`, marks `DELIVERED`, records `delivered_at`, and clears lease. If HTTP non-2xx or connection failure: increments `attempts` and truncates `last_error[:1000]`. If `attempts >= max_attempts`, marks `DEAD`. Otherwise calculates exponential backoff and sets `next_attempt_at`.

---

### 3. Webhook egress security and HMAC signatures

1. **SSRF protection (`app.platform.integration.ssrf`):**
   - Scheme must be `https://` (or `http://` only if `settings.webhook_allow_insecure_http` is explicitly enabled for development/testing).
   - Userinfo (`user:pass@host`) is forbidden.
   - Strict destination port allowlist (`443` only by default; `80` and `443` when `settings.webhook_allow_insecure_http` is enabled).
   - Hostname is resolved via `socket.getaddrinfo`. If any resolved IP is loopback (`127.0.0.0/8`, `::1`), private (RFC 1918 `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, RFC 4193 `fc00::/7`), CGNAT (`100.64.0.0/10`), benchmark (`198.18.0.0/15`), link-local / cloud metadata (`169.254.0.0/16`, `fe80::/10`, `fd00:ec2::254`, Alibaba `100.100.100.200`), IPv4-mapped IPv6, or multicast/reserved/unspecified, delivery is blocked immediately and logged as SSRF error.
   - Resolved public IPs are captured into `PinnedDestination` and injected into `_PinnedIPNetworkBackend` (`_PinnedAsyncHTTPTransport`, `trust_env=False`, `follow_redirects=False`), preventing secondary DNS lookups (closing DNS-rebinding TOCTOU) and ambient proxy bypasses.
2. **HMAC-SHA256 payload signature (`app.platform.integration.signing`):**
   - Delivery sends headers:
     - `X-Delivery-Id`: UUID of `webhook_deliveries`
     - `X-Webhook-Event`: Domain event type string
     - `X-Webhook-Timestamp`: Unix epoch integer timestamp
     - `X-Webhook-Signature`: `t={timestamp},v1={hex_digest}` (with comma-separated secondary `v1=` signature during active secret-rotation overlap windows)
   - Signature is computed as `HMAC-SHA256(signing_secret, f"{timestamp}.".encode() + payload_bytes)`.
   - Verification rejects duplicate `t=` fields, empty key/value pairs, non-digit timestamps, and negative `tolerance_seconds`.
3. **Secret management:**
   - Endpoints are generated with high-entropy `whsec_<random_hex>` secrets.
   - The plain secret is returned **only once** upon endpoint creation or secret rotation (`/rotate-secret`). Subsequent queries return a masked prefix (`whsec_...****`).
4. **Response body protection:**
   - Response payloads from external webhooks are capped at 64 KiB to prevent denial-of-service / memory exhaustion.

---

### 4. Batch data exchange (Import & Export)

1. **Master data batch import (`app.platform.integration.batch`):**
   - Supported entities: `customer`, `product`, `account`.
   - Validates rows via `masterdata.schemas` (`CustomerCreate`, `ProductCreate`, `AccountCreate`) and `app.core.customfields.validate_and_normalize_custom_data` under `masterdata_service.lock_company_unchecked`, preserving literal text (`@`, `+`) without modification on ingest.
   - Isolates each row inside a PostgreSQL savepoint (`session.begin_nested()`) so per-row validation or constraint errors record cleanly without aborting remaining row checks.
   - Supports `dry_run = True` (validates all rows in transaction, records per-row errors, and rolls back cleanly).
   - Default atomic execution: if any row fails validation in non-dry-run mode, the entire batch transaction rolls back, leaving no corrupted partial state.
2. **Business document batch export via Core Port:**
   - Defines `BatchExportProvider` in `app/core/integration.py`.
   - Business modules (`sales`, `inventory`, `receivables`, `ledger`, `masterdata`) register read-only export adapters at the composition root (`app/main.py`), returning `list[dict[str, Any]]` executed inside `company_context(company_id)`.
   - `platform.integration` queries the registered provider through `app.core.integration`, preserving `import-linter` dependency isolation.
   - Supported export entities: `customer`, `product`, `account`, `sales_order`, `invoice`, `payment`, `journal_entry`, `stock_summary`.
3. **CSV formula injection defense (`neutralize_formula` on CSV export only):**
   - Cells containing embedded/leading control chars, invisible Unicode whitespace (`\u200b`..`\ufeff`, `\u00a0`), `|`, `;`, quote-wrapped formula prefixes, non-finite numbers (`+Infinity`, `+NaN`), underscores (`+1_000`), scientific notation (`+1e5`), or starting with `=`, `+`, `-`, `@` (unless strictly matching plain ASCII decimal literals `^[+-]?\d+(?:\.\d+)?$`) are prefixed with a single quote `'` on CSV export to neutralize formula execution in Microsoft Excel and LibreOffice Calc.
4. **Taiwan Excel compatibility:**
   - CSV exports default to plain UTF-8 (`include_bom = False`), with explicit opt-in `include_bom = True` to prepend a UTF-8 Byte Order Mark (BOM: `\ufeff`) when Windows Excel display compatibility is required.
   - CSV inputs automatically detect and strip a leading UTF-8 BOM if present.

---

### 5. Database schema & constraints

Migration `0014_platform_integration.py` creates 4 tables:

```sql
CREATE TABLE webhook_endpoints (
    id UUID NOT NULL,
    company_id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    signing_secret VARCHAR(255) NOT NULL,
    previous_signing_secret VARCHAR(255) NULL,
    previous_secret_expires_at TIMESTAMPTZ NULL,
    secret_prefix VARCHAR(16) NOT NULL,
    event_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    timeout_seconds NUMERIC(5, 2) NOT NULL DEFAULT 10.00,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT pk_webhook_endpoints PRIMARY KEY (id),
    CONSTRAINT fk_webhook_endpoints_company FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    CONSTRAINT uq_webhook_endpoints_company_id UNIQUE (company_id, id),
    CONSTRAINT uq_webhook_endpoints_company_name UNIQUE (company_id, name),
    CONSTRAINT ck_webhook_endpoints_timeout CHECK (timeout_seconds >= 1.00 AND timeout_seconds <= 60.00),
    CONSTRAINT ck_webhook_endpoints_max_attempts CHECK (max_attempts >= 1 AND max_attempts <= 20)
);

CREATE TABLE integration_outbox_receipts (
    outbox_id UUID NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_integration_outbox_receipts PRIMARY KEY (outbox_id),
    CONSTRAINT fk_integration_outbox_receipts_outbox FOREIGN KEY (outbox_id) REFERENCES outbox(id) ON DELETE CASCADE
);

CREATE TABLE webhook_deliveries (
    id UUID NOT NULL,
    company_id UUID NOT NULL,
    endpoint_id UUID NOT NULL,
    outbox_event_id UUID NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NULL,
    last_http_status INTEGER NULL,
    last_error VARCHAR(1000) NULL,
    delivered_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_webhook_deliveries PRIMARY KEY (id),
    CONSTRAINT fk_webhook_deliveries_company_endpoint FOREIGN KEY (company_id, endpoint_id)
        REFERENCES webhook_endpoints(company_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_webhook_deliveries_outbox FOREIGN KEY (outbox_event_id)
        REFERENCES outbox(id) ON DELETE CASCADE,
    CONSTRAINT uq_webhook_deliveries_idempotency UNIQUE (company_id, endpoint_id, outbox_event_id),
    CONSTRAINT ck_webhook_deliveries_status CHECK (status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'DEAD'))
);

CREATE INDEX ix_webhook_deliveries_due ON webhook_deliveries (next_attempt_at)
    WHERE status IN ('PENDING', 'IN_PROGRESS');

CREATE TABLE integration_batch_jobs (
    id UUID NOT NULL,
    company_id UUID NOT NULL,
    job_type VARCHAR(32) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    format VARCHAR(16) NOT NULL DEFAULT 'CSV',
    status VARCHAR(32) NOT NULL,
    dry_run BOOLEAN NOT NULL DEFAULT false,
    total_rows INTEGER NOT NULL DEFAULT 0,
    succeeded_rows INTEGER NOT NULL DEFAULT 0,
    failed_rows INTEGER NOT NULL DEFAULT 0,
    error_summary JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT pk_integration_batch_jobs PRIMARY KEY (id),
    CONSTRAINT fk_integration_batch_jobs_company FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    CONSTRAINT ck_integration_batch_jobs_type CHECK (job_type IN ('IMPORT', 'EXPORT')),
    CONSTRAINT ck_integration_batch_jobs_status CHECK (status IN ('COMPLETED', 'FAILED', 'DRY_RUN'))
);
```

---

### 6. Permissions and REST API

Migration `0014` registers 3 permissions in `permissions` and grants them to the `admin` role:
- `platform.integration.read`: View webhook endpoints, delivery logs, batch job status, and run exports.
- `platform.integration.manage`: Create/edit/deactivate webhooks, rotate secrets, and retry dead deliveries.
- `platform.integration.import`: Execute batch master data imports.

Catalog count expands from 52 to **55 permissions**.

#### REST endpoints under `/api/v1/integrations`:
- `GET /api/v1/integrations/webhooks` (`platform.integration.read`)
- `POST /api/v1/integrations/webhooks` (`platform.integration.manage`)
- `GET /api/v1/integrations/webhooks/{endpoint_id}` (`platform.integration.read`)
- `PATCH /api/v1/integrations/webhooks/{endpoint_id}` (`platform.integration.manage`)
- `DELETE /api/v1/integrations/webhooks/{endpoint_id}` (`platform.integration.manage` — soft deactivation `is_active = False`)
- `POST /api/v1/integrations/webhooks/{endpoint_id}/rotate-secret` (`platform.integration.manage`)
- `GET /api/v1/integrations/deliveries` (`platform.integration.read`)
- `GET /api/v1/integrations/deliveries/{delivery_id}` (`platform.integration.read`)
- `POST /api/v1/integrations/deliveries/{delivery_id}/retry` (`platform.integration.manage`)
- `POST /api/v1/integrations/imports` (`platform.integration.import`)
- `POST /api/v1/integrations/exports` (`platform.integration.read`)
- `GET /api/v1/integrations/jobs` (`platform.integration.read`)
- `GET /api/v1/integrations/jobs/{job_id}` (`platform.integration.read`)

---

### 7. Import-linter contracts

`pyproject.toml` adds Contract 14:
```toml
[[tool.importlinter.contracts]]
name = "platform.integration may depend on core and masterdata only"
type = "forbidden"
source_modules = ["app.platform.integration"]
forbidden_modules = [
    "app.modules.sales",
    "app.modules.inventory",
    "app.modules.receivables",
    "app.modules.ledger",
    "app.plugins",
    "app.platform.permissions",
    "app.platform.plugins",
    "app.platform.customfields",
    "app.platform.workflow",
    "app.main",
    "app.cli",
]
```
`"app.platform.integration"` is registered under `protected_modules` to prevent bare module traversal.

---

## Action Items (Week 13 Implementation Sequence)

1. [x] **Slice A — Architecture ADR, Core port, Migration 0014, and import-linter contracts:**
   - Create `docs/adr/ADR-013-platform-integration.md`.
   - Create `app/core/integration.py` (`BatchEntityType`, `BatchExportProvider`, registration functions).
   - Update `app/core/settings.py` for Webhook/batch settings.
   - Add Alembic migration `0014_platform_integration.py` (4 tables, constraints, 3 permissions granted to `admin`).
   - Add Contract 14 in `pyproject.toml` and move `httpx` to `[project.dependencies]`.
   - Unit tests for core port and migration upgrade/downgrade (`tests/core/test_integration_port.py`, `tests/test_migrations.py`).
2. [x] **Slice B — `platform.integration` models, SSRF/HMAC security, and Webhook Admin API:**
   - Create `app/platform/integration/{models,schemas,ssrf,signing,service,router}.py`.
   - Wire router in `app/main.py`.
   - Unit and router tests for Webhook CRUD, secret rotation, delivery listing, and dead retry (`tests/platform/test_integration_service.py`, `tests/platform/test_integration_router.py`).
3. [x] **Slice C — Outbox Worker, batch import/export engine, Core export adapters, and CLI tooling:**
   - Create `app/platform/integration/worker.py` and `app/cli/run_outbox_worker.py`.
   - Create `app/platform/integration/batch.py` and `app/cli/manage_integrations.py`.
   - Implement read-only export providers in `app/main.py` for business entities and expose batch/job REST endpoints.
   - Tests for outbox worker claiming, lease expiry, backoff, and CSV/JSON import/export (`tests/platform/test_integration_worker.py`, `tests/platform/test_integration_batch.py`, `tests/cli/test_manage_integrations.py`).
4. [x] **Slice D — Concurrency proofs, anti-SSRF/anti-injection tests, adversarial review, and D.5 security closure:**
   - Concurrency & hardening tests: dual-worker `FOR UPDATE SKIP LOCKED`, cross-tenant round-robin fair claim, lease crash-recovery, IP-pinned DNS-rebinding defense, decompression-bomb/Slow-Loris limits, dual-secret overlap emission, and hardened CSV formula injection defense (`tests/platform/test_integration_concurrency.py`).
   - Run adversarial security audit subagent, remediate all HIGH/MEDIUM findings (documenting `VULN-06` Accepted Risk in `threat-model.md`), and verify via final adversarial re-review.
   - Update `threat-model.md`, `README.md`, `README.zh-TW.md`, `SECURITY.md`, `HANDOFF.md`, `docs/adr/README.md`, and D.5 security gate manifest.
