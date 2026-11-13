# mini-ERP Security Threat Model

Status: Slice D.5 baseline (reconciled through Week 13 Slice D / ADR-013). This document is scan context, not a certification.

## System and assets

- The API serves company-scoped ERP data backed by PostgreSQL; CLI/bootstrap paths can access the same domain services.
- Assets: credentials and sessions (A-001), actor/company/role/permission state (A-002), company-scoped masterdata and transactions (A-003), ledger and receivable balances (A-004), database and migration integrity (A-005), audit and operational evidence (A-006), webhook signing secrets and delivery history (A-007), batch import/export job records and data files (A-008).
- Actors: authenticated users (AC-001), disabled or unauthenticated clients (AC-002), compromised user sessions (AC-003), operators running trusted CLI/bootstrap code (AC-004), and external callers controlling HTTP input (AC-005).

## Entry points and trust boundaries

- EP-001 HTTP routes; EP-002 bearer/session authentication; EP-003 current-company and tenant context; EP-004 role/catalog permission checks; EP-005 company switching; EP-006 session logout; EP-007 CLI/bootstrap and migrations; EP-008 PostgreSQL and deployment configuration.
- TB-001 client to HTTP: all input is untrusted; a company header is never tenant authority.
- TB-002 bearer to actor/session: authentication must resolve an active actor and current session; missing context fails closed.
- TB-003 route to service: each route has exactly one marker: Public, Authenticated, or RequiresPermission.
- TB-004 actor/company to tenant rows: queries and mutations remain company-scoped.
- TB-005 switch target to entitlement: switching re-checks destination-company entitlement.
- TB-006 current session to lifecycle mutation: switch and logout operate only on an actor-owned current session.
- TB-007 role/catalog to business mutation: permission checks are enforced at route and service boundaries where applicable.
- TB-008 CLI/bootstrap/migration to database: operator paths are privileged and must not be exposed as ordinary user authority.

## Security invariants

- SI-001 route classification is exactly `4 Public / 2 Authenticated / 79 RequiresPermission / 0 invalid` (after Slice E moved 5 masterdata write endpoints to CLI, ADR-010 Slice B added 3 plugin governance endpoints, ADR-011 Slice B added 5 custom-field governance endpoints, ADR-012 Slice B added 11 workflow governance endpoints, and ADR-013 Slices B–D added 13 integration webhook, delivery, and batch import/export governance endpoints across 85 application `APIRoute`s).
- SI-002 permission catalog parity is `55 migration (54 active + 1 retained session.manage)` (including `platform.plugin.{read,manage}`, `platform.customfield.{read,manage}`, `platform.workflow.{read,manage,approve}`, and `platform.integration.{read,manage,import}`).
- SI-003 Public means bearer authentication is optional; it does not make input or data unrestricted.
- SI-004 Authenticated lifecycle operations require the current actor and owned current session.
- SI-005 Cross-company reads and writes fail closed; target-company access is re-authorized on switch.
- SI-006 Missing actor, session, ownership, entitlement, or permission context never falls back to allow.
- SI-007 Critical business state transitions (`sales_order.confirm`, `sales_order.cancel`, `invoice.void`, `payment.void`, `journal_entry.reverse`) require a matching, unconsumed `APPROVED` workflow instance when an active workflow definition has matching step thresholds; `submit_workflow_instance` resolves target amounts strictly server-side from locked Core tables scoped to `company_id`, `approve_workflow_instance` enforces active definitions, step RBAC, and Separation of Duties (`allow_self_approval=False`), `submit` and `verify` serialize against definition edits via `FOR SHARE`, deactivating a definition cancels bound active instances, and `verify_workflow_approval_unchecked` validates amount, snapshot step/permission parity, and SoD logs before transitioning instances to `CONSUMED` atomically inside the caller transaction under `SELECT ... FOR UPDATE`.
- SI-008 platform.integration enforces defense-in-depth at both API/service and outbox worker layers:
  - Service functions enforce `require_permission(...)` directly (`platform.integration.{read,manage,import}`); webhook `DELETE` performs soft deactivation (`is_active=False`) and cancels queued `PENDING` deliveries rather than deleting audit history.
  - Webhook URLs undergo strict scheme/userinfo/port allowlist validation (`443` only by default, `80`/`443` if `webhook_allow_insecure_http=True`), ASCII IDNA Punycode normalization (`_normalize_hostname`), and DNS IP-range checks (blocking RFC 1918, loopback, CGNAT `100.64.0.0/10`, benchmark `198.18.0.0/15`, link-local/cloud metadata `169.254.169.254` / `fd00:ec2::254` / Alibaba `100.100.100.200`, and IPv4-mapped IPv6). Before outbound HTTP dispatch, DNS resolves once into `PinnedDestination` and `_PinnedIPNetworkBackend` + `_PinnedAsyncHTTPTransport` (`trust_env=False`, `follow_redirects=False`) connect TCP directly to the verified pinned IP without a second DNS resolution; transient DNS resolution errors (`TransientDNSResolutionError` on `socket.gaierror`) use bounded exponential retry backoff, whereas SSRF policy violations fail permanently as `DEAD`.
  - Outbound HTTP responses request `Accept-Encoding: identity`, stream raw wire bytes via `aiter_raw()` bounded by `webhook_max_response_bytes`, enforce a wall-clock `asyncio.timeout(timeout_sec + 1.0)` bounded under lease expiry, and truncate persisted `last_error` strings to 1,000 characters.
  - Outbox fan-out uses `FOR UPDATE OF outbox SKIP LOCKED` with per-row savepoint isolation, JSONB dict validation, and idempotent `pg_insert(...).on_conflict_do_nothing` receipts; delivery claiming uses cross-tenant round-robin ordering (`ROW_NUMBER() OVER (PARTITION BY company_id ...)`) paired with `FOR UPDATE OF webhook_deliveries SKIP LOCKED`, bounded by `max_concurrency`.
  - Webhook signatures emit `X-Webhook-Signature: t=...,v1=...` using HMAC-SHA256 over `${timestamp}.${payload}`, support bounded dual-secret overlap (`previous_signing_secret`, `previous_secret_expires_at`), and reject duplicate `t=` keys or negative `tolerance_seconds`.
  - Batch imports validate RBAC, cap request/content size and rows (1 MB, `BatchImportRequest.content` `max_length=1_048_576`, 2,000 rows), translate `csv.Error` into HTTP `422` (`BatchImportValidationError`), preserve literal text without `'` corruption, isolate per-row savepoints (rejecting non-dict rows and ragged CSV rows cleanly), and validate/normalize `custom_data` via `validate_and_normalize_custom_data`; exports bind `company_context(company_id)` around registered providers, default to `include_bom=False`, and apply hardened `neutralize_formula` escaping on CSV export (`=`, `+`, `-`, `@`, `|`, `;`, control chars, full Unicode whitespace/separator/format chars `isspace()` + `Cf`/`Cc`/`Zs`/`Zl`/`Zp`, quote wrappers, and non-ASCII/non-finite/non-decimal tokens via `re.ASCII`).

## Abuse cases and evidence

- AB-001 forged company header attempts tenant escape; control: server-derived actor/company context; evidence: cross-company isolation tests.
- AB-002 session from another actor attempts switch/logout; control: session ownership check; evidence: permissions service tests.
- AB-003 user switches to an unentitled company; control: destination entitlement check; evidence: switch-company tests.
- AB-004 caller reaches a high-impact mutation without a permission; control: route/service defense in depth; evidence: authorization and module tests.
- AB-005 disabled or unauthenticated caller reaches protected data; control: authentication and route markers; evidence: smoke/auth tests.
- AB-006 repeated public login attempts consume API resources; controls: a 16 KiB request-body cap, a 1,024-character password cap, a process-local per-peer limit of 10 schema-valid attempts per 60 seconds, no more than four concurrent login service/Argon2 executions per process, and Argon2 verification offloaded from the async event loop; evidence: permissions router and service tests.
- AB-007 requester self-approves, spoofs document amounts, races concurrent approvals, modifies order prices or tightens workflow steps/SoD policy after approval, reactivates a deactivated definition to resurrect stale approvals, or replays a consumed approval instance; controls: SoD enforcement (`403`), server-side `WorkflowTargetResolver` under target row lock, `WorkflowDefinition` `FOR SHARE` / `WorkflowInstance` `SELECT ... FOR UPDATE` and `uq_workflow_instance_active`, active-instance cancellation on deactivation, post-approval `target_amount`, `steps_snapshot` signature, and SoD log verification (`409`), and single-transaction `CONSUMED` transition; evidence: workflow service, business integration, and concurrency/anti-replay tests (`tests/platform/test_workflow_concurrency.py`).
- AB-008 malicious tenant registers loopback/metadata webhook URL or non-standard port, triggers DNS rebinding TOCTOU to internal infrastructure, returns a gzip decompression bomb or Slow-Loris tarpit stream, floods deliveries to starve sibling tenants, attempts spreadsheet formula injection bypasses (`\u200b=1+1`, `|calc`, `;`, `+Infinity`, quote wrappers) in CSV exports, or probes cross-tenant/unprivileged batch and service endpoints; controls: `resolve_and_pin_webhook_url` + `_PinnedIPNetworkBackend` (`_PinnedAsyncHTTPTransport`, `trust_env=False`, `follow_redirects=False`, zero secondary DNS lookups), strict port allowlist (`443` / optional `80`), `Accept-Encoding: identity` + `aiter_raw()` response-byte capping + `asyncio.timeout`, `ROW_NUMBER()` fair cross-tenant `SKIP LOCKED` worker claim with `max_concurrency` Semaphore, dual-secret HMAC rotation (`previous_signing_secret`) with single-timestamp parser hardening, service-layer `require_permission(...)` + savepoint isolation + `custom_data` validation on import, and export-only hardened `neutralize_formula`; evidence: integration service, router, worker, batch, and concurrency/security regression tests (`tests/platform/test_integration_*.py`).

## Gaps and accepted risks

Token rotation/expiry, database RLS, production TLS/CORS, external secret scanning, and deployment hardening are not assumed by this document; they remain evidence gaps for the staging hardening phase. The login limiter is process-local and keys the direct peer address: multiple workers/replicas do not share its counters, and callers behind one reverse proxy share that proxy's quota because forwarded headers are not trusted. Production ingress must provide an appropriate shared or edge rate limit before scale-out or proxy deployment. Any accepted risk requires an owner, rationale, compensating control, and expiry date. Critical and High findings are not accepted as a D.5 pass.

- **VULN-06 (Accepted Medium Risk — Plaintext `webhook_endpoints.signing_secret` and `previous_signing_secret` stored in PostgreSQL)**:
  - **Owner**: Project Maintainer.
  - **Rationale**: Outbound workers must possess the raw HMAC-SHA256 signing key to compute valid `X-Webhook-Signature` headers, and the Phase 2 API kernel intentionally avoids introducing new cryptography/KMS runtime dependencies into `pyproject.toml`.
  - **Compensating Controls**: HTTP API responses expose `signing_secret` strictly one time upon creation (`201 Created`) and secret rotation (`POST /rotate-secret`), never return `previous_signing_secret` in any schema, display only `secret_prefix` in list/detail/update/deactivation views, enforce tenant scoping + RBAC (`platform.integration.{read,manage}`), and rely on PostgreSQL credential/disk encryption boundaries (EP-008).
  - **Expiry Date**: Phase 3 production hardening checkpoint (re-evaluate envelope encryption via KMS / Fernet key management).

## Review triggers

Review this model when authentication, tenancy, permissions, session lifecycle, migrations, integrations, deployment, or data-export behavior changes; otherwise review at each release-hardening checkpoint.
