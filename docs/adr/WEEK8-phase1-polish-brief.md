# Week 8 — Phase 1 wrap-up / polish brief (for architecture consensus review)

Status: ACCEPTED — Codex plan consensus v5 APPROVED (2026-08-15).
Implementation may begin, per the sequencing in Decision 6.
Author: Claude (architect)
Date: 2026-08-15
Scope owner: Ryan

Consensus history: v1 → REJECTED (6 points — release-boundary
ambiguity, public-host stance not yet a decision, static-badge
weakness, no publication gate, no acceptance criteria, no concrete
sequencing). v2 → REJECTED (6 points — internal contradictions the v1
fixes introduced: Scope section still asserted a specific tag SHA while
the revision said it was a user decision; tag sequencing implicitly
gated on Week 8 presentation work despite the tag meaning only the
application snapshot; publication gate and coverage mechanism both
still underspecified; tag mutability policy self-contradictory). v3 →
REJECTED (3 points — leftover stale open-questions list contradicting
its own resolutions; coverage mechanism still forked between two tools
and two delivery paths; tag policy still self-contradictory in a
narrower way). v4 → REJECTED (2 points — GitHub Actions triggers are
workflow-level, not job-level, and the plan described job-level
triggers; the badge-liveness verification criterion would have forced
an artificial coverage-changing commit against a functionally-frozen
Phase 1). v5 → **APPROVED, no new findings**. All findings across all
five rounds were verified/resolved before acceptance — see "Consensus
Revisions" at the bottom for the finding-by-finding record.

This is an **implementation brief**, not a full ADR. Week 8 is
presentation/infrastructure polish on top of a functionally-frozen
Phase 1 (see Non-scope) — one exception, Decision 0 below, is a real
(small) application-code fix that must land first, because it decides
which commit `v0.1.0` actually points at.

---

## 0. Context / where Phase 1 stands

Phase 1 ("Kernel") is complete as of Week 7 (commit `2f82276`, then
`87dc9df`'s HANDOFF.md follow-up at `87dc9df`... see git log for the
exact current HEAD at implementation time). All five business modules
are implemented, Codex-architecture-reviewed, and Codex-diff-reviewed;
Week 7 added an O2C E2E test, a Hypothesis property test proving
trial-balance/tie-out/on-hand invariants under generated legal
sequences, demo tooling (`seed_demo`, `demo_o2c`, `Makefile`), an
ar-aging SQL rewrite, and a README rewrite that (after four Codex
review rounds) accurately describes all of the above.

Week 7's brief listed four items as explicitly deferred, unordered,
"Non-DoD" (not this week's job, but not forgotten either): a demo GIF,
a `v0.1.0` git tag, a public demo host, and a coverage badge. This
brief is the actual plan for those four, arrived at through a real
Claude-Codex consensus discussion (not a review of already-written
code, since none of this had been implemented yet when the discussion
started).

### Verified current-state facts (checked against the repo, not assumed)

- No git remote configured (`git remote -v` empty, `git tag -l` empty)
  — all six-plus weeks of work are local-only commits.
- No Docker in this dev environment — `make up`/`make seed`/`make demo`
  (the real docker-compose path) has never been exercised end to end
  here; only the underlying scripts have, via a manual embedded-
  `pgserver` workaround (see HANDOFF.md).
- No terminal-recording tooling in this sandbox (checked: `vhs`,
  `asciinema`, `agg`, `ttygif`, `ffmpeg`, `terminalizer` — all absent).
- No coverage tooling (`pytest-cov` or similar) in `pyproject.toml`.
- Phase 1 has no auth/RBAC (documented Non-Goal, Phase 2 scope):
  multi-company data isolation is real and well-tested, but any caller
  who knows/guesses a company id can read/write that company's data via
  a bare `X-Company-Id` header — nothing verifies the caller is actually
  entitled to act as that company.
- README's own Week 7 audit surfaced a real gap: `Product.list_price`/
  `standard_cost` and `SalesOrderLineCreate.unit_price` had `ge=0` but
  no round-half-even quantization, unlike every money field
  `ledger.schemas`/`receivables.schemas` own — a real (if minor)
  correctness gap in the code the tag is about to mark as "v0.1.0".

## 1. Goal & definition of done

Land, in order: the rounding fix (Decision 0); a real GitHub remote
with CI observed green for the first time ever (Decision 1); coverage
tooling wired to a live, non-static badge (Decision 2); an explicit,
documented deferral of the public demo host (Decision 3); a verified
demo walkthrough script handed off for user-side GIF recording
(Decision 4); an annotated `v0.1.0` tag, created once its target SHA is
settled and pushed once the remote exists (Decision 5).

Non-DoD (still out of scope after this week too): the public demo host
itself (formally deferred, see Decision 3), the actual embedded GIF in
README (recording is user-side, see Decision 4), any Phase 2 feature
work.

## 2. Decisions

### Decision 0 — Rounding fix (the one application-code change)

`Customer.credit_limit`, `Product.list_price`/`standard_cost`
(`masterdata.schemas`), and `SalesOrderLineCreate.unit_price`
(`sales.schemas`) get the same round-half-even-to-NUMERIC(20,6)
quantization `ledger.schemas`/`receivables.schemas` already apply to
every money field they own. Each module gets its own local, private
`_MONEY_QUANTUM`/`_round_half_even_6dp` — never imported cross-module
(masterdata/sales must never import ledger/receivables, per the
import-linter module-independence contract) — matching the existing
precedent that every business module owns its own copy rather than
sharing one from `app/core/`. Unlike `receivables.schemas`'s
`_round_and_reject_zero`, these fields legitimately allow zero
(`credit_limit == 0` means "no limit"; price fields default to `0`) —
quantize only, never reject zero.

This must land, and be the settled state, **before** Decision 5 (the
tag) — it decides which SHA `v0.1.0` actually points at (the user chose
"fix it first" over "tag current HEAD as-is").

### Decision 1 — GitHub remote + CI verification

The user creates the repository themselves (via VS Code's "Publish to
GitHub" or equivalent), as a **private** repo initially. Before any
push: confirm the account/org and visibility with the user (done — see
Consensus Revisions), scan full history for secrets/credentials
(`git log --all -p | grep -iE '...'` pattern list + a tracked-filename
sweep against known-sensitive patterns — expected to find nothing,
since `.env` was always gitignored and no history rewrite has ever
happened, but run and shown, not assumed), confirm `LICENSE` and
public-facing metadata are accurate. Then: push, and confirm
`.github/workflows/ci.yml`'s Actions run actually goes green against
the real remote — this has never been observed in this project's
history; only local-equivalent `make check` runs have.

### Decision 2 — Coverage badge (CI-backed only)

One workflow file, `.github/workflows/coverage.yml`:

```yaml
on:
  pull_request: {}
  push:
    branches: [main]
    paths-ignore: ['badges/**']
```

Two jobs, each gated by a job-level `if`:
- `coverage-check` (`if: github.event_name == 'pull_request'`): `uv run
  pytest --cov=app --cov-report=term` — informational only, default
  (read-only) permissions, works unmodified on fork PRs.
- `coverage-badge` (`if: github.event_name == 'push'`): re-runs
  coverage, `coverage-badge -o badges/coverage.svg -f` regenerates the
  badge at the persistent, repo-committed path `badges/coverage.svg`
  (never an ephemeral Actions artifact — README must link something
  that doesn't expire/require auth), then a bot commit
  (`github-actions[bot]`) pushes it back to `main` directly. Needs
  `permissions: { contents: write }` on this job only. The `push`
  trigger's `paths-ignore: ['badges/**']` (shown above) is the sole
  anti-recursion mechanism — the bot's own commit never retriggers the
  workflow.

Tool is `coverage-badge` (a single, fully-specified choice, not
"genbadge or coverage-badge"). No third-party account (Codecov/
Coveralls/a Gist-writing PAT) by default — flagged as a user-swappable
alternative at implementation time, not the default. Verification: on
the one real `coverage-badge` job run, confirm the bot commit occurred
and the SVG's rendered percentage matches that same run's own measured
`pytest --cov` output (a same-run consistency check — proving the
mechanism is wired correctly needs no contrived, scope-violating
coverage-changing commits against a functionally-frozen Phase 1).
Explicitly informational, never a merge-blocking threshold — Phase 1's
gate stays ruff/mypy/lint-imports/pytest; no coverage-percentage gate
is added as a side effect of adding the badge.

### Decision 3 — Public demo host: formally deferred

Confirmed by the user. Deferred until Phase 2 ships at least a minimal
auth gate. Reconsideration criteria: (a) Phase 2 auth lands, or (b) the
user explicitly commissions a dedicated, separately Codex-reviewed
security/deployment mini-brief scoping a narrow, specific safe shape
(e.g. a single, read-only or periodically-reset demo company) —
"read-mostly or rate-limited" alone is not an implementable spec and
was never treated as a complete one. Rationale: today's Phase 1 has no
auth/RBAC by design (Phase 2 scope); a public host would be the first
time that documented absence gets exposed to real adversarial internet
traffic rather than trusted local/CI use.

### Decision 4 — Demo GIF: script/handoff only

Week 8 delivers the verified walkthrough script and exact captured
output — largely already real via README.md's "Try it" curl transcript
and the `make demo` sample output (both captured from actual command
runs in Week 7). The embedded animated GIF itself is explicitly **not**
required for Week 8 completion or the tag: this sandboxed dev
environment has no terminal-recording tooling (checked and confirmed
absent), so actual screen recording + GIF encoding + embedding is
handed off to the user, on their own machine, as an open follow-up —
not an unmet Week 8 DoD item, since the gap is environmental, not a
scope choice.

### Decision 5 — `v0.1.0` annotated tag

Tags a SHA representing Phase 1's *application code* — never asserted
by Claude or Codex; the user confirmed (see Consensus Revisions) it
should be the SHA that lands **after** Decision 0's rounding fix, not
current-HEAD-as-is. Tag *creation* is decoupled from every other
decision in this brief (can happen as soon as that SHA is confirmed
real and committed); tag *push* naturally waits only until the remote
exists (Decision 1). Acceptance: `git tag -a v0.1.0 -m "<message
describing Phase 1 scope>" <confirmed-sha>`; verified locally via `git
show v0.1.0` before push; pushed via `git push origin v0.1.0`;
presence on the remote confirmed via `git ls-remote --tags origin`.

**Tag mutability policy**: a local, not-yet-pushed tag may be freely
deleted and recreated (nothing external depends on it). Once pushed,
`v0.1.0` is permanent — never deleted, re-pointed, or force-pushed
over. A mistake discovered after push is corrected by releasing
`v0.1.1`, never by moving `v0.1.0`.

### Decision 6 — Sequencing

1. Decision 0 (rounding fix) — lands first, its own Codex diff review,
   independent of everything else below.
2. Decision 5's tag *creation* (local only) — can happen any time after
   step 1's SHA is real, does not wait on steps 3–6.
3. Decision 1's publication readiness checks, then user authorization,
   then remote creation + push + CI verification.
4. Decision 2's coverage integration (PR job) + confirm a real number
   in a real PR's checks.
5. Decision 2's badge (push-to-main job) + confirm live per its
   same-run verification criterion.
6. Decision 4's GIF script finalized/handed off (does not block
   anything).
7. Decision 5's tag *push* (waits only on step 3's remote existing, not
   on steps 4–6).

## 3. Scope / non-scope

**Scope**: Decisions 0–5 above.

**Non-scope**: any Phase 2 ("Platform") feature work — plugin loader,
custom-fields admin UI, workflow/approval engine, real auth/RBAC; any
further change to Phase 1's actual application behavior beyond
Decision 0's narrow rounding fix; the public demo host itself
(Decision 3); the actual embedded GIF file (Decision 4).

## 4. Validation

Decision 0: full `make check`-equivalent (ruff/format/mypy/lint-
imports/pytest) plus dedicated round-half-even test cases per touched
field (a value exactly halfway between two NUMERIC(20,6) values, chosen
so a passing test distinguishes round-half-even from naive
truncation — not just "value survives a round trip"). Real Codex diff
review before commit, per this project's standing workflow.

Decisions 1–5: acceptance criteria are stated per-decision above; no
separate validation section needed beyond those.

## 5. Risks

- **Public demo host** (addressed by deferring it — Decision 3): the
  highest-risk item in the original four-item list, by far.
- **First-time going public with a GitHub remote**: irreversible-in-
  spirit (history becomes visible) — mitigated by the publication gate
  in Decision 1.
- **A coverage badge that lies**: a static, never-updated badge would
  misrepresent a live signal — avoided entirely by Decision 2's design
  (CI-backed only, no static fallback ever considered viable).
- **Scope creep into Phase 2** while "in the neighborhood" of hosting/
  infra work — explicitly excluded in Non-scope.

## 6. Open decisions — resolved with the user (not Claude's or Codex's to make)

- GitHub account/org + visibility: user creates it themselves via VS
  Code, private initially.
- `v0.1.0` target SHA: after Decision 0's rounding fix lands, not
  current HEAD as-is.
- Public host deferral: user confirmed agreement.
- Week 8 scope cut (this brief's Decisions 0–5, host deferred): user
  confirmed agreement.
- This brief's disposition: formalized under `docs/adr/` (this file),
  mirroring the Week 7 brief, rather than left as a root-level scratch
  `PLAN.md`.

## 7. Consensus Revisions (v1 → v5)

**v1 → v2** (6 REJECTED points, all fixed): (1) release boundary for
`v0.1.0` was undefined — fixed by making the SHA an explicit user
decision, never asserted. (2) tag sequencing implicitly conflicted with
its own meaning (a Phase-1-only tag shouldn't wait on Week 8
presentation work) — fixed by decoupling tag creation from badge/GIF
completion. (3) coverage badge mechanism was a bare placeholder ("a
gist or equivalent") — fixed with one concrete, self-contained default
(CI-generated SVG committed back to the repo, no third-party account).
(4) no concrete pre-push publication gate — added one. (5) no
acceptance criteria per deliverable — added throughout. (6) sequencing
was unordered — adopted Codex's proposed order.

**v2 → v3** (6 REJECTED points, all fixed): (1)/(2) the v2 fixes
introduced a NEW contradiction — Scope still asserted a specific SHA
(`87dc9df`) as fact while the revision said it was a user decision, and
tag sequencing still implicitly gated on Week 8 presentation-artifact
completion despite the stated intent to decouple them; fixed together
by making Scope say "TBD, user decision" and by explicitly splitting
tag *creation* (early, decoupled) from tag *push* (gated only by the
remote). (3) the publication gate was still abstract — added actual
`grep`/`git log` commands, an explicit "unsuitable file" definition,
and explicit repo-creation/default-branch/remote-verification steps.
(4) the coverage mechanism still forked between two tools and two
delivery paths ("genbadge or coverage-badge", "bot commit or
artifact") and self-contradicted on trigger conditions — narrowed
toward one path (fully resolved in v4/v5). (5) GIF acceptance was
ambiguous (embedded GIF vs. script-only) — resolved: script/handoff
only, explicitly not blocking. (6) tag acceptance criteria were
incomplete — added the full command sequence.

**v3 → v4** (3 REJECTED points, all fixed): (1) a stale "Open questions
for Codex" list from v1 still asked questions v2/v3 had already
resolved, creating an appearance of unresolved contradiction — removed
entirely, replaced with a pointer to the resolved sections. (2) the
coverage mechanism was STILL forked (tool choice, commit-vs-artifact
delivery) and self-contradicted ("push to default branch" vs. "changes
on a subsequent PR") — collapsed to exactly one tool
(`coverage-badge`) and one delivery path (persistent committed SVG, not
an artifact), with the PR/push split becoming two jobs to resolve the
trigger contradiction (refined further in v5 — see below, this was
still imprecise about where GitHub Actions triggers actually live).
(3) tag mutability policy self-contradicted ("never re-pointed" vs.
"deleted and recreated") — fixed: local tags may be freely
recreated pre-push; pushed tags are permanent, corrected only by a new
version, never moved.

**v4 → v5** (2 REJECTED points, both fixed, then **APPROVED**): (1)
GitHub Actions triggers are workflow-level, not job-level, and v3's
"two jobs, two triggers" phrasing was imprecise about that — fixed by
specifying exactly one workflow file with `on: { pull_request, push }`
at the top level and job-level `if: github.event_name == ...`
conditions inside; anti-recursion also collapsed from an "or" between
two options down to exactly one (`paths-ignore`, not `[skip ci]`
commit-message parsing). (2) the badge-liveness verification criterion
("diff the SVG across two differently-covered commits") would have
required manufacturing an artificial coverage-changing commit, which
conflicts with Phase 1 being functionally frozen — replaced with a
same-run consistency check (bot commit occurred; SVG percentage matches
that run's own measured coverage), which needs no contrived commits.
v5: **APPROVED — no new findings**, workflow-level triggers, job-level
conditions, single anti-recursion mechanism, and the same-run badge
consistency check confirmed implementable, scoped, and internally
consistent.

All findings across all five rounds were verified against actual
project state (git remote/tag absence, tooling availability, existing
schema/validator patterns, GitHub Actions trigger semantics) before
being accepted as real — none were taken as fact without that check,
matching this project's standing Codex-review discipline.
