# HANDOFF — mini-erp (WEEK9 Slices A–F Complete — 2026-10-30)

Updated 2026-10-30. This is the current handoff status; the dated sections below are retained as historical project context and may describe earlier repository states.

## Product and repository status — 2026-10-30

### Product

mini-ERP is an API-only, self-hostable modular ERP kernel for Taiwanese SMEs. The implemented Phase 1 order-to-cash flow covers master data, sales orders, shipping and inventory, receivables, payments, and double-entry ledger posting. It uses Python 3.12, FastAPI, async SQLAlchemy/asyncpg, PostgreSQL 16, and Alembic. Tenant isolation is fail-closed; posted financial state and stock movements use append-only/auditable patterns; domain events connect business modules while import-linter enforces module boundaries. ADR-009 platform authentication and permissions (Slices A–F, including the Slice D.5 Hybrid Security Gate and Slice E CLI-only reference-data/company provisioning) are fully implemented and verified.

Phase 1 is described as complete in both READMEs. The product is still an API kernel: there is no frontend, dynamic plugin loader or sandbox, workflow/approval engine, admin custom-field UI, or Taiwan tax/e-invoice integration. The in-process credit-limit plugin is trusted code and must not be treated as sandboxed. Long-term Platform → Suites → Taiwan localization plans are in `docs/open-erp-master-plan.md`.

### Repository checkpoint and GitHub publication

- **Slice D & Slice D.5**: Merged to `main` via PR #2 at commit `c077cc0de9145e2d9a78576fde9a1e48231d263b`.
- **Slice E**: Merged to `main` via PR #3 at commit `9f01b70` (`feat(masterdata): complete WEEK9 Slice E CLI-only reference data and company provisioning (#3)`), removing the 5 unauthenticated/HTTP reference-data and company POST routes (`POST /api/v1/companies`, `POST /api/v1/currencies`, `POST /api/v1/uom`, `POST /api/v1/uom-conversions`, `POST /api/v1/exchange-rates`), adding `app/cli/manage_reference_data.py` (`REFERENCE_DATA_ADMIN_ACTOR`) and `app/cli/manage_companies.py` (`COMPANY_ADMIN_ACTOR`), and updating `docs/security/d5-shards.json`, `docs/security/scan-policy.md`, and `docs/security/threat-model.md` for the 177-file inventory (`53` application `APIRoute`s = `4 Public` / `2 Authenticated` / `47 RequiresPermission`).
- **Slice F**: Completed end-to-end live-server verification of the README "Try it" O2C walkthrough alongside Slice E CLI provisioning and HTTP 405 enforcement (`tests/e2e/test_demo_o2c_live.py`), hardened `tests/conftest.py` Docker probe timeout/retry and Linux `pgserver` Unix socket URI resolution, and verified 100% completion of ADR-009 Action Items 1–11 (`docs/adr/ADR-009-platform-permissions.md`).
- All quality gates pass: `pytest` (`585 passed, 5 skipped`), `mypy --strict` (`0` issues), `import-linter` (`9/9` contracts kept), `ruff check` & `ruff format --check` (all passed).

### Slice D.5 Hybrid Security Gate status — PASSED & MERGED (2026-10-30)

Per the owner's 2026-10-22 decision, Slice D.5 is closed under the **Hybrid Right-Tool-for-the-Job Gate** (`docs/security/scan-policy.md`), combining the completed LLM semantic scans across all application/platform code (`u01`–`u11`) with deterministic code, workflow, secret, and OSV supply-chain verification for `u12`–`u14`:

| Unit | Verification Method & Final Disposition |
| --- | --- |
| `u01_identity` – `u11_operations_e2e` (151 files) | **PASSED (Codex Security `gpt-5.6-sol`)**. Completed unit scan evidence across all 151 application, platform RBAC, migration, CLI, and E2E files is accepted and preserved per owner decision. |
| `u12_gate` (2 files) | **PASSED (Deterministic Code & Contract Gate)**. Reviewed commit `95c53820a9889c659c34d39c3d1460ec0b66b6d6` in `scripts/d5_security_gate.py` and cross-platform test remediation `9011cef9f929775e4f57c25278f38f95da90030d` in `tests/security/test_d5_security_gate.py`: verified `_trusted_git_executable`, `_local_filter_overrides` (`core.fsmonitor=false`, disabled hooks/filters/templates/ext transport, rejected config includes), `_git_environment` allowlisting, Windows Job Object / Linux subreaper process-tree containment, and `codex-security-plugin@0.1.22` sealed coverage verification. Verified across Windows and Linux CI by 281 contract tests (`tests/security/test_d5_security_gate.py`), `ruff`, and `mypy --strict` (`0` issues in `app/` and `scripts/`). |
| `u13_governance` (20 files) | **PASSED (Remediated & Deterministically Verified)**. All four findings from scan `454b2e7` are remediated: (1) replaced predictable static quick-start credentials with interactive hidden `read -rsp` prompts across `README.md`, `README.zh-TW.md`, `HANDOFF.md`, and `WEEK9-slice-d-implementation-plan.md`; (2) replaced `curl -d` inline password arguments with stdin piping (`--data-binary @-`) and removed `-e VAR="$$VAR"` argv expansion; (3) pinned all GitHub Actions in `.github/workflows/ci.yml` and `.github/workflows/coverage.yml` to immutable 40-char commit SHAs (`actions/checkout@11bd7190...`, `astral-sh/setup-uv@caf0cab7...` with fixed `uv 0.12.18`, `actions/upload-artifact@b4b15b8c...`, `actions/download-artifact@fa0a91b8...`) and added top-level `permissions: contents: read`; (4) split `coverage.yml` into a read-only `coverage-generate` job (runs `alembic` + `pytest`) and an isolated `coverage-badge` publish job (`contents: write`, validates artifact, checks out `ref: main` without sparse-checkout so `git worktree` can stage `coverage.svg`, and pushes to `badges` under a unified workflow-level `concurrency` group without running project/dependency code). |
| `u14_dependencies` (2 files) | **PASSED (Deterministic OSV Supply-Chain Audit & Reachability Disposition)**. Audited all 66 locked packages in `uv.lock` against the PyPI/OSV database. Zero runtime vulnerabilities are reachable in `mini-erp`: `starlette==0.46.2` advisories (`GHSA-7f5h-v6xp-fcq8` `FileResponse`, `GHSA-82w8-qh3p-5jfq` `request.form()`, `GHSA-wqp7-x3pw-xc5r` `StaticFiles`, `GHSA-2c2j-9gv5-cj73` multipart, `GHSA-x746-7m8f-x49c` `HTTPEndpoint`, `GHSA-86qp-5c8j-p5mr` / `GHSA-jp82-jpqv-5vv3` `request.url`) are unreachable because `mini-erp` is a pure JSON `APIRoute` kernel (`_classify_routes_or_crash` rejects any `Mount` or custom `Route`, `python-multipart` is not installed, and `AuthenticationMiddleware` inspects raw ASGI `scope["path"]`); `mako==1.4.1` (`GHSA-5639-2j2p-m4mx`) is restricted to trusted operator CLI migrations (TB-008); `certifi==2026.7.22`, `urllib3==2.7.0`, `pytest==8.4.2`, and `setuptools==80.10.2` are dev-only test dependencies not shipped in production runtime (`[project.dependencies]`). Owner: Project Maintainer; target routine dependency bump scheduled before Phase 2 release tag. |

### Next work (Phase 2 — Week 10+)

1. **WEEK9 (`platform.permissions` / ADR-009) is 100% complete** across Slices A–F.
2. **Next Phase 2 Platform Modules** (per `docs/adr/PHASE2-platform-architecture-overview.md` and `docs/open-erp-master-plan.md`): Proceed to subsequent Phase 2 platform capabilities (e.g., `platform.audit`, workflow/approvals, or custom fields metadata).

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
| `platform.permissions` — Slice D code (`AuthenticationMiddleware` cutover) | **DONE & MERGED**, 14 rounds Codex Luna reviewed | commit `1db8ed9` (merged in `c077cc0`) |
| `platform.permissions` — Slice D.5 (Hybrid Security Gate & CI hardening) | **DONE & MERGED**, 4-tier hybrid gate & Linux CI green (578 passed) | commits `95c5382` / `42ce85e` / `9011cef` (merged in `c077cc0`) |
| `platform.permissions` — Slice E (reference data & company endpoint cleanup) | **DONE & MERGED** (PR #3) | commit `9f01b70` |
| `platform.permissions` — Slice F (final integration & E2E verification) | **DONE** (live O2C & Action Items 1–11 verified) | `docs/adr/ADR-009-platform-permissions.md` |

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
  export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
  read -rsp "Bootstrap admin password: " BOOTSTRAP_ADMIN_PASSWORD && echo
  export BOOTSTRAP_ADMIN_PASSWORD
  make bootstrap
  ```
- **Seed Demo Data**:
  ```bash
  BOOTSTRAP_COMPANY_CODE=MAIN make seed
  ```
- **Run Live O2C Demo** (reusing exported `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`):
  ```bash
  make demo
  ```

## Historical next steps (2026-08-25)

1. Obtain scope owner's approval to create Git Commit for Slice D.
2. Proceed to WEEK9 Slice E (reference-data and company endpoint removal/restructuring).
3. Proceed to WEEK9 Slice F (final platform verification & E2E sanity).

**Push gate**: `1507084`/`49be26a`/`1c35d85` are committed locally, never pushed. Do not push without asking the scope owner.
