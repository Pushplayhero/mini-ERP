# HANDOFF — mini-erp (D.5 u12 scanner gate hardening — 2026-10-12)

Updated 2026-10-12. This is the current handoff status; the dated sections below are retained as historical project context and may describe earlier repository states.

## Product and repository status — 2026-10-12

### Product

mini-ERP is an API-only, self-hostable modular ERP kernel for Taiwanese SMEs. The implemented Phase 1 order-to-cash flow covers master data, sales orders, shipping and inventory, receivables, payments, and double-entry ledger posting. It uses Python 3.12, FastAPI, async SQLAlchemy/asyncpg, PostgreSQL 16, and Alembic. Tenant isolation is fail-closed; posted financial state and stock movements use append-only/auditable patterns; domain events connect business modules while import-linter enforces module boundaries. ADR-009 platform authentication and permissions are implemented.

Phase 1 is described as complete in both READMEs. The product is still an API kernel: there is no frontend, dynamic plugin loader or sandbox, workflow/approval engine, admin custom-field UI, or Taiwan tax/e-invoice integration. The in-process credit-limit plugin is trusted code and must not be treated as sandboxed. Long-term Platform → Suites → Taiwan localization plans are in `docs/open-erp-master-plan.md`.

### Repository checkpoint and GitHub publication

- The D.5 work was based on parent SHA `b56c164e6e9a7c589e9253e42db96048af197b82` in `C:\Program Files\mini-ERP\mini-erp\.worktrees\d5-u12-verify-3b5d4d7`.
- After the handoff update commit, publish this checkpoint as topic branch `codex/d5-u12-gate-hardening-20261012`; do not update `main`.
- After the handoff update commit, this branch will contain 13 commits after local `main`. Local `main` is itself 11 commits ahead of `origin/main`, so publishing this topic branch makes those 24 commits reachable on GitHub. The branch does not change `main`.
- The D.5 gate-hardening commit is `95c53820a9889c659c34d39c3d1460ec0b66b6d6`; this product-state update is a separate handoff commit on top.

### Security scan status

The owner confirmed that completed u01–u11 scans remain acceptable pass evidence even when their exact SHAs differ. Preserve that evidence; do not restart the sequence at u01. The policy text in `docs/security/scan-policy.md` still contains older wording that says different-SHA reports do not cover the new baseline; reconcile that text and artifact aggregation behavior with the owner's explicit decision before making an aggregate gate claim.

| Unit | Evidence and current disposition |
| --- | --- |
| u01–u11 | Completed scan evidence is accepted by the owner. Local retained runner receipts were found for u01, u02, and u03; u04–u11 evidence must be read from the retained records already maintained for those units. |
| u12_gate | Scan at `b56c164e6e9a7c589e9253e42db96048af197b82`, cost **US$9.145969**, returned one validated High finding and exit 1; runner disposition was no-pass (`coverage_invalid`, `finding_invalid`, `scanner_nonzero_exit`). The source finding was repository-controlled fsmonitor execution before Git isolation. This checkpoint adds sterile/bounded Git execution and exact `codex-security-plugin@0.1.22` receipt-free coverage compatibility tests, but it has not been rescanned; no new scan pass is established. |
| u13_governance | Scan at `454b2e7d90f1751f29a79da40b332b405567f712`, cost **US$11.838334**, hit the cost cap with partial coverage and exit 2. Four validated findings: one Medium for predictable bootstrap credentials and three Low findings for mutable CI actions with repository-write authority, coverage execution under repository-write credentials, and password examples exposing substituted secrets through shell history/curl arguments. These remain the next remediation work; no complete scan or pass evidence exists for the remediation. |
| u14_dependencies | No retained scan result was found in the local security-results inventory. It remains unverified. The prior sequence stops at u13 because that scan was incomplete and reported findings. |

The overall D.5 gate is **not passed**. The D.5 manifest is `docs/security/d5-shards.json`; the runner and artifact contract are in `scripts/d5_security_gate.py`; policy is in `docs/security/scan-policy.md`. Private scan receipts and reports live under `C:\Users\COSH\.codex\security-results` and must remain private. The configured per-unit maximum is US$15 with cumulative ceiling fields unset; report actual spend per unit. Never reuse authorization, claim, or budget records across a different SHA or unit. Any new paid scan needs authorization bound to the exact commit SHA, unit, inventory, and profile.

### Latest implementation and verification

This checkpoint hardens Git subprocess isolation in `scripts/d5_security_gate.py`, supports only the narrowly sealed receipt-free coverage form emitted by `codex-security-plugin@0.1.22`, adds regression tests, and documents the gate contract in `docs/security/scan-policy.md`.

On the code checkpoint, `tests/security/test_d5_security_gate.py` reported **278 passed, 3 skipped in 484.03s**. Ruff check and format check passed; mypy strict passed using `python -m mypy` (the uv trampoline could not canonicalize its script path); import-linter reported **9 kept, 0 broken**; `git diff --check` passed. No paid scan was launched for this checkpoint, and no prior scan artifact, paid claim, or authorization was altered.

### Next work for Claude Code

1. Fix and verify the four u13 governance findings above. Keep credentials out of command history and curl arguments, replace predictable quick-start secrets with a safe first-run flow, pin CI actions to reviewed immutable references, and prevent untrusted project/dependency code from running with repository-write credentials.
2. Review the u12 gate-hardening commit and run non-paid static/regression verification on the final handoff commit. Treat the old u12 scan as no-pass; do not claim that the code tests substitute for a new security scan.
3. After u13 remediation and its required review, resume the manifest sequence at the appropriate unresolved unit. u14 remains unverified. Do not skip the stop-on-finding rule or start a paid scan without exact-SHA authorization.
4. Keep the overall D.5 result as no-pass until the required unit evidence and governance dispositions are complete.

---

## Historical handoff — Slice D and initial D.5 baseline (updated 2026-08-25)

Slice D and the initial D.5 baseline were checkpointed at `1db8ed9`; strict-scan policy and wording corrections were checkpointed at `fd9a5d6`. The remainder of this historical section records the owner-approved scan-cap outcomes and WEEK9 work as they stood in August 2026.

## Slice D.5 Security Baseline — implementation status

The D.5 security architecture is documented in `docs/security/threat-model.md` and `docs/security/scan-policy.md`. Current status: **canary passed; standard scan no-pass on cost cap**. CLI info, login, and dry-run preflight completed. Canary scan `1d73ff69` reviewed 26/26 files with complete coverage, 0 findings, exit 0, and cost US$0.6886922; it is diagnostic because it ran before the strict `approval_policy="never"` correction.

Planned execution uses pinned `@openai/codex-security@0.1.18` with local ChatGPT authentication, a clean exact-SHA detached worktree, repo-external private state/results, a US$1 path canary followed by an owner-approved bounded standard budget, and 30-day artifact retention. Critical/High findings block; Medium findings require an owner and deadline; exit 2/130/143 or incomplete coverage is no-pass. CI, SARIF, patching, publish, hooks, and deep scan remain deferred.

The previous `290 passed` full-suite result remains historical evidence; the corrected exact-SHA `fd9a5d6` verification also passed 290 tests. The first standard attempt used strict approval policy from a temporary backed-up Codex config, reviewed the full 170-file target, and stopped at estimated US$5.056012 against the US$5 cap before sealing a report. After the owner-approved cap increase to US$10, the rerun again stopped before sealing at estimated US$10.076331. A US$25 rerun was interrupted at US$23.249999 when the owner raised the cap to US$35; the US$35 rerun reached 54/170 files and stopped at estimated US$35.011233 with partial output only. All standard attempts have incomplete coverage and are no-pass. Private output directories are `C:\Users\COSH\.codex\security-results\mini-erp-fd9a5d6-standard`, `C:\Users\COSH\.codex\security-results\mini-erp-fd9a5d6-standard-10`, `C:\Users\COSH\.codex\security-results\mini-erp-fd9a5d6-standard-25`, and `C:\Users\COSH\.codex\security-results\mini-erp-fd9a5d6-standard-35`; no findings artifact is sealed. The pre-existing deletion of `.claude/review-state.md` remains excluded from checkpoint commits and scan knowledge-base inputs. Sol review findings were corrected; another standard run requires a new budget decision and a more bounded scope or execution plan.

## 2026-08-24 Luna High re-review summary

- Fresh evidence this round: `.venv` pytest `48 passed in 20.24s`.
- Fresh evidence this round: Ruff check passed; `git diff --check` passed with CRLF warnings only.
- Fresh evidence this round: catalog `44/44/44`; route classification `58 routes = 4 Public / 2 Authenticated / 52 RequiresPermission / 0 unclassified`.
- Historical evidence from the prior verification round: full pytest `290 passed in 162.54s`; prior format, mypy, and import-linter checks passed.
- Runner note: two `uv` trampoline failures were environment/runner issues, not code findings; the `.venv` command completed successfully.
- Final disposition: initial checkpoint not approved pending the corrected strict-scan rerun; no scan finding was reported by canary. This HANDOFF does not claim APPROVED.
## Slice D correction — 2026-08-24

### Final reviewer findings

- **P3 documentation-only:** `tests/masterdata/test_conflict_handling.py` (around lines 10–15 and 155–160) and `tests/masterdata/test_reference_data_api.py` (around line 3) retain stale pre-cutover header/HTTP-409 wording. Tests are intentionally unchanged in this round.
- **P3 documentation-only:** the HANDOFF service-boundary summary overstated permission enforcement for all public platform services; corrected below to distinguish lifecycle ownership, auth/bootstrap boundaries, and permission-checked business operations.


The authenticated session lifecycle correction is verified in the working copy. Non-Tier-1 routes are classified exactly once as `Public()`, `Authenticated()`, or `RequiresPermission(code)`; historical binary and `session.manage` lifecycle statements remain review narrative only.

`POST /auth/switch-company` and `POST /auth/logout` now use `Depends(Authenticated())`.
The service functions require actor presence, current session context, and actor/session
ownership; switch still validates target entitlement and logout still deletes the owned
session. Migration 0009 and the `session.manage` catalog/admin grants are unchanged.
Earlier binary and `session.manage` lifecycle statements in the historical recap are
superseded by this dated correction.
## Historical repository state (2026-08-25; superseded by the status above)

**HEAD: `ade0769`** — "Rewrite HANDOFF.md for WEEK9 Slice A/B/C completion, Slice D next". Local `main` is **4 commits ahead of `origin/main`** (`ade0769`, `1507084` Slice A, `49be26a` Slice B, `1c35d85` Slice C) — **none pushed**. Working tree contains the complete, uncommitted Slice D changes across application routers, services, middleware, CLI tools, migration 0010, and tests.

**Verification evidence:** fresh round evidence is recorded above; prior full pytest remains historical evidence.
- Focused correction pytest: `48 passed`.
- Full pytest: `290 passed in 162.54s`.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: **129 files already formatted**.
- `uv run python -m mypy .`: passed.
- `uv run python -m importlinter.cli`: passed.
- `git diff --check`: passed.
## Prior work recap (WEEK9 Slices A–C)

### Slice A — migration 0009 + `app/core/authorization.py` (commit `1507084`)
Six RBAC tables (`users`/`roles`/`permissions`/`role_permissions`/`user_company_roles`/`sessions`), minimal placeholder catalog (`admin` role + `session.manage`), `ActorContext`, `ContextVar` management, `actor_context()`, `require_permission()`, and `Public`/`Authenticated`/`RequiresPermission` factories.

### Slice B — `platform.permissions` package + `bootstrap_admin.py` (commit `49be26a`)
Authentication service (`login` with Argon2id + dummy timing protection, `resolve_actor`, `switch_company`, `logout`), context management (`current_session_id`), `/auth` router, and single-transaction advisory-locked `bootstrap_admin.py`.

### Slice C — import-linter contracts (commit `1c35d85`)
Configured 9 import-linter contracts in `pyproject.toml`, introducing `protected` contracts to enforce strict isolation between business modules, core, and platform.

## Slice D implementation & review summary (This session)

### 1. Architectural Changes Implemented

1. **Full Permission Catalog & Migration 0010** (`alembic/versions/0010_platform_permissions_full_catalog.py`):
   - Added 44 granular endpoint permission codes across `masterdata`, `sales`, `ledger`, `inventory`, and `receivables`.
   - Granted all 44 permissions to the `admin` role (alongside `session.manage` from Migration 0009, totaling 45 unique permissions).
2. **`AuthenticationMiddleware` & Ambient Context Binding** (`app/main.py`):
   - Replaced Phase 1 header-based `tenancy_middleware` with fail-closed Bearer session authentication.
   - Extracts and validates `Authorization: Bearer <token>` (case-insensitive for `bearer`, `Bearer`, `BEARER`).
   - Resolves active token via `permissions_service.resolve_actor`, binding `ActorContext`, `current_session_id`, and `company_context` per request with guaranteed cleanup in `finally:`.
3. **Startup Route Classification & Shadowing Defense** (`app/main.py`):
   - `_classify_routes_or_crash(app)` inspects all 58 application `APIRoute`s at startup:
     - 4 `Public()` routes: `/health`, `/api/v1/uom`, `/api/v1/currencies`, `/auth/login`.
     - 2 `Authenticated()` routes: `/auth/switch-company`, `/auth/logout`.
     - 52 `RequiresPermission(...)` routes covering all business and platform endpoints.
   - Strictly enforces that Tier-1 documentation routes (`/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`) originate only from `fastapi` framework endpoints, rejecting custom `APIRoute`s or Starlette `Route`/`Mount` shadows with a startup crash.
4. **Service-Layer Enforcement & Unchecked Splits**:
   - `switch_company()` and `logout()` use authenticated actor presence plus current-session ownership; switch also re-checks target-company entitlement. `login()`/`resolve_actor()` and bootstrap helpers have their own public/bootstrap boundaries. Other business mutation/read functions enforce permission checks.
   - Explicit `_unchecked` splits provided for intra-transaction lookups and CLI utilities (`load_company_unchecked`, `list_companies_unchecked`, etc.).
5. **CLI & Operational Tooling**:
   - `bootstrap_admin.py`: Enforces functional currency invariant (`TWD`), validates credentials, and prevents duplicate executions.
   - `seed_demo.py`: Fails closed if multiple companies exist without `--company-code` / `BOOTSTRAP_COMPANY_CODE`, and enforces `functional_currency_code == "TWD"`.
   - `demo_o2c.py`: Refactored to authenticate against `/auth/login` via HTTP client and drive live REST endpoints.
   - `Makefile` & `docker-compose.yml`: Propagate credentials purely via `docker compose exec -T -e ...` without exposing secrets in process arguments.
6. **Test Suite Migration & HTTP Route Verification**:
   - `tests/_auth_helpers.py`: Provides `setup_company_with_admin`, `login_user`, and token caching. Ensures **all** company creations in tests (including the initial anchor) are executed via `POST /api/v1/companies` to thoroughly exercise HTTP authentication and permission checks (`masterdata.company.create`).
   - `tests/conftest.py`: Updated `TEST_ADMIN_ACTOR` with the complete 45-permission set, eliminating drift.

### 2. 14 Rounds of Codex Luna 5.6 Review

All review feedback from `codex.cmd exec -m gpt-5.6-luna` was rigorously addressed:
- **Round 1–7**: Handled initial route classification, session ownership verification, and service boundary splits.
- **Round 8**: Added fail-closed company selection in `seed_demo.py` and case-insensitive Bearer header parsing in middleware.
- **Round 9**: Enforced `TWD` functional currency validation in bootstrap/seed scripts; routed test company creations through `POST /api/v1/companies`.
- **Round 10–11**: Removed untrusted self-attestation review markers; modernized docstrings in `app/main.py`; synchronized test token cache on company switch.
- **Round 12**: Strengthened Tier-1 route classification to detect and reject custom Starlette `Route` shadowing.
- **Round 13–14**: Updated `Makefile` `seed` target with `BOOTSTRAP_COMPANY_CODE`; synchronized `TEST_ADMIN_ACTOR` with the complete 45-permission catalog; enforced HTTP-based company creation across all test suites.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata / ledger / sales / inventory+shipping / receivables | committed, Codex-reviewed | ADR-001–ADR-008 |
| Week 7/8 hardening + polish | done | WEEK7/WEEK8 briefs |
| Phase 2 overview | ACCEPTED, no code | `docs/adr/PHASE2-platform-architecture-overview.md` |
| `platform.permissions` — ADR | **ACCEPTED** (Codex round 8) | `docs/adr/ADR-009-platform-permissions.md` |
| `platform.permissions` — implementation brief | **ACCEPTED** (Codex plan-consensus round 6) | `docs/adr/WEEK9-platform-permissions-implementation-brief.md` |
| `platform.permissions` — Slice A (migration + authorization primitive) | **DONE**, Codex diff-reviewed round 2 | commit `1507084` |
| `platform.permissions` — Slice B (package + bootstrap CLI) | **DONE**, Codex diff-reviewed round 4 | commit `49be26a` |
| `platform.permissions` — Slice C (import-linter contracts) | **DONE**, Codex diff-reviewed round 5 | commit `1c35d85` |
| `platform.permissions` — Slice D plan | **ACCEPTED** (20 rounds, 76 findings resolved) | `docs/adr/WEEK9-slice-d-implementation-plan.md` |
| `platform.permissions` — Slice D code (`AuthenticationMiddleware` cutover) | **VERIFIED IN WORKING COPY**, focused/full/static gates green | working copy (commit/push require scope owner approval) |
| `platform.permissions` — Slice E (reference data & company endpoint cleanup) | next up | — |
| `platform.permissions` — Slice F (final integration & E2E verification) | pending | — |

## Running things

- **Database & Migrations**: `docker compose up -d`, `uv run alembic upgrade head`.
- **Tests & Quality Checks**:
  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run python -m mypy .
  uv run python -m importlinter.cli
  uv run python -m pytest
  ```
- **Bootstrap Admin User**:
  ```bash
  BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_password make bootstrap
  ```
- **Seed Demo Data**:
  ```bash
  BOOTSTRAP_COMPANY_CODE=MAIN make seed
  ```
- **Run Live O2C Demo**:
  ```bash
  BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=your_password make demo
  ```

## Historical next steps (2026-08-25)

1. Obtain scope owner's approval to create Git Commit for Slice D.
2. Proceed to WEEK9 Slice E (reference-data and company endpoint removal/restructuring).
3. Proceed to WEEK9 Slice F (final platform verification & E2E sanity).

**Push gate**: `1507084`/`49be26a`/`1c35d85` are committed locally, never pushed. Do not push without asking the scope owner.
