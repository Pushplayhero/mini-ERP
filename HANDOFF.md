# HANDOFF — mini-erp (context window rollover)

Rewritten 2026-08-15 (Week 6 session, receivables). Previous session's
context window filled up; work continues in a new Claude Code window on
the same machine. This doc replaces the earlier 2026-08-15 handoff (that
one's job — Week 5 wrap-up — is done and committed at `9bf9949`).

## Week 6 status (this session): receivables — DONE, committed at `ab0e407`

**Committed.** Week 6 (receivables) landed on `master` as `ab0e407`
("Phase 1 / Week 6: receivables ..."), 34 files, +5709/-83. Working tree
is clean. This closes Phase 1's Week 1–6 functional scope — all five
kernel modules (masterdata, ledger, sales, inventory, receivables) are
implemented and Codex-reviewed. What remains for Phase 1 is Week 7–8
hardening/polish (E2E, seed data, report-query perf, README/ADR/demo,
v0.1.0 tag) — see "Roadmap after this handoff" at the bottom. The
detailed review-history record below is retained for provenance.

---

### Detailed Week 6 record (retained for provenance)

ADR-008 (receivables: invoicing, payment application, AR aging) passed a
real `codex` CLI consensus review after 5 rounds (v1-v4 REJECTED with real
findings each time, v5 APPROVED) — see `docs/adr/ADR-008-receivables.md`
"Consensus Revisions" for the full R1-R17 history. Implementation is
complete against that ADR: migration 0008, the new `receivables` module
(models/schemas/events/service/router), `ledger` control-account
protection (R5/R11), `sales_orders.shipped_at` persistence (R13/R17),
masterdata TWD-only enforcement (R12), the credit-limit plugin's revised
exposure formula (Decision 6), and `app/cli/rebuild_ar_balances.py`.

**A real `codex exec -m gpt-5.6-sol` diff review (high-risk tier) then ran
against the full implementation diff and returned REJECTED with 4 findings**
— all 4 verified against actual code/ADR text (not blindly trusted) and
confirmed real, then fixed:
1. `receivables.service.get_ar_aging` read open-invoice balances and
   unapplied credits via two separate `session.execute()` calls — a
   concurrently-committing `allocate_payment` between them could produce a
   report that doesn't tie to the ledger (ADR-008 Decision 5's defining
   property). Fixed: combined into one `UNION ALL` statement (one READ
   COMMITTED snapshot for both sides).
2. `credit_limit.check_credit_limit` had the identical two-statement
   mixed-snapshot problem for its uninvoiced-orders/open-invoice-balance
   sum — the customer lock serializes concurrent confirms but not against
   `receivables.void_invoice`, so a void landing between the two reads
   could drop an order out of both sums at once, understating exposure.
   Fixed: combined into one SELECT via two `.scalar_subquery()`s.
3. `receivables.router.create_payment`'s duplicate-`external_ref` 409 was a
   generic message, but ADR-008 R2 explicitly requires the response body to
   identify the existing payment (so a client retrying an uncertain POST
   can reconcile). Fixed: `_is_external_ref_conflict()` (constraint-name
   check, same idiom as `ledger.posting._is_source_conflict`) + a new
   `service.get_payment_by_external_ref()` lookup, named in the 409 detail.
4. Migration 0008's `shipped_at` backfill only checked "still NULL after
   the UPDATE?" — silent against a shipped order with 2+ matching
   `sales.goods_shipped` outbox events, since `UPDATE ... FROM` picks an
   arbitrary match when there's more than one (a violation of the
   idempotent-posting invariant the backfill relies on, but not one the old
   check could ever catch). Fixed: a preflight `LEFT JOIN ... HAVING
   count(ob.id) <> 1` raises before any row is written.

Each fix has a new regression test (2 structural "exactly N statements"
tests for findings 1/2 — the underlying races aren't practically
reproducible from application-level test code once the reads are genuinely
atomic — plus a strengthened 409-body assertion for finding 3 and a
duplicate-outbox-event migration test for finding 4).

**All local checks are green after the fixes**: `ruff check .`,
`ruff format --check .`, `lint-imports` (5/5 contracts kept), `mypy`
(scoped re-check of every touched file: clean; full-repo run shows only
pre-existing test-file debt, unrelated), and the full `pytest` suite —
**211 tests passing** (208 baseline + 3 net-new), run from a fresh database
exercising the complete migration chain 0001→0008.

**A second Codex diff-review pass verifying the 4 fixes ran and returned
REJECTED again — but narrowly**: findings 1-3 confirmed RESOLVED outright
(AR aging UNION ALL atomicity, credit-limit combined-select atomicity,
external_ref 409 detail), and finding 4's own migration SQL was also
confirmed correct (`LEFT JOIN ... HAVING count(ob.id) <> 1` genuinely
catches both zero-match and multi-match cases, `event_type` correctly
scoped in the `ON` clause) — but the test suite only exercised the
multi-match half, leaving a hypothetical `LEFT JOIN` → `INNER JOIN`
regression free to pass every test. Verified against the actual test file
(confirmed: only a 1-match success case and a 2-match rejection case
existed, no 0-match case) and fixed: added
`tests/test_migrations.py::test_migration_0008_shipped_at_backfill_rejects_zero_matching_outbox_events`
— a SHIPPED order with zero matching outbox rows, plus one *decoy* outbox
row (right `event_type`, wrong `source_id`) proving the join's `source_id`
filter is doing real work, asserting the preflight's own error message
(not the older post-backfill backstop's different message). Full suite
re-verified green: **212 tests passing** (211 + this one).

**A third Codex diff-review pass, scoped narrowly to just the new
zero-match test, returned APPROVED**: confirmed the decoy outbox row's
`source_id` cannot satisfy the join, confirmed the assertion matches the
preflight's own message (not the older backstop's differently-worded one,
so a `LEFT JOIN` → `INNER JOIN` regression would now fail this test), and
confirmed the cleanup correctly restores the shared `db_engine` fixture to
head with no tenant-scoping or test-race issue. **This closes out the
entire ADR-008 diff-review gate — all 4 original findings resolved and
Codex-confirmed, nothing outstanding.**

**Working tree is dirty — nothing from this session is committed.** This
is now the actual next step, and it needs the user's explicit go-ahead
(this project's standing push-gate rule — never commit without being
asked):
1. `git add -A && git commit` (branch first if still on `master`/`main` —
   this repo is still on `master`).
2. `/CODEX REVIEW PRE-PUSH` before pushing, if/when a remote exists.

**One self-caught bug worth knowing about**: the receivables router
originally returned the just-mutated ORM object directly after `commit()`
for `void_invoice`/`void_payment`/`create_payment`(with allocations) —
this hit the *exact* `updated_at`/`MissingGreenlet` issue `sales.router`'s
own comments already document (an UPDATE-triggered `onupdate=func.now()`
column gets expired, not repopulated, and Pydantic serializing it outside
an `await` raises). Fixed by re-selecting via `service.get_invoice`/
`get_payment` after every write, matching `sales.router`'s established
pattern — applied uniformly to all five write endpoints. If you add a new
receivables write endpoint, re-select before serializing.

**Also fixed this session, unrelated to receivables but blocking all local
testing**: `tests/conftest.py`'s pgserver (Docker-unavailable) fallback
built its DSN with `host=<pgdata directory path>`, which the previous
handoff described as "does NOT work on Windows" — actually just a bug:
`pgserver`'s own `get_postmaster_info()` reports a real TCP
`hostname`/`port` (127.0.0.1, ephemeral port) that works fine on Windows.
Fixed; Windows sandboxes without Docker can now run the real test suite.

## Project

Open-source ERP kernel ("mini-erp"), Python/FastAPI, explicitly scoped to
eventually rival 鼎新 (Digiwin) on the O2C (order-to-cash) flow — portfolio /
career-leverage project, also a personal engineering challenge.

Repo root: this folder (`mini-erp/`).
Git remote: none configured yet — all work so far is local commits only.
Current HEAD: `9bf9949` — "Phase 1 / Week 5: inventory + shipping, plus
Week 2-4 consensus review fixes". **Working tree is DIRTY** — Week 6
(receivables) is implemented and locally verified (see above) but not yet
committed; see the Week 6 status section above for what's next.

## State of the world right now

- **Weeks 1–5 are committed and, as of this session, genuinely
  Codex-reviewed** — not just Week 5. Earlier sessions' "Codex review" for
  Weeks 1–4 turned out to have actually been Claude self-review using the
  `codex-reviewer` skill's prompt templates (never a real independent
  `codex` CLI run) — caught mid-Week-5, documented in the prior HANDOFF.md,
  and corrected this session by re-running a genuine Claude-Codex
  consensus review against Week 2 (ADR-005), Week 3 (ADR-003/004), and
  Week 4 (ADR-006) from scratch, on top of Week 5's own (already-correct)
  real Codex review.
- All of it was REJECTED on the first pass, multiple real bugs were found
  and fixed (see "What just got fixed" below), re-reviewed, and the final
  state on every one of Weeks 2–5 is **Codex APPROVED**. Full commit
  message on `9bf9949` has the complete finding-by-finding account — read
  `git show 9bf9949 --stat` / the commit body before assuming anything
  about Weeks 2–4 is still suspect.
- Full local check sequence is green as of `9bf9949`: `ruff check`,
  `ruff format --check`, `mypy`, `lint-imports` (5/5 import-linter
  contracts kept), and the full `pytest` suite — **169 tests passing**
  against real PostgreSQL via testcontainers, run from a fresh database
  (exercises the complete migration chain 0001→0007 in one transaction).

## What just got fixed (so you don't re-litigate it)

Genuine, Codex-confirmed bugs found and fixed this session, most
significant first:

1. **Accounting-critical (Week 3)**: `_post_reversal` used to copy the
   original entry's `source_type`/`source_id` onto the reversal, colliding
   with `uq_journal_entries_source` — **every reversal of an event-sourced
   entry failed**, including all Week 5 COGS postings. Fixed: reversals now
   get `source_type=None`/`source_id=None` (traceable via `reversal_of_id`
   instead). If you ever see a 409 on `/journal-entries/{id}/reverse` for
   an entry with a `source_type`, that regression came back — check
   `ledger.service._post_reversal` first.
2. `post_journal_entry`/`_post_reversal`'s internal `flush()` could raise
   `IntegrityError` that bypassed the router/wrapper's 409 translation
   entirely, surfacing as an unhandled 500. Fixed in `ledger.service`
   (`create_journal_entry`, `reverse_journal_entry`) and mirrored in
   `sales.router` (`confirm_sales_order`, `ship_sales_order`) — the
   try/except must wrap the *core service call*, not just the later
   `commit()`. Watch for this exact pattern in any new flush-only-core +
   committing-wrapper function you add.
3. `replay_outbox.py`'s payload parsing ran outside the per-row
   try/except — a malformed payload used to abort the whole replay run
   instead of failing just its own row (ADR-004 R3).
4. Ledger: functional currency was hardcoded to TWD in `ledger.service`
   while `masterdata` allowed any registered currency at company creation
   — `CompanyCreate.functional_currency_code` now rejects non-TWD in
   Phase 1. No app-level round-half-even rounding on journal amounts
   (relied on Postgres's ties-away-from-zero column coercion) — fixed via
   a Pydantic validator. Trial balance's accounts join was missing an
   explicit `company_id` filter its own docstring claimed existed — fixed
   (defense-in-depth; not an active leak under normal writes).
5. `unregister()` in both `app.core.hooks` and `app.core.events` only
   removed the *first* occurrence of a duplicate registration — fixed to
   remove all occurrences.
6. **Sales confirm-time repricing (Week 4, the big one)**: `confirm_order`
   never recomputed `unit_price`/`amount`/`total` from current masterdata,
   contradicting ADR-006 Decision 3's own text ("a draft that sat for a
   week confirms at current prices unless lines carried manual
   overrides"). Fixed via a new `SalesOrderLine.unit_price_is_override`
   column (migration 0007) — non-override lines now reprice at confirm
   time; override lines don't. Getting this fix right took two more Codex
   rounds (R2's positive-total check was validating the stale pre-reprice
   total; `list_price` had no floor, so a negative price could crash
   confirm's flush() as an unhandled 500; the new snapshot CHECK
   constraint didn't cover `SHIPPED` orders) — all fixed, see the commit
   body for details if you touch `confirm_order` again.
7. New DB constraint: `sales_orders` confirmed/shipped orders must carry a
   customer snapshot (migration 0006) — a bypass writer previously could
   persist one without.

**One environment quirk worth knowing**: this project's `alembic/env.py`
runs the *entire* `upgrade head` sequence in one transaction (no
`transaction_per_migration`). A migration that references an enum value
added by an *earlier* migration in the same upgrade run will hit
Postgres's `UnsafeNewEnumValueUsageError` unless you cast the *column* to
text and compare against a plain string literal, instead of casting the
literal to the enum type — see migration 0006's docstring
(`ck_sales_orders_confirmed_has_snapshot`) for the worked example. This
will bite you again the next time a new migration needs to reference the
`SHIPPED` enum value (or any enum value added within the last few
migrations of the chain).

## Planning documents (read these first, in order)

1. `open-erp-master-plan.md` — Phase 1–4 roadmap, full O2C→ERP scope vs.
   鼎新. §10 "審查修訂記錄" holds R1–R5 cross-cutting decisions (money
   precision, multi-company isolation, plugin trust model, outbox delivery,
   inventory concurrency) that every module implements.
2. `mini-erp-architecture.md` — original Phase 1 sketch (superseded in
   detail by the ADRs below, kept for context).
3. `docs/adr/ADR-005-ledger-journal-design.md` — ledger/journal design.
4. `docs/adr/ADR-003-posting-engine.md` — posting engine (events → journal
   entries).
5. `docs/adr/ADR-004-event-bus.md` — event bus (publish/subscribe/replay).
6. `docs/adr/ADR-006-sales-and-hook-registry.md` — sales order lifecycle +
   hook registry + credit-limit plugin.
7. `docs/adr/ADR-007-inventory-and-shipping.md` — inventory + shipping.

Each ADR has a "Consensus Revisions" section (R1, R2, R3…) — these are not
optional follow-ups, they're load-bearing fixes found during architecture
review. Do not "clean up" code that looks redundant against an Rn note
without rereading the note first. As of this handoff, every ADR's
implementation has also passed a genuine Codex diff review (not just the
architecture review) — see "What just got fixed" above for the load-bearing
corrections that came out of that.

## Engineering workflow (established preference — keep enforcing this)

For medium/high-risk work:
1. `/CODEX REVIEW ARCHITECTURE` on the ADR draft, before implementation.
   Nothing gets handed to an implementation agent until Architecture
   Consensus Status is APPROVED.
2. Implementation delegated to a `sonnet` agent (Agent tool, `model: sonnet`)
   or done directly by Claude Code, depending on session context.
3. `/CODEX REVIEW DIFF` after implementation, against the ADR.
4. `/CODEX REVIEW PRE-PUSH` before pushing multi-slice/production-facing
   work.

**Standing constraint, confirmed twice now — do not relitigate**: "Codex
review" must always mean the real `codex` CLI (confirmed logged in on this
machine), invoked from Claude Code — never Claude generating both the plan
and its own review of it. This was violated silently through Weeks 1–4
(caught mid-Week-5, 2026-08-14) and the fix (redoing the review for real)
is what most of this session's work was. If a future session's "Codex
review" output looks suspiciously fast, well-formatted, or absent of any
actual `codex.cmd` invocation in the transcript, stop and verify before
trusting it.

## Established cross-cutting doctrines (apply these, don't relitigate them)

- **Multi-tenancy**: `contextvars.ContextVar` + SQLAlchemy `do_orm_execute`
  hook + `with_loader_criteria`, fail-closed (`TenancyContextError` if no
  company bound). `TenantScopedMixin` on any table queried directly. Any
  Core-table (`sqlalchemy.table()`) reference used to dodge the
  cross-module-import contract must explicitly filter `company_id` itself
  — this was missed once (ledger's trial balance) and fixed this session;
  double-check new Core-table references for the same predicate.
- **Money**: `NUMERIC(20,6)`; FX rates `NUMERIC(20,10)` + `rate_date`;
  round-half-even, enforced at the Pydantic-schema layer (see
  `ledger.schemas.JournalLineCreate`'s `field_validator`), not left to
  implicit DB column coercion. Money-typed Pydantic fields need `ge=0`
  explicitly where negative doesn't make sense — don't assume the DB
  CHECK alone is enough; see `masterdata.schemas.ProductCreate/Update
  .list_price` for the pattern.
- **Ledger**: double-entry, dual-currency lines, DEFERRED CONSTRAINT
  TRIGGER balance check at commit, immutability via `BEFORE UPDATE OR
  DELETE` triggers, gapless numbering (`ledger_sequences` + `FOR UPDATE`),
  reversal-only corrections. **Reversals never copy the original entry's
  `source_type`/`source_id`** — see fix #1 above.
- **Event bus** (`app/core/events.py`): synchronous, in-process, same-
  transaction dispatch; `publish()` validates + outbox-writes + dispatches;
  `redispatch()` is the *only* replay entry point (never re-`publish()`);
  every event schema must carry `company_id: uuid.UUID`; handler exceptions
  propagate unchanged (fail-closed). `unregister()` removes *all*
  occurrences of a handler, not just the first.
- **Hooks** (`app/core/hooks.py`): distinct from events — synchronous
  veto/augment points, not durably recorded, not replayed. Same
  `unregister()` all-occurrences fix applies here too.
- **Plugins** (`app/plugins/`): exempt from the module-independence
  import-linter contract (may import core + any module); core/modules may
  never import plugins. Plugins importing real ORM models (not Core-table
  shadows) is an intentional, documented ADR-006 trade-off — don't "fix"
  it without re-reading ADR-006 Decision 2 first.
- **Transaction ownership ("flush-only core + committing wrapper", ADR-003
  R1)**: `post_journal_entry`, `confirm_order`, `ship_order` etc. only
  `flush()`; the HTTP router or the handler that opened the SAVEPOINT owns
  commit/rollback. "The transaction belongs to whoever opened it." **The
  wrapper's try/except must wrap the core call itself, not just the final
  `commit()`** — the core's own `flush()` can raise `IntegrityError` too;
  see fix #2 above. Check this explicitly for any new flush-only-core
  function you write.
- **Idempotent event handlers**: partial unique index (`WHERE source_type
  IS NOT NULL`) + `session.begin_nested()` SAVEPOINT catching the resulting
  `IntegrityError` as an "already processed, skip" no-op. Used by posting
  (`journal_entries`) and inventory (`stock_moves`). A row with a NULL
  `source_type` (manual entries, reversals) is deliberately excluded from
  the uniqueness check — don't ever populate `source_type` on a reversal.
- **Race prevention ("R1 doctrine")**: every state-machine transition takes
  `SELECT ... FOR UPDATE` on the row *before* re-checking its status.
  Applied to accounting periods, sales order confirm/cancel/ship.
- **`_commit_or_conflict()`**: every service-layer commit catches
  `IntegrityError` → translates to `ConflictError` (409), never a raw 500.
  Only wraps `commit()` — doesn't help a flush-only core's own `flush()`;
  see fix #2.
- **Testing**: real PostgreSQL only, never SQLite/mocks. `tests/conftest.py`
  uses `testcontainers` when Docker is available, falls back to embedded
  `pgserver` (the pgserver fallback path does NOT work on Windows — passes
  a Windows directory path as asyncpg's `host`, which fails DNS resolution;
  use Docker on Windows). Hypothesis property tests for invariants (e.g.
  trial balance always balances).
- **Alembic transaction scope**: the whole `upgrade head` run is one
  transaction (see "One environment quirk" above) — matters for any
  migration referencing an enum value added earlier in the same chain.

## Module status

| Module | State | ADR |
|---|---|---|
| masterdata | committed (Week 1) | — |
| ledger | committed (Week 2–3), Codex-reviewed for real | ADR-005, ADR-003, ADR-004 |
| sales | committed (Week 4), Codex-reviewed for real | ADR-006 |
| inventory + shipping | committed (Week 5), Codex-reviewed for real | ADR-007 |
| receivables | **implemented, tests green, NOT committed, NOT yet Codex-diff-reviewed** (this session) | ADR-008 (architecture APPROVED v5; diff review still pending) |

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

On Windows, if the repo path is very long (this project's Cowork-generated
path is), `uv`/`ruff`/`mypy` can hit `MAX_PATH`-related failures. Workaround
used this session: a short junction (`mklink /J C:\wt\merp <repo path>`)
plus a venv created outside the long path (`UV_PROJECT_ENVIRONMENT` pointed
at `C:\wt\venv`, or just `uv sync --python 3.12` from the junction). Also:
if `git` commands fail with "Filename too long", pass `-c core.longpaths=true`.

CI (`.github/workflows/`) runs the same checks; last known green at the
Week 4 commit — hasn't been re-verified against `9bf9949` (no CI runner
available in this environment).

## Roadmap after this handoff

Phase 1 Weeks 1–6 are complete and committed (`ab0e407`). What remains is
Week 7 (hardening) + Week 8 (release polish).

**Week 7 plan is ACCEPTED** — `docs/adr/WEEK7-phase1-hardening-brief.md`
passed a real Codex architecture-consensus review across 3 rounds (v1/v2
NEEDS REVISION with real findings verified against the repo and folded in,
v3 APPROVED 2026-08-15). The plan covers: a full O2C E2E test with per-step
balance-delta assertions; a hypothesis property test over domain-event
sequences (isolated-per-example harness, proving trial-balance balance +
AR/1100 tie-out + on_hand>=0); a fresh-DB-safe idempotent `seed_demo` CLI +
`demo_o2c` runner + `Makefile`; an ar-aging SQL bucketing rewrite (the one
correctness-adjacent slice, guarded by boundary characterization tests
landed first); and README rewrite + ADR-001/002 backfill.

**Implementation sequencing** (Decision 6 — each slice = one commit + a
Codex diff review; tier `sol` for slices 1–4 tests/seed/aging, `terra` for
docs). NB: **user has mandated a Codex diff review on EVERY slice.** Order:
1. **DONE, committed `93c135f`** — ADR-001 (modular monolith vs
   microservices) + ADR-002 (append-only fact tables/rebuildable
   summaries) — pure docs of already-shipped decisions. Took 3 Codex
   diff-review rounds (`terra` tier) to land, all real findings: v1 caught
   ADR-002 wrongly claiming the ledger has a "maintained summary column"
   (it doesn't — trial balance is always an on-the-fly aggregate, ADR-005
   Decision 4); the fix required restructuring ADR-002 around two
   separable questions (is the fact table append-only — uniform axiom; is
   the aggregate additionally cached — a genuine per-domain trade-off,
   decided differently for ledger vs. inventory/receivables); v2 then
   caught the rewrite's own "Pros" paragraph re-conflating the two
   questions (attributing all three invariants — trial balance, AR
   tie-out, stock non-negative — to "append-only alone", when each is
   actually enforced by its own separate mechanism); v3 APPROVED.
2. **DONE** — Full O2C E2E test (`tests/e2e/test_o2c_end_to_end.py`): one
   test function, create → confirm → ship → invoice → pay → allocate,
   per-step account-balance-delta snapshots (not just a global "still
   balances" check), non-zero AR/1100 tie-out proven at invoice-issue.
   Took 2 Codex diff-review rounds (`sol` tier — high-risk despite being
   test-only, since it exercises the full posting chain): v1 confirmed
   every accounting assertion correct (account/amount pairs match
   `POSTING_RULES`, the `_delta` helper correctly distinguishes
   unchanged/absent/wrong-account/wrong-amount, tenant isolation fine, the
   credit-limit hook genuinely exercised, R15 population logic correct)
   but caught ONE real bug: the test hard-coded `create_period(2026, 8)`
   while every event in the flow (ship/invoice/payment) defaults its own
   date to the real wall clock — meaning the test would silently start
   failing the moment real calendar time crosses into September 2026 (a
   coincidental match with this sandbox's current date, not a derived
   one). Fixed by deriving the period from `date.today()` at test-run
   time. v2 APPROVED. **Next: commit this slice.**
3. Seed + Makefile + demo runner (`app/cli/seed_demo.py`,
   `app/cli/demo_o2c.py`, `Makefile`, compose seed path) + run-twice
   idempotency test.
4. ar-aging SQL rewrite (`receivables.service.get_ar_aging`) — boundary
   characterization tests in `tests/receivables/test_aging.py` FIRST.
5. Property-over-events test (`tests/e2e/test_property_o2c_balances.py`) —
   last; if its harness proves unreliable, formally defer to Week 8 in a
   committed brief edit (do NOT silently drop — it's a binding DoD line).
6. README rewrite — LAST, once every command/transcript it documents exists.

Then Week 8: demo GIF, `v0.1.0` tag, (optional) public demo host, CI
coverage badge — none of which are in the Week 7 brief's scope.

**The Week 7 brief file** (`docs/adr/WEEK7-phase1-hardening-brief.md`) is
currently UNCOMMITTED (working tree has it as a new untracked file). It can
be committed on its own, or folded into slice 1's commit — either is fine;
it is the normative spec for everything above.
