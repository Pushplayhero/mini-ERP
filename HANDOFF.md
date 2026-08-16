# HANDOFF — mini-erp (Week 8 complete; Phase 2 discovery underway)

Rewritten 2026-08-17. Week 8 (Phase 1 polish) is fully done — see "Week 8
status" below, condensed from the prior handoff. **This session's real
news**: Phase 2 ("Platform") architecture discovery started —
`docs/adr/PHASE2-platform-architecture-overview.md` is ACCEPTED, and the
first per-module ADR, `docs/adr/ADR-009-platform-permissions.md`, is
ACCEPTED after an 8-round Codex consensus. **Neither has any implementation
yet** — this project's plan-then-implement discipline held throughout; the
next session's job is either implementation or the next ADR
(`platform.plugins`), not both at once.

## Current state

**HEAD: `51a6b43`** — "Fix coverage-badge job: publish to a separate badges
branch, not main". Working tree clean (besides untracked
`.claude/review-state.md`).
**Tag `v0.1.0`** on `9e8fa52`, unchanged, still correct (see Week 8
Decision 5 below for why).
**Remote `origin` = `https://github.com/Pushplayhero/mini-ERP.git`, now
PUBLIC** (flipped from private this session, after a secrets/history scan
found nothing — see "GitHub repo/CI infra" below), branch `main` tracks
`origin/main`, in sync. **A second branch, `badges`, now exists** —
CI-managed only, holds the live coverage badge SVG, never touched by
humans.

## Phase 2 status

### `docs/adr/PHASE2-platform-architecture-overview.md` — ACCEPTED

A map-altitude overview of the five Phase 2 modules (`permissions`,
`plugins`, `customfields`, `workflow`, `integration`), authored by an Opus
subagent, Codex-architecture-consensus-reviewed 3 rounds (round 1 REJECTED
with 7 findings, round 2 REJECTED with 2 leftover consistency issues, round
3 APPROVED). Identifies the authN+authZ+company-entitlement decision as the
keystone constraining the other four modules.

**§7 "Decision round"** records the scope-owner's resolution of all seven
§6 open questions plus the §3 keystone questions, each of the harder ones
(Q1, Q3, Q4–Q7) run through a dedicated Codex advisory discussion first
(this project's now-standing pattern: **every decision goes through Codex
consensus, the result gets reported to the user, and nothing proceeds
without the user's explicit go-ahead** — a meta-instruction the user gave
mid-session after an early turn skipped straight from a discussion to
recording a decision without pausing; see "Engineering workflow" below).
Key resolutions:

- **Q1 (authZ depth)**: pure RBAC (action-level) + a mandatory
  company-entitlement gate; row/field-level deferred. Amount-threshold
  approval = "workflow routing picks the step, RBAC decides who may
  approve it," not a generic per-actor ceiling engine.
- **Q2 (identity)**: zero-external-dependency login mandatory, local
  user+credential store the default; external OIDC optional, never
  required.
- **Q3 (permissions placement)**: dependency inversion — business modules
  depend on a narrow `app.core` authorization port; `platform.permissions`
  implements it; the composition root (`app.main`) wires them, the same
  pattern already used for plugins.
- **Q7 (integration transport)**: no broker in Phase 2; a co-deployed
  standalone worker (changed from the scope owner's initial in-process
  lean, on Codex's recommendation — transaction/ContextVar/lifecycle
  coupling risk).

### `docs/adr/ADR-009-platform-permissions.md` — ACCEPTED (Codex round 8)

The first per-module ADR, per §7.2 Q4's sequencing (permissions first,
contract-before-feature). **This is the headline story of the session**:
8 rounds of real Codex architecture-consensus review, each finding real
(if progressively narrower) issues — not a rubber-stamp process. Full
round-by-round history is in the ADR's own "Consensus Revisions" sections;
summary:

- **Round 1** (5 findings): bootstrap chicken-and-egg cycle unresolved,
  the `app.core.authorization` port/wiring contract underspecified + no
  system-actor concept for CLI paths, a self-contradiction in the
  fail-closed design, incomplete data-model invariants, a too-narrow
  import-linter contract.
- **Round 2** (3 findings): `demo_o2c.py` (a real HTTP client) was wrongly
  grouped with direct-service-call CLI scripts; an overclaimed test-
  coverage sentence; a self-contradictory fail-closed summary.
- **Round 3** (3 findings) — **a real architecture gap, not a wording
  bug**: `rebuild_ar_balances.py`/`rebuild_stock_summary.py` do raw ORM
  queries and `replay_outbox.py` calls `events.redispatch()` directly —
  none of the three ever reaches the service-layer authorization boundary
  the whole ADR was built around. Paused and resolved with the user via a
  dedicated Codex advisory discussion (not decided unilaterally),
  producing the "trusted-operator maintenance exemption" (Decision 10).
- **Round 4** (5 findings) — **a second instance of the same gap,
  self-caught during the round-3 fix**: `POST /companies`/`GET
  /companies`/`GET /companies/{id}` have the identical global-vs-
  per-company-RBAC problem `Company` is deliberately never
  `TenantScopedMixin`. Resolved with the user the same way, producing
  Decision 12 (create removed to CLI-only; reads reclassified as
  entitlement-filtered, not removed — reading your own companies is a real
  ongoing feature, unlike an unrestricted global write).
- **Round 5** (4 findings): Rule 10's split (10a reconstruction/replay,
  10b first-install) wasn't applied consistently everywhere, and —
  substantively — the *new* CLI tools built to replace the removed HTTP
  writes (Decisions 11/12) had no stated authorization treatment of their
  own; fixed by reusing the already-established `seed_demo.py`/
  `DEMO_SEED_ACTOR` pattern (purpose-specific actor + real
  `require_permission()` at the service boundary) rather than inventing a
  third exemption category.
- **Rounds 6–8**: progressively narrower wording/consistency leftovers
  (a stale phrase in one Options Considered subsection, an asymmetric
  sentence between two parallel Option A/B descriptions). Round 8:
  **APPROVED, no new findings.**

**12 decisions total** — data model (6 tables: `users`/`roles`/
`permissions`/`role_permissions`/`user_company_roles`/`sessions`);
Argon2id password hashing; opaque bearer tokens with a *separate* SHA-256
session-token hash (fast lookup vs. slow password defense are different
problems — don't reuse Argon2id for both); `app.core.authorization` as a
context-carrier port, never a shared `SYSTEM_ACTOR` — purpose-specific
actors only (`DEMO_SEED_ACTOR`, `REFERENCE_DATA_ADMIN_ACTOR`,
`COMPANY_ADMIN_ACTOR`); `AuthenticationMiddleware` replacing
`tenancy_middleware`'s header trust boundary while reusing
`company_context()` unchanged; bootstrap CLI (Rule 10b); defense-in-depth
fail-closed (real router-level enforcement + service-boundary authoritative
check + startup route-classification crash); per-request entitlement
revalidation with explicit 401/403 mapping; 3 new import-linter contracts;
the Rule 10a/10b maintenance exemption; **Decision 11** (4 already-shipped
`v0.1.0` reference-data write endpoints removed, replaced by a CLI tool
with its own bound actor); **Decision 12** (`POST /companies` removed the
same way, `GET /companies(/id)` reclassified entitlement-filtered).

**Not yet implemented.** No migration, no `app/core/authorization.py`, no
`app/platform/permissions/` package exists yet. Next per-module ADR per
§7.2 Q4 is `platform.plugins`.

## GitHub repo/CI infra (this session)

- **Repo flipped private → public.** Re-scanned this session's own new
  commits for secrets first (clean); description was already set from
  Decision 1; added 11 topics (`python`, `fastapi`, `postgresql`, `erp`,
  `sqlalchemy`, `open-source`, `order-to-cash`, `double-entry-bookkeeping`,
  `plugin-architecture`, `modular-monolith`, `taiwan`).
- **Branch protection on `main`**: classic branch protection (not
  Repository Rulesets), `required_status_checks` = the 4 CI jobs,
  `enforce_admins: false` (repo owner can still push directly — every push
  this session showed `Bypassed rule violations` in the remote output),
  `allow_force_pushes: false`, `allow_deletions: false`, no required PR.
- **A real, live regression happened and was fixed** — worth reading in
  full if touching CI again: enabling branch protection broke the
  coverage-badge job's direct push to `main`, because `github-actions[bot]`
  (the default `GITHUB_TOKEN` identity) is not an administrator and gets no
  `enforce_admins` bypass. One push happened to succeed right after
  protection was first enabled (a grace-period artifact, not reliable
  behavior); the next two both failed identically
  (`GH006: Protected branch update failed... 4 of 4 required status checks
  are expected`), silently leaving `badges/coverage.svg` stale for two real
  pushes before anyone noticed.
  - **Explored and confirmed non-viable**: a dedicated GitHub App as a
    Repository Ruleset bypass actor (Codex's initial recommendation).
    Empirically confirmed — not assumed — that **GitHub Apps cannot be
    ruleset bypass actors on a personal (non-Organization) GitHub
    account**: `POST /repos/.../rulesets` with an `Integration`-type
    bypass actor fails with `"must be part of the ruleset source or owner
    organization"`. A dedicated App (`mini-erp-coverage-bot`, app-id
    `4615270`) was actually created and installed before this was
    discovered — now uninstalled/unused, its two repo secrets deleted.
  - **Also learned**: a Repository Ruleset with an empty `bypass_actors`
    list sets `current_user_can_bypass: "never"` for *everyone*, including
    the repo owner — caught this via the API response *before* actually
    pushing anything under that configuration, avoiding locking out normal
    owner pushes too.
  - **Actual fix, simpler than the explored alternatives**: the
    coverage-badge job no longer pushes to `main` at all. It publishes
    `coverage.svg` to a separate, unprotected `badges` branch instead (via
    a linked `git worktree` — `--orphan` on the branch's first-ever
    creation, a tracked worktree on every run after, with an explicit
    `git fetch` first since `actions/checkout@v4` only fetches `main` by
    default — Codex diff review round 1 caught that exact gap, round 2
    APPROVED). Since the bot never touches the protected branch, **no
    bypass mechanism of any kind is needed** — branch protection was
    restored to its simple, already-proven classic-protection shape.
  - README's badge now points at
    `https://raw.githubusercontent.com/Pushplayhero/mini-ERP/badges/coverage.svg`
    (repo is public, no auth needed); the stale `badges/coverage.svg` copy
    was removed from `main`'s tracking.

## What's next

The fork every prior handoff has flagged is now resolved — the user chose
Phase 2, and Phase 2 discovery is underway. The actual next decision:

- **Continue Phase 2**: write the `platform.plugins` ADR next (§7.2 Q4's
  order), following the same discipline ADR-009 just proved out
  (discovery → draft → Codex consensus rounds → user scope decisions where
  real gaps surface → APPROVED before any code).
- **Start implementing ADR-009** instead, before moving to the next
  module's ADR — a legitimate alternative reading of "permissions first":
  design the whole keystone module's ADR-level shape first (now done), or
  implement it before designing the next one. Not yet decided — ask.
- Minor, low-priority leftovers from Week 8 never picked back up: a
  coverage-history trend beyond the point-in-time badge, the user's own
  GIF recording for the README walkthrough.

Don't assume — ask.

## Project

Open-source ERP kernel ("mini-erp"), Python/FastAPI, explicitly scoped to
eventually rival 鼎新 (Digiwin) on the O2C (order-to-cash) flow — portfolio /
career-leverage project, also a personal engineering challenge.

Repo root: this folder (`mini-erp/`). Remote is now public (see above).

## Engineering workflow (established preference — keep enforcing this)

For medium/high-risk work:
1. Architecture discovery + a real Codex plan/architecture-consensus
   review before implementation. Nothing gets implemented until Consensus
   Status is APPROVED.
2. **Standing meta-rule, stated explicitly by the user mid-session**:
   *any* decision — architecture, scope, a bugfix approach with multiple
   real options — goes through a real Codex consensus/advisory pass
   first; the result gets reported to the user; **nothing proceeds until
   the user gives explicit go-ahead**, even when the fix seems obviously
   correct. This was stated after an earlier turn this session jumped
   from a Codex discussion straight into writing decisions into a
   document without pausing for the user first — don't repeat that.
   Applies to code changes too, not just architecture (the coverage-badge
   branch-protection fix went through this same loop: Codex advisory →
   report → user picks the option → implement → Codex diff review →
   report → user says commit/push).
3. Implementation by Claude Code directly, or delegated to a subagent
   (this session: an Opus subagent drafted the Phase 2 overview; a Sonnet
   main thread did everything else, including all of ADR-009's 8 rounds).
4. A real Codex diff review after implementation, against the spec — every
   slice/change, no exceptions for "just a workflow YAML" or "just a
   README fix" (both have caught real issues in past sessions).
5. `/CODEX REVIEW PRE-PUSH` before pushing multi-slice/production-facing
   work — still not yet exercised as a distinct step.

**Standing constraint — do not relitigate**: "Codex review" must always
mean the real `codex` CLI (confirmed logged in via `codex.cmd login
status` → "Logged in using ChatGPT"), invoked from Claude Code — never
Claude generating both the plan and its own review of it. This was
violated silently through Weeks 1–4 (caught and fixed retroactively). If a
future session's "Codex review" output looks suspiciously fast,
well-formatted, or absent of any actual `codex.cmd` invocation in the
transcript, stop and verify before trusting it.

**When Codex rejects a finding, verify it against actual code before
fixing — don't fix reflexively, and don't dismiss reflexively either.**
Two sharp illustrations now on record: Week 8 Decision 4's round 2
"account_type should be lowercase" finding was a real-sounding but false
positive (settled by live HTTP evidence + source, see the doctrine below);
ADR-009's round-3/4 findings were the opposite case — real, substantive
architecture gaps that "sounded like nitpicking" at first glance
(a maintenance CLI's exact call path; whether `/companies` has the same
shape as reference data) but held up completely under verification. Always
verify with real evidence — grep the actual code, run the actual command,
check the actual API response — before accepting OR dismissing a finding
either model raises.

**Push gate**: never `git commit`/push/tag-push, and never touch GitHub
account/security settings (branch protection, secrets, app installs, repo
visibility), without the user's explicit go-ahead in the current
conversation — approval from an earlier turn or session does not carry
forward. This session added a concrete case: destructive GitHub API calls
(e.g. `DELETE` on branch protection) get blocked by this environment's own
permission classifier regardless of prior context — when that happens,
tell the user exactly what's blocked and why, and let them either do it
themselves (e.g. via the GitHub web UI) or explicitly authorize a retry;
don't try to work around the block.

**Language**: the user's standing preference (memorized) is all
user-facing replies in Traditional Chinese (繁體中文) — English is fine in
code/commits/docs, but chat responses should be Chinese.

## Established cross-cutting doctrines (apply these, don't relitigate them)

- **Multi-tenancy**: `contextvars.ContextVar` + SQLAlchemy `do_orm_execute`
  hook + `with_loader_criteria`, fail-closed (`TenancyContextError` if no
  company bound) — the hook only fires for ORM `SELECT` statements
  (`app/core/db.py` returns immediately for any non-SELECT); write-safety
  is a separate convention (every INSERT stamps `company_id` from
  `require_current_company_id()`, and every mutate-by-id re-fetches the
  row through a hook-filtered SELECT first) — see README.md's
  "Multi-tenancy" section for the accurate version. `TenantScopedMixin` on
  any table queried directly. Any Core-table (`sqlalchemy.table()`)
  reference used to dodge the cross-module-import contract must explicitly
  filter `company_id` itself — this is exactly the pattern
  `ledger.service.get_trial_balance` uses to read `accounts` (owned by
  `masterdata`) without importing it; the raw Core query bypasses more
  than just the import contract, though — it also skips Pydantic enum
  serialization (`account_type` renders as the enum member's uppercase
  `.name`, not the lowercase `.value` — a Codex false-positive finding
  settled by live evidence, Week 8 Decision 4). A standalone CLI
  (`app/cli/*`) running outside any HTTP request must bind
  `company_context(...)` explicitly before any tenant-scoped write —
  **and per ADR-009 (not yet implemented), will additionally need to bind
  either a purpose-specific authorization actor or fall under the Rule
  10a/10b maintenance exemption once permissions lands.**
- **A "global, never-tenant-scoped resource" is a recurring category with
  its own authorization gap, not a one-off** (ADR-009's headline
  architectural lesson): `Company` (the tenant root) and reference data
  (currencies/UoM/exchange rates) are both deliberately outside
  `TenantScopedMixin`'s per-company filtering — which also means neither
  fits neatly into a *per-company* RBAC model once one exists. ADR-009's
  Decisions 11/12 close this gap for those two instances (remove/CLI-only
  writes, entitlement-filtered reads where a read is a legitimate ongoing
  feature) but explicitly flag that other global tables may have the same
  gap — an audit is real implementation-brief work, not yet done.
- **Money**: `NUMERIC(20,6)`; FX rates `NUMERIC(20,10)` + `rate_date`;
  round-half-even enforced at the Pydantic-schema layer for every money
  field — each module owns a private rounding helper (never imported
  cross-module), but **the helper's literal name is not uniform**:
  `masterdata`/`sales`/`receivables` name theirs `_round_half_even_6dp`;
  `ledger` names its `_round_half_even_to_6dp` (a pre-existing
  inconsistency, not yet fixed). Zero is always legal for
  `credit_limit`/price/cost fields. **A discriminating rounding test needs
  a value where `ROUND_HALF_EVEN` and `ROUND_HALF_UP` actually disagree**
  — use `X.0000025` (even digit before the tie), not `X.0000015`.
- **Ledger**: double-entry, dual-currency lines, DEFERRED CONSTRAINT
  TRIGGER balance check at commit, immutability via `BEFORE UPDATE OR
  DELETE` triggers, gapless numbering, reversal-only corrections. Trial
  balance is always an on-the-fly `SUM` aggregate — `accounts` has no
  balance column. `TrialBalanceLine` includes `account_id`/`account_type`
  alongside the four expected fields — six total.
- **Event bus** (`app/core/events.py`): synchronous, in-process,
  same-transaction dispatch; `publish()` validates + outbox-writes +
  dispatches; `redispatch()` is the *only* replay entry point; every event
  schema must carry `company_id`; handler exceptions propagate unchanged
  (fail-closed). `sales.goods_shipped` has two subscribers whose order is
  normative: `inventory` before `ledger.posting`. A standalone CLI
  publishing events must `import app.main` first.
- **Hooks** (`app/core/hooks.py`): synchronous veto/augment points, not
  durably recorded, not replayed. The one Phase 1 hook is
  `sales.order.validate_confirm`, consumed by `credit_limit.py`.
- **Plugins** (`app/plugins/`): exempt from the module-independence
  import-linter contract; core/modules may never import plugins. In-
  process, no sandbox — a plugin is admin-installed trusted code. Phase 1
  ships one hard-wired demonstration plugin; a real dynamic loader is
  Phase 2 scope (next ADR after permissions, per §7.2 Q4).
- **Transaction ownership ("flush-only core + committing wrapper")**: most
  `service.py` write functions only `flush()`; the caller owns
  commit/rollback. The wrapper's try/except must wrap the core call
  itself, not just the final `commit()`. Exceptions:
  `masterdata`/`ledger`'s simple `create_*` functions and
  `sales.service.create_order` self-commit via `_commit_or_conflict()` —
  grep for that function name in a module before assuming either pattern.
- **mypy narrowing through a proxy boolean doesn't work**: narrow directly
  at the point of use (`y = x if x is not None else default`), not through
  an intermediate boolean variable. CI's `mypy app` job scopes to `app/`
  only, tighter than an unscoped local `uv run mypy .`.
- **Idempotent event handlers**: partial unique index + `begin_nested()`
  SAVEPOINT catching `IntegrityError` as an "already processed" no-op.
- **Append-only facts + per-domain-decided caching**: every fact table is
  append-only; whether a derived aggregate is *also* cached in a
  maintained column is decided per domain (on whether a concurrent
  capacity-check needs a row to lock).
- **Race prevention ("R1 doctrine")**: every state-machine transition
  takes `SELECT ... FOR UPDATE` before re-checking status. A derived
  report combining two related aggregates must read them via ONE SQL
  statement, not two.
- **Idempotent CLIs/scripts**: get-or-create by natural key isn't enough —
  also validate other attributes match what was intended, fail loudly on
  mismatch; a reconciliation target must be remaining-work-aware.
- **`_commit_or_conflict()`**: every service-layer commit catches
  `IntegrityError` → `ConflictError` (409), never a raw 500.
- **Testing**: real PostgreSQL only. `tests/conftest.py` uses
  `testcontainers` when Docker is available, falls back to embedded
  `pgserver` — the DSN must use the real TCP host/port, not the pgdata
  path (learned skill `pgserver-windows-tcp-dsn`, reused successfully for
  ad-hoc manual verification databases outside the pytest suite too).
  Hypothesis property tests need a fresh company per example. A pure
  schema-layer unit test is legitimate when isolating validator behavior
  specifically. **When verifying a doc claim, prefer running the actual
  documented command over re-reading the code** (Week 8 Decision 4: static
  reading missed two real README staleness gaps; running the walkthrough
  caught them immediately).
- **Alembic transaction scope**: the whole `upgrade head` run is one
  transaction — a migration referencing an enum value added earlier in the
  same chain must cast the column to text, never the literal to the enum
  type. A backfill relying on "exactly one matching row" must
  preflight-check both the zero-match and duplicate-match cases.
- **CI/infra that "looks right" per spec still needs a REAL run before
  it's trusted** — proven twice now: Week 8's `coverage-badge` job passed
  Codex review but still failed its actual first run (`badges/` didn't
  exist yet); this session's branch-protection change passed Codex review
  too but broke the same job differently (bot has no admin bypass) two
  pushes later. Careful design/review only gets you to "should work" —
  only an actual run gets you to "does work," and one successful run isn't
  proof either (the bot's one early success was a fluke, not reliable
  behavior — confirmed by two subsequent identical failures).
- **A platform/vendor limitation you haven't personally hit yet is not
  something to assume away** — this session hit two, both only discovered
  by actually trying the API call: classic branch protection requires
  GitHub Pro on a *private* repo (Week 8), and GitHub Apps cannot be
  Repository Ruleset bypass actors on a *personal* (non-Organization)
  account (this session). Both were genuine surprises to Claude and to
  Codex's own recommendation. When a plan depends on a platform capability
  neither party has verified firsthand on this exact account/repo, verify
  empirically before committing to the design, not after.
- **Plan-consensus review can take many rounds, and that's fine, even many
  rounds in a row on the same document** — Week 8's brief took 5 rounds;
  ADR-009 took 8. Findings got progressively narrower (round 3/4 were real
  architecture gaps; rounds 6–8 were single leftover phrases), which is
  the expected shape of a review converging, not a sign something's wrong
  with the process.
- **CI that has never actually run is not validated, regardless of how
  clean local checks look.**

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata | committed (Week 1), rounding fix Week 8 | — |
| ledger | committed (Week 2–3), Codex-reviewed for real | ADR-005, ADR-003, ADR-004 |
| sales | committed (Week 4), rounding fix + mypy fix Week 8 | ADR-006 |
| inventory + shipping | committed (Week 5), Codex-reviewed for real | ADR-007 |
| receivables | committed (Week 6), Codex-reviewed for real | ADR-008 |
| Week 7 hardening | all 6 slices done, Codex-reviewed for real | `docs/adr/WEEK7-phase1-hardening-brief.md`, ADR-001, ADR-002 |
| Week 8 polish | all 6 Decisions done | `docs/adr/WEEK8-phase1-polish-brief.md` |
| Phase 2 overview | ACCEPTED, no code | `docs/adr/PHASE2-platform-architecture-overview.md` |
| `platform.permissions` | **ADR ACCEPTED, no code yet** | `docs/adr/ADR-009-platform-permissions.md` |
| `platform.plugins` | not started | next ADR per §7.2 Q4 |

**Phase 1 ("Kernel") is complete**, `v0.1.0` is tagged on a real, CI-green,
now-public GitHub remote with a live coverage badge. Phase 2 architecture
discovery has produced two ACCEPTED design documents and zero lines of
Phase 2 application code. See "What's next" above.

## Running things

```
uv sync
docker compose up -d          # postgres for local dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest                 # uses testcontainers or embedded pgserver
uv run pytest --cov=app --cov-report=term   # with coverage
uv run ruff check .
uv run ruff format --check .
uv run mypy .                 # local: app + tests (42 pre-existing errors in tests/, not CI's gate)
uv run mypy app               # matches CI's exact "mypy --strict" job — this must be 0 errors
uv run lint-imports            # import-linter contracts
```

Or, with the `Makefile`: `make check` runs the full ruff/mypy/lint-imports/
pytest sequence; `make up`/`make seed`/`make demo` are the docker-compose
quick-start. **Still never actually exercised end to end in this dev
environment** (no Docker here) — only the constituent scripts have been run
manually via an embedded `pgserver` workaround.

**GitHub remote + CI, now public** (`https://github.com/Pushplayhero/mini-ERP`,
branch `main`):
```
gh run list --repo Pushplayhero/mini-ERP --limit 5   # recent CI runs
gh run view <run-id> --repo Pushplayhero/mini-ERP     # job breakdown
gh api repos/Pushplayhero/mini-ERP/branches/main/protection   # branch protection state
```
`gh` CLI is installed and authenticated as the `Pushplayhero` account. CI's
`mypy` job scope is `app/` only.

**Coverage badge**: lives on a dedicated `badges` branch (`coverage.svg` at
its root), **not** on `main` — see "GitHub repo/CI infra" above for why.
README links `https://raw.githubusercontent.com/Pushplayhero/mini-ERP/badges/coverage.svg`.
Regenerated by `.github/workflows/coverage.yml`'s `coverage-badge` job on
every push to `main`. Never hand-edit — it's overwritten on the next run
regardless, and doing so would defeat the whole point (a static badge that
lies).

**Windows long-path workaround** (this session's environment): the real
repo path is very long (Cowork-generated). A short junction `C:\wt\merp` →
the real repo path, plus a venv outside the long path
(`UV_PROJECT_ENVIRONMENT` pointed at `C:\wt\venv`), with `RUFF_CACHE_DIR`/
`MYPY_CACHE_DIR` also redirected to `C:\wt\.ruff_cache`/`C:\wt\.mypy_cache`.
Git commands against the real path need `-c core.longpaths=true`. Reuse the
same junction/venv if it still exists at session start.

**`.claude/review-state.md`** — a session-tooling marker file (the
`codex-review` skill's plan-consensus pass marker), NOT a project
deliverable. Left untracked/uncommitted deliberately. Don't `git add
.claude/`.

228 tests pass locally as of `7794fb9` (unchanged since — this session's
work was all docs/ADRs/CI config, no application code); 87% coverage
(measured for real, multiple times, in CI); ruff/lint-imports clean;
`mypy app` (CI's exact scope) clean; `mypy .` (broader local scope) has 42
pre-existing errors, all in `tests/`, confirmed not a regression and out of
CI's actual gate scope.
