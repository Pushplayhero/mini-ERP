# HANDOFF — mini-erp (context window rollover)

Rewritten 2026-08-15 (Week 7 session, hardening). Previous session's
context window filled up; work continues in a new Claude Code window on
the same machine. This doc replaces the earlier 2026-08-15 handoff (that
one's job — Week 6 receivables — is done and committed; see "Recent
history" below).

## Week 7 status (this session): slices 1–3 DONE and committed; 3 remain

**Current HEAD: `862375d`** — "Phase 1 / Week 7 slice 3: seed_demo CLI +
demo_o2c runner + Makefile". **Working tree is clean.**

Week 7 (Phase 1 hardening — the plan doc is
`docs/adr/WEEK7-phase1-hardening-brief.md`, ACCEPTED after a real 3-round
Codex architecture-consensus review, itself committed in slice 1) has 6
slices, each its own commit + a mandatory real Codex diff review (this is
a **user-mandated standing rule for this week specifically**: every slice,
no exceptions, regardless of how low-risk it looks):

1. **DONE, `93c135f`** — ADR-001 (modular monolith vs microservices) +
   ADR-002 (append-only fact tables / rebuildable summaries). 3 Codex
   review rounds to land (`terra` tier).
2. **DONE, `82a73b6`** — Full O2C E2E test
   (`tests/e2e/test_o2c_end_to_end.py`), per-step account-balance-delta
   assertions, non-zero AR/1100 tie-out proven at invoice-issue. 2 Codex
   review rounds (`sol` tier).
3. **DONE, `862375d`** — `app/cli/seed_demo.py` (idempotent demo-data
   seed, direct service-layer calls) + `app/cli/demo_o2c.py` (HTTP demo
   runner) + `Makefile` (`up`/`seed`/`demo`/`test`/`check`) +
   `tests/e2e/test_seed_idempotent.py` + `tests/e2e/test_demo_o2c_smoke.py`.
   The most contested slice — 3 Codex review rounds (`sol` tier), 5 real
   findings on the first pass (see the commit body / git log for the
   full account — the short version: get-or-create idempotency and
   remaining-work-aware stock reconciliation are genuinely easy to get
   subtly wrong, and `demo_o2c.py` re-introduced a bug class
   `seed_demo.py` had already been fixed for in the SAME session).
4. **NOT STARTED — next up.** ar-aging SQL rewrite
   (`app/modules/receivables/service.py::get_ar_aging`) — the ONE
   remaining slice that touches an existing correctness invariant (the
   ADR-008 Decision 5 tie-out property + R15 population semantics: a
   payment-only customer with no open invoice must still appear, with a
   negative net). Read `docs/adr/WEEK7-phase1-hardening-brief.md`
   Decision 3 in full before starting — it specifies the exact query
   shape (outer conditional aggregation over the existing `UNION ALL`)
   and a hard sequencing rule: **boundary characterization tests land
   FIRST** (add cases to `tests/receivables/test_aging.py` covering the
   exact bucket edges — 0/1, 30/31, 60/61, 90/91 days past due, multiple
   invoices in one bucket, a customer with both open balance AND
   unapplied credit — and confirm they pass against the CURRENT Python
   implementation before touching anything), THEN the SQL rewrite, THEN
   confirm the same characterization tests still pass (proving
   equivalence) plus the existing tie-out/payment-only/structural tests
   stay green unchanged. This is high-risk tier (`sol`) per the brief's
   Decision 6/O-4.
5. **NOT STARTED.** Property-over-events test
   (`tests/e2e/test_property_o2c_balances.py`) — a hypothesis property
   test driving domain events (not raw journal entries) through a
   generated-but-legal O2C operation plan, proving trial-balance-balance +
   AR/1100 tie-out + `on_hand >= 0` under any sequence. Read Decision 2 in
   the brief closely before starting: the harness MUST use a fresh company
   per Hypothesis example (never share/accumulate state the way the
   existing `tests/ledger/test_property_trial_balance.py` does), and this
   is a **binding DoD line, not droppable** — if the harness proves
   unreliable, formally defer it to Week 8 via a committed brief edit,
   never silently skip it.
6. **NOT STARTED — must be LAST.** README rewrite. Sequencing is load-
   bearing here too (Codex v1 caught this on the brief itself): README
   must not go first or mid-week, because it would advertise commands
   (`make seed`, `make demo`) that didn't exist yet when written — now
   that slices 1–3 are done, `make seed`/`make demo` are real, but the
   ar-aging rewrite (slice 4) and property test (slice 5) aren't yet, so
   still wait until after those land too.

**Read `docs/adr/WEEK7-phase1-hardening-brief.md` in full before doing
slice 4 or 5** — it is the normative spec (went through 3 rounds of real
Codex architecture-consensus review, with a full "Consensus Revisions"
record of every finding), not just a suggestion. Do not re-derive the
design from scratch; the hard parts (remaining-work-aware stock math,
per-document-type idempotency recovery, the aging query shape) are already
worked out there and in the slice-3 commit body.

**Codex review tier for the rest of the week**: `sol` (high-risk) for
slices 4 and 5 (both touch/prove correctness invariants), `terra`
(routine) would be defensible for slice 6 (pure docs) but the user's
standing instruction this week is every slice gets reviewed regardless —
don't skip it even for README.

**Dispatch mechanics** (same pattern used for every slice this session):
write a review prompt to a scratch `.md` file, `git add` the touched
files, `git diff --cached > <scratch>.patch`, `git reset` (unstage —
don't leave things staged), copy the patch into the repo working directory
(Codex's sandboxed `--cd` root can't see the scratch dir), then:
```
cat <prompt>.md | codex.cmd exec --sandbox read-only --cd C:/wt/merp -m <model> - > <output>.output
```
Read the `.output` file for a verdict line (`grep -n "^(APPROVED|REJECTED)"`
first — files can be huge). Verify every finding against the actual code
before fixing anything (this project's non-negotiable discipline — a
REJECTED verdict is input, not a command). Clean up the copied `.patch`/
`.md` files from the repo root before committing (they're scratch, not
deliverables). Iterate (re-diff, re-review, narrower prompt each round)
until APPROVED, then commit.

## Recent history (context, not action items)

- **Weeks 1–6 are committed and Codex-reviewed for real** (not
  self-review — see "Engineering workflow" below for why that distinction
  is a hard-learned, standing rule on this project). Week 6 (receivables,
  ADR-008) was the last functional module; it landed after a 5-round
  architecture consensus review and a 3-round diff review (4 real findings
  — two READ-COMMITTED atomicity bugs in `get_ar_aging`/`check_credit_limit`,
  a missing 409-body detail, a migration backfill preflight gap — all
  fixed and Codex-confirmed). Full details in that commit's body
  (`git log --grep "Week 6"` or `git show <sha> --stat`).
- Phase 1's five kernel modules (masterdata, ledger, sales, inventory,
  receivables) are all implemented, Codex-architecture-reviewed, and
  Codex-diff-reviewed. Week 7 is hardening on top of that, not new
  business logic.
- Earlier in the project's history (documented in a prior, now-superseded
  handoff), "Codex review" for Weeks 1–4 turned out to have actually been
  Claude self-review using a prompt template — caught, and every one of
  those weeks was genuinely re-reviewed from scratch before Week 6 began.
  This is why the "real `codex` CLI, never self-review" rule below is
  phrased as strongly as it is.

## Project

Open-source ERP kernel ("mini-erp"), Python/FastAPI, explicitly scoped to
eventually rival 鼎新 (Digiwin) on the O2C (order-to-cash) flow — portfolio /
career-leverage project, also a personal engineering challenge.

Repo root: this folder (`mini-erp/`).
Git remote: none configured yet — all work so far is local commits only.

## Engineering workflow (established preference — keep enforcing this)

For medium/high-risk work:
1. `/CODEX REVIEW ARCHITECTURE` (or an equivalent real architecture-
   consensus dispatch) on the design doc, before implementation. Nothing
   gets implemented until Consensus Status is APPROVED.
2. Implementation by Claude Code directly, or delegated to a `sonnet`
   agent, depending on session context.
3. A real Codex diff review after implementation, against the spec —
   **every slice this week, per explicit user instruction**, not just
   medium/high-risk ones.
4. `/CODEX REVIEW PRE-PUSH` before pushing multi-slice/production-facing
   work (no remote configured yet, so this hasn't been exercised, but stay
   ready for it).

**Standing constraint — do not relitigate**: "Codex review" must always
mean the real `codex` CLI (confirmed logged in on this machine via
`codex.cmd login status` → "Logged in using ChatGPT"), invoked from Claude
Code — never Claude generating both the plan and its own review of it.
This was violated silently through Weeks 1–4 of this project (caught and
fixed retroactively). If a future session's "Codex review" output looks
suspiciously fast, well-formatted, or absent of any actual `codex.cmd`
invocation in the transcript, stop and verify before trusting it.

**Push gate**: never `git commit`/push without the user's explicit
go-ahead in the current conversation — approval from an earlier turn or an
earlier session does not carry forward.

**Language**: the user's standing preference (memorized) is all
user-facing replies in Traditional Chinese (繁體中文) — English is fine in
code/commits/docs, but chat responses should be Chinese.

## Established cross-cutting doctrines (apply these, don't relitigate them)

- **Multi-tenancy**: `contextvars.ContextVar` + SQLAlchemy `do_orm_execute`
  hook + `with_loader_criteria`, fail-closed (`TenancyContextError` if no
  company bound). `TenantScopedMixin` on any table queried directly. Any
  Core-table (`sqlalchemy.table()`) reference used to dodge the
  cross-module-import contract must explicitly filter `company_id` itself.
  A standalone CLI (`app/cli/*`) running outside any HTTP request must
  bind `company_context(...)` explicitly before any tenant-scoped write —
  see `seed_demo.py`/`rebuild_ar_balances.py`/`rebuild_stock_summary.py`.
- **Money**: `NUMERIC(20,6)`; FX rates `NUMERIC(20,10)` + `rate_date`;
  round-half-even, enforced at the Pydantic-schema layer, not left to
  implicit DB column coercion. Money-typed Pydantic fields need `ge=0`/
  `gt=0` explicitly where negative/zero doesn't make sense.
- **Ledger**: double-entry, dual-currency lines, DEFERRED CONSTRAINT
  TRIGGER balance check at commit, immutability via `BEFORE UPDATE OR
  DELETE` triggers, gapless numbering (`ledger_sequences` + `FOR UPDATE`),
  reversal-only corrections. Reversals never copy the original entry's
  `source_type`/`source_id`. Trial balance is always an on-the-fly `SUM`
  aggregate (ADR-005 Decision 4) — `accounts` has NO balance column,
  unlike `stock_summary`/`invoices.settled_amount` (see ADR-002 for why
  that's a deliberate per-domain difference, not an inconsistency).
- **Event bus** (`app/core/events.py`): synchronous, in-process, same-
  transaction dispatch; `publish()` validates + outbox-writes + dispatches;
  `redispatch()` is the *only* replay entry point; every event schema must
  carry `company_id: uuid.UUID`; handler exceptions propagate unchanged
  (fail-closed). A standalone CLI that publishes events must
  `import app.main` first (side-effecting — installs schema registrations
  and posting-handler subscriptions) or the first `publish()` raises
  `UnknownEventTypeError`.
- **Hooks** (`app/core/hooks.py`): distinct from events — synchronous
  veto/augment points, not durably recorded, not replayed.
- **Plugins** (`app/plugins/`): exempt from the module-independence
  import-linter contract; core/modules may never import plugins.
- **Transaction ownership ("flush-only core + committing wrapper", ADR-003
  R1)**: most write functions in `service.py` modules only `flush()`; the
  HTTP router (or whatever caller opened the transaction — including a
  standalone CLI) owns commit/rollback. **The wrapper's try/except must
  wrap the core call itself, not just the final `commit()`** — the core's
  own `flush()` can raise `IntegrityError` too. Exception: `masterdata`
  and `ledger`'s simple `create_*` functions (company, customer, product,
  account, period) commit themselves via `_commit_or_conflict()` — this is
  an older, Week-1/2 convention that predates the flush-only-core doctrine
  and was never retrofitted; check which pattern a given function follows
  before assuming either one.
- **Idempotent event handlers**: partial unique index (`WHERE source_type
  IS NOT NULL`) + `session.begin_nested()` SAVEPOINT catching the
  resulting `IntegrityError` as an "already processed, skip" no-op.
- **Append-only facts + per-domain-decided caching** (ADR-002): every
  fact table (`journal_lines`, `stock_moves`, `payment_allocations`) is
  append-only, unconditionally. Whether the derived current-state
  aggregate is ALSO cached in a maintained column is decided per domain,
  on whether there's a concurrent capacity-check that needs a row to
  lock — ledger doesn't cache (no such check), inventory/receivables do.
  Don't assume this ADR means "always cache" or "always compute on read"
  — read it before touching either pattern.
- **Race prevention ("R1 doctrine")**: every state-machine transition
  takes `SELECT ... FOR UPDATE` on the row *before* re-checking its
  status. A derived report combining two related aggregates (e.g. an
  aging report, a credit-exposure sum) must read them via ONE SQL
  statement, not two separate `session.execute()` calls — a two-statement
  read is racy under READ COMMITTED even with no explicit lock bug (this
  bit both `get_ar_aging` and `check_credit_limit` in Week 6; see that
  commit's body, and the learned skill
  `postgres-read-committed-race-single-statement-fix` if this pattern
  comes up again).
- **Idempotent CLIs/scripts** (Week 7 addition): get-or-create by natural
  key is not enough on its own — also validate that an existing row's
  OTHER attributes match what you intended (fail loudly on a mismatch,
  per `seed_demo.py`/`demo_o2c.py`'s `compatible` callback pattern), and
  if a script reconciles a maintained quantity (like stock on-hand) to a
  target, that target must be **remaining-work-aware** (account for work
  not yet done), never a fixed number applied unconditionally — a fixed
  target silently drifts on rerun once some of the work is already done.
- **`_commit_or_conflict()`**: every service-layer commit catches
  `IntegrityError` → translates to `ConflictError` (409), never a raw 500.
- **Testing**: real PostgreSQL only, never SQLite/mocks.
  `tests/conftest.py` uses `testcontainers` when Docker is available,
  falls back to embedded `pgserver` — the pgserver DSN must use the real
  TCP `hostname`/`port` from `get_postmaster_info()`, not the pgdata
  directory path (that fails DNS resolution on Windows; see the learned
  skill `pgserver-windows-tcp-dsn`). Hypothesis property tests for
  invariants. A "fix a race condition by combining two statements into
  one" change can't be tested by reproducing the old race (the window no
  longer exists) — write a structural test instead, asserting the exact
  statement count via a `before_cursor_execute` SQLAlchemy event hook.
- **Alembic transaction scope**: the whole `upgrade head` run is one
  transaction — a migration referencing an enum value added by an
  *earlier* migration in the same chain must cast the *column* to text
  and compare against a plain string literal, never cast the literal to
  the enum type. A migration backfill relying on "exactly one matching
  row" must preflight-check BOTH the zero-match and duplicate-match cases
  before writing anything (`UPDATE ... FROM` picks arbitrarily among
  multiple matches) — see migration 0008 and the learned skill
  `migration-backfill-exactly-one-match-tests`.
- **Writing/reviewing a general-pattern ADR from multiple concrete
  instances**: separate what's genuinely uniform across instances from
  what's decided per-instance, and re-audit "Pros"/summary paragraphs
  specifically for silently re-flattening that distinction after drafting
  the split (this bit ADR-002 twice in the same review cycle) — see the
  learned skill `adr-backfill-uniform-vs-per-instance`.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata | committed (Week 1) | — |
| ledger | committed (Week 2–3), Codex-reviewed for real | ADR-005, ADR-003, ADR-004 |
| sales | committed (Week 4), Codex-reviewed for real | ADR-006 |
| inventory + shipping | committed (Week 5), Codex-reviewed for real | ADR-007 |
| receivables | committed (Week 6), Codex-reviewed for real | ADR-008 |
| Week 7 hardening | slices 1–3 of 6 committed and Codex-reviewed; slices 4–6 remain | `docs/adr/WEEK7-phase1-hardening-brief.md` |

## Running things

```
uv sync
docker compose up -d          # postgres for local dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest                 # uses testcontainers or embedded pgserver
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run lint-imports            # import-linter contracts
```

Or, once Week 7 slice 3's `Makefile` lands in your checkout: `make check`
runs the full ruff/mypy/lint-imports/pytest sequence in one command;
`make up`/`make seed`/`make demo` are the docker-compose quick-start (see
the Makefile itself and `docs/adr/WEEK7-phase1-hardening-brief.md`
Decision 4 for exactly what each does and why `seed` runs inside the
container while `demo` runs from the host).

**Windows long-path workaround** (this session's environment): the real
repo path is very long (Cowork-generated). A short junction
`C:\wt\merp` → the real repo path, plus a venv outside the long path
(`UV_PROJECT_ENVIRONMENT` pointed at `C:\wt\venv`), with `RUFF_CACHE_DIR`/
`MYPY_CACHE_DIR` also redirected to `C:\wt\.ruff_cache`/`C:\wt\.mypy_cache`
(the default cache-in-repo location hits the same long-path failure). Git
commands against the real path need `-c core.longpaths=true`. All local
check commands in this session were run via the `C:\wt\merp` junction with
those env vars set.

CI (`.github/workflows/`) runs the same checks; last known green state
hasn't been re-verified against current HEAD in this environment (no CI
runner available here) — the local check sequence (`make check` or the
five commands above) is the actual gate this session has been using.
