# HANDOFF — mini-erp (context window rollover)

Rewritten 2026-08-15 (Week 7 session, hardening — **Week 7 complete**).
Previous session's context window filled up; this doc replaces the
mid-week 2026-08-15 handoff (that one's job — landing slices 1–3 — is
done; this rewrite covers slices 4–6, which finish the week).

## Week 7 status: DONE — all 6 slices committed, each Codex-reviewed for real

**Current HEAD: `2f82276`** — "Phase 1 / Week 7 slice 6: README full
rewrite (Week 7 complete)". **Working tree is clean.**

Week 7 (Phase 1 hardening — the plan doc is
`docs/adr/WEEK7-phase1-hardening-brief.md`, ACCEPTED after a real 3-round
Codex architecture-consensus review, itself committed in slice 1) had 6
slices, each its own commit + a mandatory real Codex diff review (a
**user-mandated standing rule for the whole week**: every slice, no
exceptions, regardless of how low-risk it looked — and it paid off: even
the "pure docs" README slice got REJECTED four times before APPROVED).
All 6 DoD items from the brief's own DoD list are now satisfied — nothing
was silently dropped, and the one binding/droppable-only-via-explicit-
brief-edit item (the property test, Decision 2) landed properly instead
of needing deferral:

1. **`93c135f`** — ADR-001 (modular monolith vs microservices) + ADR-002
   (append-only fact tables / rebuildable summaries). 3 Codex review
   rounds (`terra` tier).
2. **`82a73b6`** — Full O2C E2E test
   (`tests/e2e/test_o2c_end_to_end.py`), per-step account-balance-delta
   assertions, non-zero AR/1100 tie-out proven at invoice-issue. 2 Codex
   review rounds (`sol` tier).
3. **`862375d`** — `app/cli/seed_demo.py` (idempotent demo-data seed,
   direct service-layer calls) + `app/cli/demo_o2c.py` (HTTP demo
   runner) + `Makefile` (`up`/`seed`/`demo`/`test`/`check`) +
   `tests/e2e/test_seed_idempotent.py` + `tests/e2e/test_demo_o2c_smoke.py`.
   3 Codex review rounds (`sol` tier), 5 real findings on the first pass
   — get-or-create idempotency and remaining-work-aware stock
   reconciliation are genuinely easy to get subtly wrong.
4. **`292f618`** — ar-aging SQL rewrite
   (`app/modules/receivables/service.py::get_ar_aging`), per
   `WEEK7-phase1-hardening-brief.md` Decision 3: bucketing moved from a
   Python loop into a single outer conditional-aggregation SQL query over
   the existing `UNION ALL`. Characterization tests
   (`tests/receivables/test_aging.py`) landed FIRST, confirmed against
   the OLD Python implementation, then confirmed unchanged against the
   NEW SQL implementation (equivalence proof) — the sequencing Decision 3
   mandates. 1 Codex review round (`sol` tier) — a real finding (an
   inaccurate inline comment claiming SQLAlchemy mistypes `Date - Date`
   as `Interval`, when the installed 2.0.52 already maps it to `Integer`)
   verified against the actual SQLAlchemy source and fixed.
5. **`401ca1f`** — Property-over-events test
   (`tests/e2e/test_property_o2c_balances.py`), per Decision 2: a
   Hypothesis test drawing a random-but-always-legal O2C operation plan
   (`_draw_plan`, an interactive `st.data()` draw that only ever offers
   legal actions) and executing it against the real service layer, with
   a **fresh company per Hypothesis example** (never accumulating state
   the way `tests/ledger/test_property_trial_balance.py`'s older pattern
   does) — proving trial-balance-balance + AR/1100 tie-out +
   `on_hand >= 0` hold. This was Decision 2's binding DoD line ("not
   droppable — either land it properly or formally defer to Week 8") and
   it landed properly; no deferral needed. 3 Codex review rounds (`sol`
   tier), 5 real findings across the first two rounds — two were genuine
   timezone/date-basis bugs (`ship_order`/`create_payment` stamp UTC
   `datetime.now()` while `create_invoice` defaults to local
   `date.today()`; on hosts west of UTC this could *systematically*, not
   just rarely, reject a legal plan) that a purely local test run would
   never have surfaced. See the commit body for the full account.
6. **`2f82276`** — README full rewrite, per Decision 5a, landed LAST per
   Decision 6's sequencing (must not advertise commands before they're
   real). One-line positioning, `docker compose`/`make seed`/`make demo`
   quick start, an inline mermaid C4-ish diagram, a "Design Decisions"
   section linking every ADR, accurate Non-Goals, and an O2C `curl`
   transcript ending in a balanced trial balance — **all captured from
   real command output**, not hand-written (ran a real app server against
   a freshly-migrated Postgres via embedded `pgserver`, no Docker in this
   environment). `terra` tier (routine, pure docs) but reviewed anyway
   per the standing rule — REJECTED 3 times, APPROVED on round 4. The
   findings were sharper than either code slice's: a hard-coded UUID in
   the transcript that would have broken on copy-paste, and half a dozen
   architecture claims (which modules publish vs. subscribe to which
   events, whether the `sales.goods_shipped` two-subscriber ordering is
   normative or incidental, the exact scope of the tenancy filter's
   `do_orm_execute` hook — SELECT only, not writes) that were each
   individually plausible-sounding but wrong on inspection. Worth
   internalizing: **documentation claims need the same verify-against-
   actual-code discipline as code claims** — "it sounds right" is not
   "it's true", and this project's own Codex-review culture caught that
   here just as reliably as it does in application code.

**Non-DoD, explicitly deferred (not started, not blocking anything)**:
demo GIF recording, `v0.1.0` git tag, public demo host, coverage badge —
these were always out of scope for Week 7 per the brief's own "Non-DoD"
list, not things that slipped.

## What's next

**Phase 1 ("Kernel") is now complete** — masterdata, ledger, sales,
inventory+shipping, receivables, all Codex-architecture-reviewed and
Codex-diff-reviewed, plus this week's hardening pass (E2E proof, property
tests, demo tooling, ar-aging SQL, and documentation that actually
matches reality). No open threads block calling Phase 1 done.

Two paths from here, not yet decided:
- **Week 8 polish** (the explicitly-deferred items above: demo GIF,
  `v0.1.0` tag, public demo host, coverage badge) — small, low-risk,
  mostly project-presentation work.
- **Phase 2 ("Platform")** per `docs/open-erp-master-plan.md` §1 — plugin
  loader (dynamic discovery/registration, vs. today's one hard-wired
  demo plugin), custom-fields UI/admin surface (the `custom_data JSONB`
  mechanism already exists on primary entities, per this week's README
  audit — just no UI/central field-definition table), workflow/approval
  engine, RBAC (today's `X-Company-Id` header is a documented stand-in
  for a verified auth claim, not real auth). This is a much bigger
  planning lift than any Week 7 slice — start with architecture
  discovery and a real Codex consensus review on the Phase 2 brief
  before writing any code, same discipline as every phase/week so far.

Whoever picks this up next should ask the user which path they want
before assuming either.

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
   **every slice, every week, per explicit user instruction now proven
   out twice** (Week 7's ar-aging slice and README slice both looked
   "surely fine" and both had real findings on first review).
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
earlier session does not carry forward. Held exactly this way for all 6
Week 7 slices.

**Language**: the user's standing preference (memorized) is all
user-facing replies in Traditional Chinese (繁體中文) — English is fine in
code/commits/docs, but chat responses should be Chinese.

## Established cross-cutting doctrines (apply these, don't relitigate them)

- **Multi-tenancy**: `contextvars.ContextVar` + SQLAlchemy `do_orm_execute`
  hook + `with_loader_criteria`, fail-closed (`TenancyContextError` if no
  company bound) — **but note the hook only fires for ORM `SELECT`
  statements** (`app/core/db.py` returns immediately for any non-SELECT);
  write-safety is a separate convention (every INSERT stamps `company_id`
  from `require_current_company_id()`, and every mutate-by-id re-fetches
  the row through a hook-filtered SELECT first) — this distinction was
  fuzzy in prose until the Week 7 README review forced it precise; see
  README.md's "Multi-tenancy" section for the accurate version.
  `TenantScopedMixin` on any table queried directly. Any Core-table
  (`sqlalchemy.table()`) reference used to dodge the cross-module-import
  contract must explicitly filter `company_id` itself. A standalone CLI
  (`app/cli/*`) running outside any HTTP request must bind
  `company_context(...)` explicitly before any tenant-scoped write —
  see `seed_demo.py`/`rebuild_ar_balances.py`/`rebuild_stock_summary.py`.
- **Money**: `NUMERIC(20,6)`; FX rates `NUMERIC(20,10)` + `rate_date`;
  round-half-even is enforced at the Pydantic-schema layer **specifically
  for journal line amounts (`ledger.schemas`) and receivables
  payment/allocation amounts (`receivables.schemas`)** — not universally;
  e.g. `Product.list_price`/`standard_cost` are `NUMERIC(20,6)`-typed but
  not yet explicitly quantized at the schema layer (a real gap the Week 7
  README review surfaced, not yet fixed in code — worth a future slice).
  Money-typed Pydantic fields need `ge=0`/`gt=0` explicitly where
  negative/zero doesn't make sense.
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
  comes up again). Week 7's ar-aging SQL rewrite (slice 4) preserved this
  property by construction — the whole point of moving bucketing into the
  SQL was to keep it one statement, now trivially so (one outer SELECT).
- **Idempotent CLIs/scripts** (Week 7 addition): get-or-create by natural
  key is not enough on its own — also validate that an existing row's
  OTHER attributes match what you intended (fail loudly on a mismatch,
  per `seed_demo.py`/`demo_o2c.py`'s `compatible` callback pattern), and
  if a script reconciles a maintained quantity (like stock on-hand) to a
  target, that target must be **remaining-work-aware** (account for work
  not yet done), never a fixed number applied unconditionally — a fixed
  target silently drifts on rerun once some of the work is already done.
  Slice 5's property test hit the same trap from a different angle:
  seeding stock to cover every drawn order's qty (not just the orders
  that actually ship) over-provisions and can hide a double-decrement bug
  behind the slack — seed exactly what the plan's `Ship` ops will
  actually consume, no more.
- **`_commit_or_conflict()`**: every service-layer commit catches
  `IntegrityError` → translates to `ConflictError` (409), never a raw 500.
- **Testing**: real PostgreSQL only, never SQLite/mocks.
  `tests/conftest.py` uses `testcontainers` when Docker is available,
  falls back to embedded `pgserver` — the pgserver DSN must use the real
  TCP `hostname`/`port` from `get_postmaster_info()`, not the pgdata
  directory path (that fails DNS resolution on Windows; see the learned
  skill `pgserver-windows-tcp-dsn`). Hypothesis property tests for
  invariants — and when a property test spans multiple modules/a domain
  state machine, give each Hypothesis example a **fresh company**, never
  accumulate state across examples the way
  `tests/ledger/test_property_trial_balance.py`'s older single-invariant
  pattern does (see `tests/e2e/test_property_o2c_balances.py`'s module
  docstring for the full reasoning — shared state makes failures order-
  dependent and Hypothesis shrinking unreliable). A "fix a race condition
  by combining two statements into one" change can't be tested by
  reproducing the old race (the window no longer exists) — write a
  structural test instead, asserting the exact statement count via a
  `before_cursor_execute` SQLAlchemy event hook.
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
- **Documentation claims need the same rigor as code claims** (Week 7
  slice 6 addition): a README/doc sentence that "sounds right" — which
  modules publish vs. subscribe to which events, exactly what a tenancy
  hook filters, whether a test proves something "arbitrary" vs. "many
  bounded examples of" — needs the same grep-the-actual-code
  verification as a code review finding, before AND after Codex flags it.
  Four review rounds on a "pure docs" slice is not overkill; it's the
  discipline working as intended.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata | committed (Week 1) | — |
| ledger | committed (Week 2–3), Codex-reviewed for real | ADR-005, ADR-003, ADR-004 |
| sales | committed (Week 4), Codex-reviewed for real | ADR-006 |
| inventory + shipping | committed (Week 5), Codex-reviewed for real | ADR-007 |
| receivables | committed (Week 6), Codex-reviewed for real | ADR-008 |
| Week 7 hardening | **all 6 slices done**, Codex-reviewed for real | `docs/adr/WEEK7-phase1-hardening-brief.md`, ADR-001, ADR-002 |

**Phase 1 ("Kernel") is complete.** See "What's next" above for the two
undecided paths forward (Week 8 polish vs. Phase 2 planning).

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

Or, with the `Makefile` (landed Week 7 slice 3): `make check` runs the
full ruff/mypy/lint-imports/pytest sequence in one command; `make up` /
`make seed` / `make demo` are the docker-compose quick-start (`up` blocks
until `/health` responds; `seed` runs inside the container against its
own `DATABASE_URL`; `demo` runs from the host as a real HTTP client
against the published port — see the Makefile and README.md's "Quick
start" for why each runs where it does).

**Windows long-path workaround** (this session's environment): the real
repo path is very long (Cowork-generated). A short junction
`C:\wt\merp` → the real repo path, plus a venv outside the long path
(`UV_PROJECT_ENVIRONMENT` pointed at `C:\wt\venv`), with `RUFF_CACHE_DIR`/
`MYPY_CACHE_DIR` also redirected to `C:\wt\.ruff_cache`/`C:\wt\.mypy_cache`
(the default cache-in-repo location hits the same long-path failure). Git
commands against the real path need `-c core.longpaths=true`. All local
check commands across the Week 7 session were run via the `C:\wt\merp`
junction with those env vars set — reuse the same junction/venv if it
still exists at session start rather than recreating it.

**No Docker in this environment** — `make up`/`make seed`/`make demo`
were never exercised via the real Makefile path this session; the README
slice's "real output" transcripts were captured by manually reproducing
the same flow (migrate + run the app server against an embedded
`pgserver` instance + run `seed_demo.py`/`demo_o2c.py`/raw `curl`
directly) rather than via `docker compose`. If Docker becomes available
in a future session, it's worth actually running `make up && make seed &&
make demo` once to confirm the Makefile path itself works end to end —
it never has been, only its constituent scripts have.

CI (`.github/workflows/`) runs the same checks; last known green state
hasn't been re-verified against current HEAD in this environment (no CI
runner available here) — the local check sequence (`make check` or the
five commands above) is the actual gate this session has been using. All
222 tests (up from 221 after Week 7 slice 5 added
`test_property_o2c_balances.py`) pass locally as of `2f82276`;
ruff/mypy/lint-imports all clean (mypy carries 43 pre-existing errors in
unrelated test-helper files, confirmed unchanged across every Week 7
slice via `git stash` comparison — not a regression, just never cleaned
up; a future slice could tackle `mypy --strict` compliance in
`tests/*/_helpers.py` if that's ever prioritized).
