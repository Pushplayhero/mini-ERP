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

## What Phase 2 implemented (ADR-010 & ADR-011)

- **Plugin governance (ADR-010)**: Phase 2 added a deterministic Kahn topological plugin loader (with SemVer range compatibility, entry-point discovery, and dependency declarations), per-company tenant enablement and JSONB configuration governance (`/api/v1/plugins`, `company_plugins`), and CI-enforced import-linter contracts ensuring plugins touch only public service/schemas layers rather than internal ORM models or routers. Untrusted code sandboxing remains a deliberate non-goal (plugins are trusted in-process code). See `app/plugins/README.md` for details.
- **Custom fields defense-in-depth (ADR-011)**: Tenant-governed custom fields across 8 primary transactional entities (`Customer`, `Product`, `Account`, `AccountingPeriod`, `JournalEntry`, `SalesOrder`, `Invoice`, `Payment`) enforce fail-closed runtime validation (`extra="forbid"` rejects unknown keys with 422), 5 PostgreSQL DDL CHECK constraints on definition schema, tenant root row serialization (`lock_company_unchecked` with `SELECT ... FOR UPDATE`) eliminating TOCTOU race conditions and write-skew under concurrent definition mutations, Banker's Rounding (`ROUND_HALF_EVEN`) to `NUMERIC(20, 6)` precision, and dependency inversion via `app.core.customfields` preventing business modules from importing platform implementations. Automated journal entry postings remain fail-safe and unblocked by manual custom field definitions.

## Reporting a vulnerability

This is a Phase 1 educational/reference kernel, not (yet) a project with a
formal disclosure process or security contact. Do not publish sensitive
security findings, raw scanner output, source excerpts, credentials, or
proofs of concept in a public issue. Send them only to an authorized project
owner through a private security channel; if no private channel is available,
keep the material offline and request one before disclosure. Non-sensitive
documentation issues may still be reported publicly after confirming that no
security detail is exposed.
