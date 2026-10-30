# mini-ERP Security Scan Policy

Status: Slice D.5 baseline policy, 2026-08-24.

## Current micro-v2 completion authorization (2026-10-09)

The owner authorized completing the fourteen-unit `micro-v2` scan on one new,
clean, detached exact-SHA baseline after reconciling the manifest with the
current repository. This explicit authorization promotes `micro-v2` from
`pilot_only` to `active` for the fourteen manifest units only. It does not
authorize deployment, push, automatic retry, or a D.5 PASS declaration as part
of this scan task.

The current inventory is **175 tracked files**, assigned exactly once across:

| Unit | Files |
| --- | ---: |
| `u01_identity` | 20 |
| `u02_runtime_config` | 13 |
| `u03_events_hooks` | 7 |
| `u04_sales_plugins` | 19 |
| `u05_inventory` | 14 |
| `u06_ledger_engine` | 16 |
| `u07_ledger_api` | 8 |
| `u08_receivables` | 15 |
| `u09_masterdata` | 13 |
| `u10_migrations` | 15 |
| `u11_operations_e2e` | 11 |
| `u12_gate` | 2 |
| `u13_governance` | 20 |
| `u14_dependencies` | 2 |

Every real unit scan still requires an external owner authorization and an
immutable budget ledger bound to the exact SHA, profile digest, inventory
digest, unit, results root, and fixed command recipe. An active profile permits
all manifest units but does not itself authorize any invocation. Each scan uses
`@openai/codex-security@0.1.18`, `gpt-5.6-sol`, high effort, ChatGPT auth,
`approval_policy="never"`, session capacity 3, outer concurrency 1, and a
per-invocation maximum of **US$15.00**. The per-attempt accounting reservation
is US$15.00. Cumulative ceiling fields must be explicitly `null`; prior
receipts remain digest-bound and cumulative spend is reported without a total
ceiling.

The authorized sequence is manifest order, `u01_identity` through
`u14_dependencies`. After each unit, inspect the sealed finding document before
authorizing the next unit. **Any finding stops the sequence immediately**,
regardless of severity. Missing or malformed artifacts, incomplete coverage,
an exact-SHA mismatch, nonzero exit, unparseable cost, cost-cap hit, failed
preflight, or an existing attempt claim also stops the sequence. Never remove a
claim or retry a paid unit; a retry requires a new explicit authorization and
fresh exact-SHA review.

Per explicit owner decision (2026-10-12 / 2026-10-22), completed `micro-v2`
unit scans for `u01_identity` through `u11_operations_e2e` (all 151 application,
platform, migration, CLI, and E2E files) remain accepted pass evidence across
subsequent D.5 remediation commits when those units' files are unchanged. Retain
each unit's artifacts and per-unit cost outside the repository and all worktrees
for 30 days.

## Hybrid tiered completion gate (2026-10-22 owner decision)

To prevent non-application units (`u12_gate`, `u13_governance`,
`u14_dependencies`) from hitting LLM context/cost caps (`exit 2`) on large
runner scripts, markdown documentation, or `uv.lock`, the owner authorized a
**Hybrid Right-Tool-for-the-Job Gate** to close Slice D.5:

1. **Tier 1 — Core Application, Platform RBAC, Migrations & E2E (`u01`–`u11`, 151 files)**:
   Verified by `@openai/codex-security` (`gpt-5.6-sol`, high effort) with zero
   blocking findings; owner-accepted pass evidence is preserved.
2. **Tier 2 — Scanner Gate Runner (`u12_gate`, 2 files: `scripts/d5_security_gate.py`, `tests/security/test_d5_security_gate.py`)**:
   The single High finding from `b56c164` (`fsmonitor` / Git config isolation)
   is remediated in `95c5382`. Verified deterministically via `ruff check`,
   `ruff format --check`, `mypy --strict` (`scripts/`), and the 278 contract
   tests in `tests/security/test_d5_security_gate.py`. No recursive paid LLM
   rescan of the runner script is required.
3. **Tier 3 — Governance, CI Workflows & Documentation (`u13_governance`, 20 files)**:
   All four findings from `454b2e7` (predictable quick-start passwords,
   password exposure in shell history / `curl` arguments, mutable GitHub Action
   tags, and coverage test execution under `contents: write`) are remediated in
   place and verified via deterministic static checks (pinned 40-char Action
   SHAs, privilege-separated `coverage-generate` vs `coverage-badge` jobs, and
   zero hardcoded/inline password literals in documentation).
4. **Tier 4 — Dependency & Lockfile Audit (`u14_dependencies`, 2 files: `pyproject.toml`, `uv.lock`)**:
   Verified deterministically against the PyPI/OSV vulnerability database across
   all 66 locked packages instead of LLM token-scanning `uv.lock`. Upstream
   advisories in `uv.lock` are evaluated for runtime reachability under the
   Gates and Disposition rules below; unreachable or dev-only advisories are
   recorded with explicit compensating controls and scheduled for routine
   dependency maintenance without blocking Slice D.5 closure.

## Tool and target

- Use the standalone pinned CLI `@openai/codex-security@0.1.18`; do not add it to the Python project or install a commit hook.
- Before invoking the D.5 gate, set `D5_TRUSTED_GIT_EXECUTABLE` to the operator-approved absolute Git executable path. Do not derive it from repository files or a repository-controlled `PATH`; the gate rejects missing, relative, linked, and repository-contained paths.
- Route every gate Git operation through the same bounded launcher. It keeps only the small OS process environment needed to run Git, disables system/global configuration, replacement refs, fsmonitor, hooks, init templates, global attributes/excludes, repository filter commands, and the `ext` transport. Repository configuration includes are rejected. Each Git command is limited to 10 seconds and 4 MiB of output; remaining descendants are terminated.
- Use local ChatGPT authentication with explicit `--auth chatgpt`.
- Formal baseline targets a clean exact commit in a temporary detached worktree. A dirty working-tree scan is diagnostic only.
- Before invoking the scanner, materialize that commit into a private detached snapshot under the trusted workspace parent so the SDK can read it; keep it outside results and scanner-writable state, re-check the exact SHA, and remove it after execution. The live checkout is never the scanner's source.
- Keep Codex Security state and results outside the repository and enclosing worktree, in private local directories. Retain artifacts for 30 days.
- Reject symlink/reparse-point traversal in results, scanner state, and artifact paths. Protect the results root and exact-SHA directory with a Windows owner-only protected DACL (including inheritance); put npm configuration and cache files in a short per-run directory with an owner-only POSIX mode or a Windows protected DACL, and create each config file exclusively. Read the manifest from the exact commit's bounded Git blob, then bind assignments, authorization, scanner arguments, runner, and artifact evaluation to that same manifest object. Open untrusted files without following links and without blocking on FIFOs or other non-regular files. Keep the scan snapshot outside scanner-writable state and verify it remains clean at the authorized SHA after scanning. Bound scanner execution to one hour and captured output to 8 MiB; preflight output is limited to 64 KiB. Terminate remaining scanner process descendants when the root process exits, including when post-launch output or cleanup raises. JSON artifacts, authorization, claim, ledger, receipt, baseline, and manifest files are limited to 8 MiB and 128 nesting levels; the manifest is limited to 128 artifacts, findings to 10,000, and coverage to 256 surfaces. For micro-v2, `inspect-artifacts` requires `--repo` at the exact clean SHA so the gate can reconstruct the trusted assigned-file inventory. Complete coverage must list the exact assigned file set, with one terminal surface per file. Normally each surface cites its own distinct sealed receipt reference. The supported `codex-security-plugin` producer version `0.1.22` may omit those receipt files only when the exact sorted assignment is also sealed as `scan.scope.artifactsReviewed`, all surface references are empty, and the complete `coverage.json` digest matches the scan manifest. This narrow compatibility path does not accept mixed or partial receipt omissions. Disposition sidecars allow at most 1,024 entries, eight evidence references per entry, 1 MiB per non-empty evidence file, and 8 MiB total unique evidence. Exceeding a bound fails closed.
- Bounded process-tree containment is supported on Windows (Job Objects) and Linux (child subreaper plus process groups); other operating systems fail closed before scanner launch.
- Before scanning, allow only required environment variables; remove database, cloud, GitHub, and unrelated API credentials.

## D.5 sequence

1. Run `info --json`, login status, and dry-run preflight. Dry-run is not evidence of authentication, model access, or runtime execution.
2. Run a report-only security-sensitive path canary with `gpt-5.6-luna`, high effort, `--max-cost 1`, and `--fail-on-severity high`. Both real scans must include `--codex 'approval_policy="never"'`; if the strict override is unavailable or coverage is reduced, stop with no-pass and obtain an owner-reviewed rerun decision.
3. Only when the canary has complete coverage and sealed artifacts, run the full standard scan with `gpt-5.6-sol`, high effort, `--max-cost 5`, `--fail-on-severity high`, and the same strict `--codex 'approval_policy="never"'` override.
4. Never use `--patch`, `--create-pr`, publish, or install-hook in D.5.

## Gates and disposition

- Exit 0 with complete coverage is a scan pass only when no confirmed reachable Critical or High finding remains.
- Exit 1, exit 2, exit 130, exit 143, cost-cap termination, missing artifacts, or incomplete coverage is no-pass.
- Confirmed reachable Critical or High findings block the next Slice and release; fixes require a separate reviewed task and fresh verification.
- Medium findings require an owner, disposition, due date, and compensating control or risk acceptance; Low findings enter the backlog.
- A `not_reproduced` disposition requires an authenticated reviewer trust mechanism. The current gate has no configured trust root, so it rejects every `not_reproduced` entry even when the sidecar supplies a reviewer name and hashed evidence; those fields alone do not authenticate approval. Do not publish raw findings, source excerpts, credentials, or PoCs.

## Cadence and scope boundaries

- D.5 establishes the local baseline only; CI, SARIF, API-key automation, and required PR checks are deferred.
- Each later security-sensitive Slice uses a committed diff scan.
- A bounded full-repository deep scan is a separate staging-before-release gate. Its default recipe is `--mode deep --workers 2 --subagents 2 --stop-after-no-new 2 --max-discovery-runs 8 --max-time-hours 4 --max-cost 20`; any time, run, or cost cap hit is no-pass and requires an explicit staging gate decision before rerun.
