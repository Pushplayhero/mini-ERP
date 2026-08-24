# mini-ERP Security Scan Policy

Status: Slice D.5 baseline policy, 2026-08-24.

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
