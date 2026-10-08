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
per-invocation maximum of **US$11.75**. The per-attempt accounting reservation
remains US$13.00. Cumulative ceiling fields must be explicitly `null`; prior
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

Prior pilot scans and ordinary scoped reports remain immutable historical
evidence. Because they target different SHAs, inventories, or artifact
contracts, they do not cover units on the new baseline and are not included in
its artifact union. The legacy five-shard profile remains documented below but
is not used for this authorized micro-v2 sequence. Retain each new unit's
artifacts and per-unit cost outside the repository and all worktrees for 30 days.

## Tool and target

- Use the standalone pinned CLI `@openai/codex-security@0.1.18`; do not add it to the Python project or install a commit hook.
- Use local ChatGPT authentication with explicit `--auth chatgpt`.
- Formal baseline targets a clean exact commit in a temporary detached worktree. A dirty working-tree scan is diagnostic only.
- Keep Codex Security state and results outside the repository and enclosing worktree, in private local directories. Retain artifacts for 30 days.
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
- False positives require an occurrence ID, evidence, and reviewer. Do not publish raw findings, source excerpts, credentials, or PoCs.

## Cadence and scope boundaries

- D.5 establishes the local baseline only; CI, SARIF, API-key automation, and required PR checks are deferred.
- Each later security-sensitive Slice uses a committed diff scan.
- A bounded full-repository deep scan is a separate staging-before-release gate. Its default recipe is `--mode deep --workers 2 --subagents 2 --stop-after-no-new 2 --max-discovery-runs 8 --max-time-hours 4 --max-cost 20`; any time, run, or cost cap hit is no-pass and requires an explicit staging gate decision before rerun.
