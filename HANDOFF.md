# HANDOFF — mini-erp (Week 8 complete)

Rewritten 2026-08-16 (Week 8 session). **All six Week 8 Decisions are now
done.** Previous handoff (same date, mid-session rollover) covered
Decisions 0/1/3/5; this session finished Decisions 2 and 4.

## Week 8 status: all 6 Decisions done

**Current HEAD: `7794fb9`** — "Week 8 Decision 4: verify walkthrough
script, fix README staleness". **Working tree is clean** (besides the
untracked `.claude/review-state.md` session-tooling marker — see below).
**Tag `v0.1.0` exists, annotated, pushed, on `9e8fa52`** (predates
Decisions 2/4 — see Decision 5's note below for why that's correct and
not stale).
**Remote `origin` = `https://github.com/Pushplayhero/mini-ERP.git`
(private), branch `main` tracks `origin/main`, both in sync.**

Week 8's plan doc is `docs/adr/WEEK8-phase1-polish-brief.md` (ACCEPTED
after a 5-round Codex plan-consensus review, committed at `d0421f9`).
All six Decisions:

- **Decision 0 (rounding fix) — DONE, `3072009`.**
- **Decision 1 (GitHub remote + CI) — DONE.** First-ever CI run failed on
  a real pre-existing mypy bug, fixed at `9e8fa52`; next run was this
  project's first fully green CI.
- **Decision 2 (coverage badge) — DONE this session, `627b467` /
  `9f9d66b` / `e3b29b6`.** `.github/workflows/coverage.yml`: two jobs
  (`coverage-check` on PR, informational; `coverage-badge` on push to
  main, bot-commits `badges/coverage.svg`). Landed via a real
  branch+PR ([PR #1](https://github.com/Pushplayhero/mini-ERP/pull/1),
  merged) specifically so `coverage-check` could be exercised in a real
  PR's checks per the brief's Decision 6 sequencing — everything else in
  this project's history has gone straight to `main`. Fully verified
  end-to-end, not just "the YAML looks right":
  - `coverage-check` ran for real on the PR: 228 passed, 87%.
  - `coverage-badge` ran for real on merge to `main`: bot commit
    `e3b29b6` landed, `badges/coverage.svg` shows "87%", matching that
    same run's own `pytest --cov` output (the brief's required same-run
    consistency check).
  - Anti-recursion confirmed for real: the bot's own commit did not
    retrigger the workflow (`paths-ignore: ['badges/**']` works as
    designed).
  - Two real bugs found and fixed during implementation, neither
    anticipated by the brief, both Codex-diff-reviewed:
    1. `coverage-badge` (unmaintained since 2023) hard-imports
       `pkg_resources`, which `setuptools>=81` no longer ships by
       default — fixed by pinning `setuptools<81` as an explicit dev
       dependency (not a tool substitution).
    2. The `coverage-badge` job had no concurrency guard — two
       near-simultaneous pushes to `main` could race on the
       commit+push, leaving a stale badge or a rejected push. Fixed
       with job-level `concurrency` (serialize per ref, cancel
       superseded runs) + `checkout: ref: main` (live branch tip, not
       the frozen triggering SHA). Codex round 1 REJECTED on exactly
       this finding; round 2 APPROVED after the fix.
    3. (Found only after merging to `main`, not caught by review since
       it only manifests against a truly fresh checkout with no
       `badges/` directory yet): `coverage-badge` does not create its
       output directory, and `badges/` had never existed in this repo.
       First real `coverage-badge` run on `main` failed with
       `FileNotFoundError`. Fixed with a one-line `mkdir -p badges`
       step, pushed directly as a hotfix (`9f9d66b`), reproduced and
       verified locally first. Second run on `main` succeeded.
- **Decision 3 (public demo host) — DONE (as a deferral).** Unchanged
  from the prior handoff — deferred until Phase 2 auth.
- **Decision 4 (demo GIF script) — DONE this session, `7794fb9`.**
  Verification-only per the brief ("verify/finalize the walkthrough
  script... largely already real via README's Try it section"). Actually
  ran both documented flows against a fresh live app instance this
  session (embedded Postgres + real HTTP, not just re-reading the docs):
  - `make demo` equivalent (`app.cli.demo_o2c` against a running
    `uvicorn`): output matched README's documented sample **byte-for-byte**.
    No change needed.
  - "Try it" curl walkthrough: dollar amounts all matched, but the
    documented trial-balance JSON sample was stale — missing
    `account_id`/`account_type`, two fields `TrialBalanceLine` actually
    returns. Fixed the README sample to match.
  - Also found and fixed, while in there: README's "Money" paragraph
    still described Decision 0's rounding fix as *future* work, even
    though Decision 0 had already shipped in `v0.1.0`. Rewrote to
    describe the current, accurate state.
  - Codex-diff-reviewed, 3 rounds: round 1 REJECTED a real finding
    (README claimed a uniform helper name, `_round_half_even_6dp`,
    across all four modules — `ledger.schemas` actually names its
    helper `_round_half_even_to_6dp`, one module's naming inconsistency
    that predates this session). Round 2 REJECTED a **false positive**
    (claimed `account_type` should render lowercase per the
    `AccountType` Python enum's `.value`) — verified against actual
    code and the live capture already in hand (the trial-balance
    endpoint reads the native Postgres enum column via a raw
    SQLAlchemy Core query, bypassing Pydantic enum serialization
    entirely; SQLAlchemy's `Enum()` with no `values_callable` stores by
    `.name`, i.e. uppercase — confirmed by the migration file too).
    Round 3 confirmed the false positive with that evidence, APPROVED.
  - The embedded GIF itself is still not required for Week 8 — still
    handed off to the user's own machine (no capture tooling in this
    sandbox, unchanged from the prior handoff's finding).
- **Decision 5 (`v0.1.0` tag) — DONE, unchanged from the prior handoff.**
  Points at `9e8fa52`, not this session's later commits — correct,
  because the brief's intent was tagging the completed *application-code*
  Phase 1 kernel (Decision 0's rounding fix + the mypy fix), not every
  presentation/infra polish item. Decisions 2/4 are deliberately
  post-tag infrastructure/doc polish, matching the tag mutability policy
  (pushed tags are permanent; a future release would be `v0.1.1`, not a
  moved `v0.1.0`).

**All six Decisions are now done. Week 8 is complete.**

## What's next

Same fork as every prior handoff has flagged — ask the user which of:

- **Further Week 8/9 polish**: GitHub branch protection requiring CI to
  pass before merge (raised as a natural next step once a real remote +
  CI existed; still not done, still not asked of the user), `v0.1.0`'s
  repo description/topics, maybe flipping the repo to public once the
  user is ready, a coverage-history trend if they want more than the
  self-contained badge, the user's own GIF recording + embedding it in
  README.
- **Phase 2 ("Platform")** per `docs/open-erp-master-plan.md` §1 —
  plugin loader, custom-fields admin UI, workflow/approval engine, real
  auth/RBAC. Bigger planning lift than any Week 7/8 item — start with
  architecture discovery and a real Codex consensus review on the
  Phase 2 brief before writing any code.

Don't assume either — ask.

## Project

Open-source ERP kernel ("mini-erp"), Python/FastAPI, explicitly scoped to
eventually rival 鼎新 (Digiwin) on the O2C (order-to-cash) flow — portfolio /
career-leverage project, also a personal engineering challenge.

Repo root: this folder (`mini-erp/`).
**Git remote**: `origin` = `https://github.com/Pushplayhero/mini-ERP.git`
(private). Branch `main`, tracked against `origin/main`.

## Engineering workflow (established preference — keep enforcing this)

For medium/high-risk work:
1. `/CODEX REVIEW ARCHITECTURE` (or an equivalent real architecture-
   consensus/plan-consensus dispatch) on the design doc, before
   implementation. Nothing gets implemented until Consensus Status is
   APPROVED.
2. Implementation by Claude Code directly, or delegated to a `sonnet`
   agent, depending on session context.
3. A real Codex diff review after implementation, against the spec —
   **every slice/change, per explicit user instruction, proven out
   repeatedly now**, including this session's Decision 2 (2 review
   rounds, a real concurrency-bug finding) and Decision 4 (3 review
   rounds — one real finding, one false positive Codex itself conceded
   after being shown live evidence + source). Don't skip this step even
   for "just a workflow YAML" or "just a README fix" — both caught real
   issues this session.
4. `/CODEX REVIEW PRE-PUSH` before pushing multi-slice/production-facing
   work — still not yet exercised as a distinct step; this session's
   pushes went through the same per-change diff-review-then-commit-
   then-push flow as everything else, now also including a real
   branch+PR cycle for Decision 2 specifically (see above).

**Standing constraint — do not relitigate**: "Codex review" must always
mean the real `codex` CLI (confirmed logged in on this machine via
`codex.cmd login status` → "Logged in using ChatGPT"), invoked from Claude
Code — never Claude generating both the plan and its own review of it.
This was violated silently through Weeks 1–4 of this project (caught and
fixed retroactively). If a future session's "Codex review" output looks
suspiciously fast, well-formatted, or absent of any actual `codex.cmd`
invocation in the transcript, stop and verify before trusting it.

**When Codex rejects a finding, verify it against actual code before
fixing — don't fix reflexively, and don't dismiss reflexively either**
(this session's sharpest illustration yet): Decision 4's round 2 finding
("account_type should be lowercase") looked completely reasonable on its
face — it cited a real enum in real code — but was wrong for this
specific endpoint, which bypasses that enum's serialization path
entirely via a raw SQLAlchemy Core query reading a native Postgres enum
column that SQLAlchemy's `Enum()` type stores by member `.name`
(uppercase), not `.value`. The live HTTP capture already in hand from
manually re-running the walkthrough settled it immediately. Always
prefer real captured evidence over either side's static-reading
inference when the two conflict.

**Push gate**: never `git commit`/push/tag-push without the user's
explicit go-ahead in the current conversation — approval from an earlier
turn or an earlier session does not carry forward. Held exactly this way
for every Week 7 slice, every Week 8 Decision including this session's
Decision 2 PR-open/merge/hotfix-push and Decision 4 push — each of those
sub-steps got its own explicit go-ahead, not one blanket approval reused.

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
  "Multi-tenancy" section for the accurate version. `TenantScopedMixin`
  on any table queried directly. Any Core-table (`sqlalchemy.table()`)
  reference used to dodge the cross-module-import contract must
  explicitly filter `company_id` itself — this is exactly the pattern
  `ledger.service.get_trial_balance` uses to read `accounts` (owned by
  `masterdata`) without importing it; the raw Core query bypasses more
  than just the import contract, though — see the Codex false-positive
  entry above for a concrete case where that same bypass also skips
  Pydantic enum serialization. A standalone CLI (`app/cli/*`) running
  outside any HTTP request must bind `company_context(...)` explicitly
  before any tenant-scoped write.
- **Money**: `NUMERIC(20,6)`; FX rates `NUMERIC(20,10)` + `rate_date`;
  round-half-even is now enforced at the Pydantic-schema layer for
  **every** money field across every module — each module owns a
  private, near-identically-shaped rounding helper (never imported
  cross-module, per the import-linter contract), but **the helper's
  literal name is not uniform**: `masterdata`/`sales`/`receivables` all
  name theirs `_round_half_even_6dp`; `ledger` names its
  `_round_half_even_to_6dp` (an inconsistency that predates Week 8,
  caught by Codex during Decision 4's README review — worth fixing for
  real consistency someday, but out of scope for a doc-only pass).
  Zero is always a legal value for `credit_limit`/price/cost fields,
  never rejected by the rounding validator (unlike
  `receivables.schemas`'s `_round_and_reject_zero`, which is specific to
  payment/allocation amounts where zero genuinely isn't legal). **A
  discriminating rounding test needs a value where `ROUND_HALF_EVEN` and
  `ROUND_HALF_UP` actually disagree** — use `X.0000025` (even digit
  before the tie), not `X.0000015` (odd digit — same answer under both
  modes). Money-typed Pydantic fields need `ge=0`/`gt=0` explicitly
  where negative/zero doesn't make sense.
- **Ledger**: double-entry, dual-currency lines, DEFERRED CONSTRAINT
  TRIGGER balance check at commit, immutability via `BEFORE UPDATE OR
  DELETE` triggers, gapless numbering (`ledger_sequences` + `FOR UPDATE`),
  reversal-only corrections. Reversals never copy the original entry's
  `source_type`/`source_id`. Trial balance is always an on-the-fly `SUM`
  aggregate (ADR-005 Decision 4) — `accounts` has NO balance column,
  unlike `stock_summary`/`invoices.settled_amount` (see ADR-002 for why
  that's a deliberate per-domain difference, not an inconsistency). The
  trial-balance report endpoint's response (`TrialBalanceLine`) includes
  `account_id`/`account_type` alongside `account_code`/`account_name`/
  `total_debit`/`total_credit` — six fields total; `account_type` renders
  as the enum member's uppercase `.name` (e.g. `"ASSET"`), not the
  lowercase `.value`, because that field is populated from a raw
  SQLAlchemy Core query reading the native Postgres enum column directly
  (see the Codex false-positive entry above).
- **Event bus** (`app/core/events.py`): synchronous, in-process, same-
  transaction dispatch; `publish()` validates + outbox-writes + dispatches;
  `redispatch()` is the *only* replay entry point; every event schema must
  carry `company_id: uuid.UUID`; handler exceptions propagate unchanged
  (fail-closed). Not every registered event has a subscriber —
  `sales.order_confirmed` is registered (so `publish()`/replay validate
  its schema and it gets an outbox row) but deliberately has none
  (ADR-006 Decision 4). `sales.goods_shipped` is the only event with TWO
  subscribers, and their order is **normative, not incidental** (ADR-007
  Decision 1): `inventory.handle_goods_shipped` (deduct stock) runs
  before `ledger.posting`'s handler (post the journal entry) — "move the
  goods, then account for them"; both still commit/rollback atomically
  together regardless of order, but the ordering determines which
  handler's exception is the attributed failure reason on a bad `ship`.
  A standalone CLI that publishes events must `import app.main` first
  (side-effecting — installs schema registrations and posting-handler
  subscriptions) or the first `publish()` raises `UnknownEventTypeError`.
- **Hooks** (`app/core/hooks.py`): distinct from events — synchronous
  veto/augment points, not durably recorded, not replayed. The one Phase 1
  hook is `sales.order.validate_confirm`, consumed by
  `app/plugins/credit_limit.py` (`credit_limit == 0` = "do not check").
- **Plugins** (`app/plugins/`): exempt from the module-independence
  import-linter contract; core/modules may never import plugins. In-
  process, no sandbox, same trust model as Odoo (see `SECURITY.md`) — a
  plugin is admin-installed trusted code, not untrusted input. Phase 1
  ships one hard-wired demonstration plugin (`credit_limit.py`); a real
  dynamic plugin *loader* is Phase 2 scope.
- **Transaction ownership ("flush-only core + committing wrapper", ADR-003
  R1)**: most write functions in `service.py` modules only `flush()`; the
  HTTP router (or whatever caller opened the transaction — including a
  standalone CLI) owns commit/rollback. **The wrapper's try/except must
  wrap the core call itself, not just the final `commit()`** — the core's
  own `flush()` can raise `IntegrityError` too. Exception: `masterdata`
  and `ledger`'s simple `create_*` functions (company, customer, product,
  account, period) commit themselves via `_commit_or_conflict()` — this is
  an older, Week-1/2 convention that predates the flush-only-core doctrine
  and was never retrofitted. **Also exception**: `sales.service.create_order`
  self-commits too (same `_commit_or_conflict()` pattern) even though
  `confirm_order`/`ship_order` in the same module are flush-only — check
  which pattern a given function follows before assuming either one; when
  in doubt, grep for `_commit_or_conflict` in that module.
- **mypy narrowing through a proxy boolean doesn't work**: `is_flag = x
  is not None; y = x if is_flag else default` does NOT let mypy conclude
  `x` is non-`None` inside the `is_flag` branch, even though `is_flag`
  and `x is not None` are logically identical at runtime — mypy only
  narrows on the actual checked expression at the point of use. Write
  `y = x if x is not None else default` (narrow directly), not through an
  intermediate variable, if a downstream operation needs the non-`None`
  type. This bit `sales.service._build_lines` and was invisible locally
  until this project's first-ever real CI run — `.github/workflows/ci.yml`'s
  `mypy app` job scopes to `app/` only, not `tests/`, so it's a tighter
  gate than an unscoped local `uv run mypy .`.
- **Idempotent event handlers**: partial unique index (`WHERE source_type
  IS NOT NULL`) + `session.begin_nested()` SAVEPOINT catching the
  resulting `IntegrityError` as an "already processed, skip" no-op.
- **Append-only facts + per-domain-decided caching** (ADR-002): every
  fact table (`journal_lines`, `stock_moves`, `payment_allocations`) is
  append-only, unconditionally. Whether the derived current-state
  aggregate is ALSO cached in a maintained column is decided per domain,
  on whether there's a concurrent capacity-check that needs a row to
  lock — ledger doesn't cache (no such check), inventory/receivables do.
- **Race prevention ("R1 doctrine")**: every state-machine transition
  takes `SELECT ... FOR UPDATE` on the row *before* re-checking its
  status. A derived report combining two related aggregates must read
  them via ONE SQL statement, not two separate `session.execute()`
  calls — a two-statement read is racy under READ COMMITTED even with no
  explicit lock bug (see the learned skill
  `postgres-read-committed-race-single-statement-fix`).
- **Idempotent CLIs/scripts**: get-or-create by natural key is not
  enough on its own — also validate that an existing row's OTHER
  attributes match what you intended (fail loudly on a mismatch), and if
  a script reconciles a maintained quantity to a target, that target must
  be **remaining-work-aware**, never a fixed number applied
  unconditionally.
- **`_commit_or_conflict()`**: every service-layer commit catches
  `IntegrityError` → translates to `ConflictError` (409), never a raw 500.
- **Testing**: real PostgreSQL only, never SQLite/mocks.
  `tests/conftest.py` uses `testcontainers` when Docker is available,
  falls back to embedded `pgserver` — the pgserver DSN must use the real
  TCP `hostname`/`port` from `get_postmaster_info()`, not the pgdata
  directory path (see the learned skill `pgserver-windows-tcp-dsn`; this
  session reused that exact pattern three more times, for fresh manual
  demo/curl-verification databases outside the pytest suite itself —
  it works equally well there). Hypothesis property tests for
  invariants — give each Hypothesis example a **fresh company**, never
  accumulate state across examples. A "fix a race condition by combining
  two statements into one" change can't be tested by reproducing the old
  race — write a structural test instead. A pure schema-layer unit test
  is a legitimate addition to this project's otherwise real-Postgres-only
  testing style when it isolates validator behavior specifically.
- **Alembic transaction scope**: the whole `upgrade head` run is one
  transaction — a migration referencing an enum value added by an
  *earlier* migration in the same chain must cast the *column* to text
  and compare against a plain string literal, never cast the literal to
  the enum type. A migration backfill relying on "exactly one matching
  row" must preflight-check BOTH the zero-match and duplicate-match cases
  before writing anything.
- **Writing/reviewing a general-pattern ADR from multiple concrete
  instances**: separate what's genuinely uniform across instances from
  what's decided per-instance — see the learned skill
  `adr-backfill-uniform-vs-per-instance`.
- **Documentation claims need the same rigor as code claims**: this
  session's Decision 4 was the sharpest instance yet — README's Money
  paragraph had been quietly wrong (describing a fix as unfixed) since
  Decision 0 shipped, and the trial-balance sample had been missing two
  real fields, both undetected until someone actually re-ran the
  documented commands against a live instance rather than just re-reading
  the prose. **When verifying a doc claim, prefer running the actual
  documented command over re-reading the code** — static reading missed
  both gaps; running the walkthrough caught them immediately.
- **A CI workflow that "looks right" per spec still needs a REAL run
  before it's trusted** (this session's second headline lesson, echoing
  Week 8's Decision 1 lesson about application CI): Decision 2's
  `coverage-badge` job was fully spec-conformant and Codex-approved, yet
  still failed on its actual first run on `main` for a reason no amount
  of static review would have caught (a missing directory that simply
  never existed yet in this specific repo's history). Two rounds of
  careful design/review only get you to "should work" — only an actual
  run gets you to "does work."
- **Plan-consensus review can take many rounds, and that's fine** — see
  the Week 8 brief's own 5-round history for the canonical example.
- **CI that has never actually run is not validated, regardless of how
  clean local checks look**.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata | committed (Week 1), rounding fix Week 8 | — |
| ledger | committed (Week 2–3), Codex-reviewed for real | ADR-005, ADR-003, ADR-004 |
| sales | committed (Week 4), rounding fix + mypy fix Week 8 | ADR-006 |
| inventory + shipping | committed (Week 5), Codex-reviewed for real | ADR-007 |
| receivables | committed (Week 6), Codex-reviewed for real | ADR-008 |
| Week 7 hardening | all 6 slices done, Codex-reviewed for real | `docs/adr/WEEK7-phase1-hardening-brief.md`, ADR-001, ADR-002 |
| Week 8 polish | **all 6 Decisions done** | `docs/adr/WEEK8-phase1-polish-brief.md` |

**Phase 1 ("Kernel") is complete**, Week 8 polish is complete, `v0.1.0`
is tagged and pushed on a real, CI-green GitHub remote, and the repo now
also has a live coverage badge. See "What's next" above.

## Running things

```
uv sync
docker compose up -d          # postgres for local dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest                 # uses testcontainers or embedded pgserver
uv run pytest --cov=app --cov-report=term   # with coverage (Week 8 Decision 2)
uv run ruff check .
uv run ruff format --check .
uv run mypy .                 # local: app + tests (42 pre-existing errors in tests/, not CI's gate)
uv run mypy app               # matches CI's exact "mypy --strict" job — this must be 0 errors
uv run lint-imports            # import-linter contracts
```

Or, with the `Makefile`: `make check` runs the full ruff/mypy/lint-imports/
pytest sequence in one command; `make up` / `make seed` / `make demo` are
the docker-compose quick-start. **Still never actually exercised end to
end in this dev environment** (no Docker here) — only the constituent
scripts have been run manually via an embedded `pgserver` workaround
(reused three more times this session for demo/curl verification).

**GitHub remote + CI, real** (`https://github.com/Pushplayhero/mini-ERP`,
private, branch `main`):
```
gh run list --repo Pushplayhero/mini-ERP --limit 5   # recent CI runs
gh run view <run-id> --repo Pushplayhero/mini-ERP     # job breakdown
gh pr list --repo Pushplayhero/mini-ERP               # this session opened/merged PR #1 for Decision 2
```
`gh` CLI is installed and authenticated in this environment as the
`Pushplayhero` account. CI's `mypy` job scope is `app/` only (`uv run
mypy app`), not `tests/`.

**Coverage badge** (`badges/coverage.svg`, Week 8 Decision 2): live,
regenerated by `.github/workflows/coverage.yml`'s `coverage-badge` job on
every push to `main` (except pushes that only touch `badges/**`, to avoid
retriggering itself), committed by `github-actions[bot]`. Never hand-edit
this file — it will be overwritten by the next push-to-main run anyway,
and doing so would defeat the whole point (a static badge that lies).

**Windows long-path workaround** (this session's environment): the real
repo path is very long (Cowork-generated). A short junction
`C:\wt\merp` → the real repo path, plus a venv outside the long path
(`UV_PROJECT_ENVIRONMENT` pointed at `C:\wt\venv`), with `RUFF_CACHE_DIR`/
`MYPY_CACHE_DIR` also redirected to `C:\wt\.ruff_cache`/`C:\wt\.mypy_cache`.
Git commands against the real path need `-c core.longpaths=true`. Reuse
the same junction/venv if it still exists at session start rather than
recreating it.

**`.claude/review-state.md`** — a session-tooling marker file (the
`codex-review` skill's plan-consensus pass marker), NOT a project
deliverable. Left untracked/uncommitted deliberately. Don't `git add
.claude/`.

228 tests pass locally as of `7794fb9` (unchanged test count from
Decision 2/4 — both were CI-config and doc-only changes, no test-visible
app-code changes); 87% coverage (measured for real, twice, in CI);
ruff/lint-imports clean; `mypy app` (CI's exact scope) clean; `mypy .`
(broader local scope) has 42 pre-existing errors, all in `tests/`,
confirmed not a regression and out of CI's actual gate scope.
