# mini-ERP Security Threat Model

Status: Slice D.5 baseline draft; reviewed 2026-08-24. This document is scan context, not a certification.

## System and assets

- The API serves company-scoped ERP data backed by PostgreSQL; CLI/bootstrap paths can access the same domain services.
- Assets: credentials and sessions (A-001), actor/company/role/permission state (A-002), company-scoped masterdata and transactions (A-003), ledger and receivable balances (A-004), database and migration integrity (A-005), audit and operational evidence (A-006).
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

- SI-001 route classification is exactly `4 Public / 2 Authenticated / 52 RequiresPermission / 0 invalid`.
- SI-002 permission catalog parity is `44 migration / 44 routes / 44 services`.
- SI-003 Public means bearer authentication is optional; it does not make input or data unrestricted.
- SI-004 Authenticated lifecycle operations require the current actor and owned current session.
- SI-005 Cross-company reads and writes fail closed; target-company access is re-authorized on switch.
- SI-006 Missing actor, session, ownership, entitlement, or permission context never falls back to allow.

## Abuse cases and evidence

- AB-001 forged company header attempts tenant escape; control: server-derived actor/company context; evidence: cross-company isolation tests.
- AB-002 session from another actor attempts switch/logout; control: session ownership check; evidence: permissions service tests.
- AB-003 user switches to an unentitled company; control: destination entitlement check; evidence: switch-company tests.
- AB-004 caller reaches a high-impact mutation without a permission; control: route/service defense in depth; evidence: authorization and module tests.
- AB-005 disabled or unauthenticated caller reaches protected data; control: authentication and route markers; evidence: smoke/auth tests.
- AB-006 repeated public login attempts consume API resources; controls: a 16 KiB request-body cap, a 1,024-character password cap, a process-local per-peer limit of 10 schema-valid attempts per 60 seconds, no more than four concurrent login service/Argon2 executions per process, and Argon2 verification offloaded from the async event loop; evidence: permissions router and service tests.

## Gaps and accepted risks

Token rotation/expiry, database RLS, production TLS/CORS, external secret scanning, and deployment hardening are not assumed by this document; they remain evidence gaps for the staging hardening phase. The login limiter is process-local and keys the direct peer address: multiple workers/replicas do not share its counters, and callers behind one reverse proxy share that proxy's quota because forwarded headers are not trusted. Production ingress must provide an appropriate shared or edge rate limit before scale-out or proxy deployment. Any accepted risk requires an owner, rationale, compensating control, and expiry date. Critical and High findings are not accepted as a D.5 pass.

## Review triggers

Review this model when authentication, tenancy, permissions, session lifecycle, migrations, integrations, deployment, or data-export behavior changes; otherwise review at each release-hardening checkpoint.
