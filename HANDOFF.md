# HANDOFF — mini-erp (WEEK9 brief accepted; Slices A/B/C implemented, D next)

Rewritten 2026-08-17 (same day as the prior handoff — this is a same-day
continuation, not a new session's discovery). Week 8 (Phase 1 polish) and
Phase 2 architecture discovery (PHASE2 overview + ADR-009, both ACCEPTED)
are unchanged from the prior handoff — condensed below. **This session's
real news**: the scope owner chose "implement ADR-009" over "write the
next ADR" (`platform.plugins`); a `WEEK9-platform-permissions-
implementation-brief.md` sequencing ADR-009's 11 Action Items into six
slices (A–F) was written, taken through **6 rounds of real Codex plan
consensus** to APPROVED, and **Slices A, B, and C are now implemented,
each independently Codex-diff-reviewed to APPROVED** (5, 4, and 5 rounds
respectively — every round a real finding, none rubber-stamped). **Slice D
(the breaking-change cutover) has not started** — this session ends here
deliberately, per the scope owner's explicit choice to open a fresh
context window before touching it, given the brief's own assessment of D
as "the highest-risk single slice in this project's history."

## Current state

**HEAD: `1c35d85`** — "WEEK9 Slice C: import-linter contracts for
app.platform". Local `main` is **3 commits ahead of `origin/main`**
(`1507084` Slice A, `49be26a` Slice B, `1c35d85` Slice C) — **none pushed**.
Working tree clean besides the usual untracked `.claude/`.
**Tag `v0.1.0`** unchanged. Remote unchanged (public,
`https://github.com/Pushplayhero/mini-ERP.git`).

**`make check`-equivalent is green at HEAD**: `ruff check`, `ruff format
--check`, `mypy app` (0 errors), `lint-imports` (9 contracts kept, 0
broken — up from 5 before this session), full `pytest` (266 passed, up
from 228 — 38 new tests across the three slices).

## This session's work

### The WEEK9 implementation brief — plan consensus, 6 rounds

`docs/adr/WEEK9-platform-permissions-implementation-brief.md`. ADR-009
itself (already ACCEPTED, prior session) names *what* to build; this brief
sequences it into six independently-landable slices (A: migration + `app.
core.authorization` primitive; B: `platform.permissions` package +
bootstrap CLI; C: import-linter contracts; D: `AuthenticationMiddleware`
cutover + non-HTTP script migration; E: reference-data/company endpoint
removal; F: verification). User confirmed the six-slice boundary via
`AskUserQuestion` before drafting; the brief then went through 6 rounds of
real `codex.cmd exec -m gpt-5.6-sol` plan review (findings: 6, 7, 2, 2, 1,
0) before APPROVED. Round 2 surfaced something more than a plan bug: **a
real placement error in the already-ACCEPTED ADR-009 itself** (Action
Item 4 put `Public`/`RequiresPermission` in `app.platform.permissions`,
which is unimplementable against ADR-009's own Decision 9 import
contract) — fixed with a dated "Post-acceptance correction" note added
directly into `ADR-009-platform-permissions.md`, not silently overridden
by the downstream brief. See the brief's own "Consensus Revisions"
section (round-by-round, findings annotated in-place through Decisions
2–7) for the full history — worth reading in full if touching Slice D,
since several of Slice D's own bullets (the two-tier public-route
mechanism, the service-boundary enforcement requirement, `session.manage`'s
seeding/classification timing) were shaped directly by that back-and-forth.

### Slice A — migration 0009 + `app/core/authorization.py`

Commit `1507084`. Six RBAC tables (`users`/`roles`/`permissions`/
`role_permissions`/`user_company_roles`/`sessions`), seeded with a
**minimal placeholder catalog only** (`admin` role + `session.manage`
permission — the full by-endpoint catalog is Slice D's job, migration
0010). `app/core/authorization.py`: `ActorContext`, `ContextVar` bind/
reset, `actor_context()` context manager (mirrors `app.core.tenancy`
exactly), `require_permission()` (fail-closed both ways — no context
bound, or insufficient permission — `is_system_actor` is an audit marker
only, never a bypass), `Public`/`RequiresPermission` FastAPI dependency
factories (placed here per the ADR-009 correction above, not in
`platform.permissions`).

Codex diff review: 2 rounds. Round 1 caught a **real regression this
slice's own migration caused**: `tests/test_migrations.py`'s four
`command.downgrade(cfg, "-1")` calls (three testing migration 0008, one
testing migration 0005) all silently changed meaning the moment migration
0009 became the new alembic head — a relative "-1" step used to land at
"before 0008" only while 0008 was still head. Fixed with explicit target
revision ids instead of a relative step; round 1's own first attempt at
the fix used the *wrong* explicit revision for the 0005 test (caught and
fixed within the same round, before round 2 re-confirmed). Round 2:
APPROVED.

### Slice B — `platform.permissions` package + `bootstrap_admin.py`

Commit `49be26a`. `models.py`/`schemas.py`/`service.py` (`login()` with
ADR-009 Decision 8 multi-company selection, `resolve_actor()` for
per-request revalidation, `switch_company()`, `logout()` — real row
deletion, not soft-revoke)/`context.py` (`current_session_id`, deliberately
separate from `ActorContext` — which session a request came from isn't an
authorization fact)/`router.py` (`/auth/login`, `/auth/switch-company`,
`/auth/logout` — the latter two do a temporary manual token-resolve +
`actor_context()`/`session_context()` bind, since `AuthenticationMiddleware`
doesn't exist until Slice D). `app/cli/bootstrap_admin.py`: the ADR-009
Decision 6 first-install sequence, Rule 10b exempt, refuses a second run.
Argon2id (new `argon2-cffi` dependency) for passwords; SHA-256 for session
tokens (deliberately not Argon2id — fast, deterministic lookup key vs.
slow brute-force defense are different problems).

Codex diff review: **4 rounds, the most substantive of the three slices**:
- **Round 1** (2 findings, both real security bugs, not nitpicks):
  (a) `bootstrap_admin.py` called `create_user()` then `grant_role()`
  separately, each self-committing — a failure between them left a
  permanently-unrecoverable half-entitled user (Rule 10b refuses every
  rerun once any user exists, so there was no retry path). (b) `login()`
  skipped the (deliberately slow) Argon2id `verify_password()` call
  entirely for unknown/disabled-user emails — a genuine **timing side
  channel** defeating `InvalidCredentialsError`'s own "one response for
  all three causes" design (the body didn't distinguish them; the clock
  did). Fixed: merged user+grant into one commit; always call
  `verify_password()` exactly once, against a dummy hash when no usable
  user exists.
- **Round 2** (1 finding): round 1's atomicity fix was **incomplete** —
  merging user+grant into one commit didn't help, because
  `_ensure_reference_data()`/`_ensure_company()` *still* called
  `masterdata_service`'s self-committing `create_currency`/`create_uom`/
  `create_company` first, each independently ending the whole
  advisory-lock-protected transaction early. Real lesson: a
  transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`) is
  released by *any* commit inside its transaction, not just the "main"
  one — a single early intermediate commit anywhere in the sequence
  defeats the whole lock.
- **Round 3** (1 finding, on the *tests* not the implementation — Codex
  explicitly confirmed "the implementation call graph itself is
  correct" this round): the new rollback/concurrency regression tests
  never actually exercised `_ensure_reference_data()`'s insert path,
  because the autouse `_seed_reference_data` fixture already pre-seeds
  TWD/EA — a regression reintroducing a commit *inside* that specific
  function would have gone undetected by tests that looked like they'd
  catch it. Fixed by deleting TWD/EA (and their FK-dependent
  `exchange_rates`/`uom_conversions` rows — needed because a full-suite
  run may have unrelated earlier tests' rows referencing them, since
  `currencies`/`uom` are deliberately never truncated between tests) at
  the start of both tests.
- **Round 4**: APPROVED, no new findings.

Final shape: `bootstrap_admin()` runs the **entire** sequence — advisory
lock, `count_users()==0` check, reference data, company, user+grant — as
**one transaction with exactly one `session.commit()`** at the very end.
Two new regression tests added per Codex's explicit request:
`test_bootstrap_admin_rolls_back_everything_on_failure` (monkeypatches a
mid-sequence failure, asserts *nothing* — not even reference data —
survives) and `test_bootstrap_admin_concurrent_runs_only_one_succeeds`
(`asyncio.gather`s two real concurrent invocations, asserts exactly one
wins).

### Slice C — import-linter contracts

Commit `1c35d85`. Config-only (`pyproject.toml`), no application code —
and still took **5 rounds** of Codex diff review, all real, on what looked
like a trivial change:
- **Rounds 1–2**: missing `app.main`/`app.cli` in the new "platform.
  permissions may depend on core and masterdata only" contract's
  forbidden-list.
- **Round 3**: forbidding bare `app.modules`/`app` (to close a "bare
  `import app.modules` then reach a forbidden submodule via an attribute
  Python already populated elsewhere" side channel) would **also** forbid
  their required allowed descendants (`app.core`, `app.modules.
  masterdata`) — `forbidden` contracts have no way to carve out one
  exception; **empirically confirmed**, not assumed, via a throwaway probe
  contract.
- **Round 4**: the bare `app.platform` entry added in round 3 (thought
  safe, since `platform.permissions` has no allowed dependency under that
  root at all) was itself **silently a no-op the whole time** —
  `ForbiddenContract`'s own docstring states source/forbidden pairs that
  overlap are skipped entirely, and `app.platform.permissions` is a
  subpackage of `app.platform`. Confirmed by reading
  `importlinter/contracts/forbidden.py` directly. Codex introduced the
  `protected` contract type (not previously used in this codebase) as the
  correct tool: `as_packages = false` makes it operate on the *literal*
  import target only, so protecting bare `app`/`app.modules`/`app.platform`
  doesn't cascade to their real descendants.
- **Round 5**: the `protected`-contract fix verified with three more
  throwaway probes (real `masterdata`/`app.main` imports stay KEPT; a
  genuine scratch-file `import app.modules` is correctly caught BROKEN) —
  **APPROVED, no new findings.**

Net result: **9 import-linter contracts** (up from 5), including a new
contract *type* (`protected`) this codebase hadn't used before this
session.

## What's next — Slice D, not started

**Explicit scope-owner instruction**: do not start Slice D in this
session. The user asked to update this handoff and will open a fresh
context window before continuing.

Slice D per the brief: `AuthenticationMiddleware` replaces
`tenancy_middleware`'s header-reading code entirely; the two-tier
public-route mechanism (Tier 1 static allowlist for framework-generated
`/docs`/`/redoc`/`/openapi.json`/`/docs/oauth2-redirect`; Tier 2
routing-metadata lookup via `app.routes` + `.matches(scope)` for every
ordinary route, including `GET /uom`/`GET /currencies` which must stay
anonymous); the startup route-classification crash; migration 0010 (the
full by-endpoint permission-code catalog, additive-only against 0009,
**must** grant every new code to `admin` or the bootstrap admin gets
locked out post-cutover — this exact bug was caught in Slice B's own
review, applies again here); service-boundary `require_permission()` calls
added inside every affected `service.py` function across `masterdata`/
`sales`/`ledger`/`inventory`/`receivables`; the four non-HTTP script
migrations (`seed_demo.py` → `DEMO_SEED_ACTOR` + production guard;
`rebuild_ar_balances.py`/`rebuild_stock_summary.py`/`replay_outbox.py` →
no change, Rule 10a exempt; `demo_o2c.py` → real login + bearer token);
`/auth/switch-company`/`/auth/logout`'s Slice-B-temporary manual token
resolution → **mandatory removal**, replaced by middleware-bound
`ActorContext`/`current_session_id`; README's "Try it" walkthrough
re-verified by actually running the updated commands.

Before writing any Slice D code: **re-read the WEEK9 brief's Decision 5
in full** (it's long, and every clause in it was shaped by a real Codex
finding across rounds 1–6 of the plan-consensus review) — do not
re-derive the mechanism from scratch.

## Established cross-cutting doctrines — additions from this session

(See the prior handoff for the full standing list — tenancy, money,
ledger, event bus, hooks, plugins, transaction ownership, mypy narrowing,
idempotent handlers, R1 doctrine, `_commit_or_conflict`, testing, Alembic
transaction scope, "CI that looks right still needs a real run," platform-
limitation verification, plan-consensus round counts, "CI that has never
run is not validated." All still apply, unchanged. New from this session:)

- **A transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`)
  is released by ANY commit inside its transaction, not just a deliberate
  "final" one.** If a locked critical section calls into helper functions
  that self-commit (e.g. another module's existing `create_*` service
  functions), the lock is gone the moment the first of those commits runs
  — long before the code "intends" to release it. The only fix is
  non-committing (`add`/`flush`-only) helpers throughout the locked
  section, with exactly one `commit()` at the very end, owned by the
  function that acquired the lock.
- **A deliberately slow hash function (Argon2id) must run unconditionally
  on every login attempt, even when the email is unknown or the account is
  disabled.** Short-circuiting past it creates a timing side channel that
  defeats an otherwise-correct "identical response body for every failure
  cause" design — the response can refuse to distinguish causes while the
  clock still does. Fix: verify against a fixed dummy hash when no usable
  user exists, so the expensive call always happens exactly once.
- **import-linter's `forbidden` contract has two easy-to-miss semantics,
  both empirically confirmed this session, not assumed:** (1) forbidding
  an ancestor package also forbids every descendant unconditionally — no
  contract shape carves out "forbid X except one specific child," so a
  partial allowance (e.g. "forbid `app.modules` except
  `app.modules.masterdata`") is only expressible as an enumerated
  descendant list, never as an ancestor-level forbid. (2) `source_modules`
  and `forbidden_modules` pairs that overlap (one is a subpackage of the
  other) are silently skipped entirely — a contract with
  `source_modules=["app.platform.permissions"]` and `app.platform` in its
  `forbidden_modules` does nothing, because the source is itself a
  descendant of the forbidden target. Neither failure mode raises an
  error; both just quietly never trigger. **When a `forbidden` contract
  needs to block a *bare* parent-package import while still allowing one
  specific descendant, use the `protected` contract type instead**
  (`protected_modules=[...]`, `allowed_importers=[...]`,
  `as_packages=false`) — it operates on the literal import target only,
  with no ancestor/descendant cascading either direction. Verify any
  non-trivial import-linter contract shape with a throwaway probe
  contract (add it, run `lint-imports`, observe, delete it) before
  trusting the configuration — this session needed three separate probes
  across two different misunderstandings to land the correct shape.
- **A "trivial config-only" change is not lower-scrutiny than application
  code** — Slice C (one file, `pyproject.toml`, zero lines of Python) took
  5 real Codex diff-review rounds, more than Slice A's 2. Diff size and
  real risk are not the same axis.
- **Every push-gate action needs asking in the current turn, even mid-flow
  after several already-approved commits.** This session committed Slice
  B without asking first (a real slip, corrected before Slice C) —
  "approval doesn't carry forward" applies within a session between
  slices, not just across sessions.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata / ledger / sales / inventory+shipping / receivables | committed, Codex-reviewed for real | ADR-001–ADR-008 |
| Week 7/8 hardening + polish | done | WEEK7/WEEK8 briefs |
| Phase 2 overview | ACCEPTED, no code | `docs/adr/PHASE2-platform-architecture-overview.md` |
| `platform.permissions` — ADR | **ACCEPTED** (Codex round 8), one post-acceptance correction (Action Item 4, this session) | `docs/adr/ADR-009-platform-permissions.md` |
| `platform.permissions` — implementation brief | **ACCEPTED** (Codex plan-consensus round 6) | `docs/adr/WEEK9-platform-permissions-implementation-brief.md` |
| `platform.permissions` — Slice A (migration + authorization primitive) | **DONE**, Codex diff-reviewed round 2 | commit `1507084` |
| `platform.permissions` — Slice B (package + bootstrap CLI) | **DONE**, Codex diff-reviewed round 4 | commit `49be26a` |
| `platform.permissions` — Slice C (import-linter contracts) | **DONE**, Codex diff-reviewed round 5 | commit `1c35d85` |
| `platform.permissions` — Slice D (AuthenticationMiddleware cutover) | **NOT STARTED** — next session's job | — |
| `platform.permissions` — Slices E/F | not started | — |
| `platform.plugins` | not started | next ADR after `permissions` fully lands |

## Running things

Unchanged from the prior handoff — see there for the full command list
(`uv sync`, `docker compose up`, `alembic upgrade head`, `uv run
uvicorn`, `uv run pytest`, ruff/mypy/lint-imports, the Windows long-path
junction workaround at `C:\wt\merp`/`C:\wt\venv`). One addition this
session: `uv add argon2-cffi` was run (already reflected in
`pyproject.toml`/`uv.lock`, committed in Slice B — nothing further needed).

**Bootstrap CLI, once Slice D lands and this is a real deployment**:
`uv run python -m app.cli.bootstrap_admin --email <e> --password <p>`
(or `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD` env vars) — not yet
meaningful to run today, since nothing enforces authentication until
Slice D.

**Push gate**: same as always — `1507084`/`49be26a`/`1c35d85` are
committed locally, never pushed, per this session's explicit per-commit
confirmations. Do not push without asking again in the next session,
regardless of this handoff's tone.
