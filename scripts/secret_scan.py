"""Zero-dependency deterministic secret scanner for mini-ERP (ADR-018).

Scans tracked repository files (`git ls-files`) and optionally full Git commit
history (`--git-history`) for accidental secret commits, private keys, cloud/SaaS
credentials, webhook HMAC secrets, and hardcoded environment secrets.

Usage:
    python scripts/secret_scan.py
    python scripts/secret_scan.py --git-history
    python scripts/secret_scan.py --repo /path/to/repo
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ALLOW_MARKER = "secret-scan: allow-test-fixture"

# Exact SHA-256 digests of known, previously-reviewed benign placeholders that
# appear in historical commits prior to D.5 u13 / ADR-018 remediation.
# Only honoured during `--git-history` traversal of past commits; active working
# tree / HEAD files must never contain raw secret literals without test markers.
_HISTORICAL_ALLOWLIST_SHA256: frozenset[str] = frozenset(
    {
        # Historical test fixture placeholder in Week 13 integration tests (replaced by generator)
        "d5a0b01bf3a85b77cd96c2da667966e42b7c204262a05172d465e26b93a20354",
        # Pre-D.5-u13 quickstart placeholder text in README.md (remediated to hidden prompt)
        "c2ef8a252a86efe72eb755af3e01eb0287c05428d39fc566e5ff8130a3b5560a",
        # Pre-D.5-u13 quickstart placeholder text in README.zh-TW.md (remediated to hidden prompt)
        "3283699d07866ff84a4bba1308fcd5a6d846081b3dba88fd31851410b0bb0100",
    }
)

_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".pdf",
        ".woff",
        ".woff2",
        ".pyc",
        ".whl",
        ".zip",
        ".gz",
    }
)

_SKIP_FILES: frozenset[str] = frozenset({"uv.lock"})

_PATTERN_WHSEC = re.compile(r"whsec_[0-9a-fA-F]{32,}")
_PATTERN_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP |PRIVATE )?PRIVATE KEY-----"
)
_PATTERN_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_PATTERN_GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"
)
_PATTERN_GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")
_PATTERN_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")
_PATTERN_HARDCODED_ENV = re.compile(
    r"^\s*(?:export\s+)?(MINI_ERP_SECRET_ENCRYPTION_KEYS|SECRET_ENCRYPTION_KEYS|"
    r"BOOTSTRAP_ADMIN_PASSWORD|DEMO_ADMIN_PASSWORD)\s*=\s*(.+?)\s*$"
)
_PATTERN_FERNET_ASSIGN = re.compile(
    r"""(?ix)
    (?:encryption_key|fernet_key|secret_encryption_keys)\s*[:=]\s*
    ["']([A-Za-z0-9_\-]{43}=)["']
    """
)


@dataclass(frozen=True, slots=True)
class SecretFinding:
    rule_id: str
    path: str
    line_no: int
    source: str
    preview: str


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_preview(matched: str) -> str:
    if len(matched) <= 10:
        return "[REDACTED]"
    return f"{matched[:8]}...[REDACTED]"


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _is_benign_env_rhs(rhs: str) -> bool:
    """Ignore empty values, shell variable expansions ($VAR / $$VAR), and template hints."""
    cleaned = rhs.strip()
    if cleaned.startswith("#") or not cleaned:
        return True
    # Strip trailing inline comment if unquoted
    if not cleaned.startswith(("'", '"')) and " #" in cleaned:
        cleaned = cleaned.split(" #", 1)[0].strip()
    unquoted = cleaned.strip("\"'").strip()
    if not unquoted:
        return True
    if unquoted.startswith("$"):
        return True
    if unquoted.startswith("<") and unquoted.endswith(">"):
        return True
    return unquoted in {"...", "[]"}


def _is_test_fixture_allowed(path: str, line: str) -> bool:
    normalized = path.replace("\\", "/")
    is_test_path = normalized == "tests" or normalized.startswith("tests/")
    return is_test_path and _ALLOW_MARKER in line


def scan_line(
    line: str,
    *,
    path: str,
    line_no: int,
    source: str,
    allow_historical_hashes: bool = False,
) -> list[SecretFinding]:
    """Evaluate all secret detection rules against one line of text."""
    if _is_test_fixture_allowed(path, line):
        return []

    findings: list[SecretFinding] = []
    simple_rules: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("WHSEC_SECRET", _PATTERN_WHSEC),
        ("PRIVATE_KEY", _PATTERN_PRIVATE_KEY),
        ("AWS_ACCESS_KEY", _PATTERN_AWS_KEY),
        ("GITHUB_TOKEN", _PATTERN_GITHUB_TOKEN),
        ("GOOGLE_API_KEY", _PATTERN_GOOGLE_API_KEY),
        ("SLACK_TOKEN", _PATTERN_SLACK_TOKEN),
    )
    for rule_id, pattern in simple_rules:
        for match in pattern.finditer(line):
            matched_val = match.group(0)
            if allow_historical_hashes and _sha256_hex(matched_val) in _HISTORICAL_ALLOWLIST_SHA256:
                continue
            findings.append(
                SecretFinding(
                    rule_id=rule_id,
                    path=path,
                    line_no=line_no,
                    source=source,
                    preview=_redact_preview(matched_val),
                )
            )

    env_flagged = False
    env_match = _PATTERN_HARDCODED_ENV.match(line)
    if env_match is not None:
        full_assignment = env_match.group(0).strip()
        rhs = env_match.group(2)
        if not _is_benign_env_rhs(rhs) and not (
            allow_historical_hashes and _sha256_hex(full_assignment) in _HISTORICAL_ALLOWLIST_SHA256
        ):
            env_flagged = True
            findings.append(
                SecretFinding(
                    rule_id="HARDCODED_SECRET_ENV",
                    path=path,
                    line_no=line_no,
                    source=source,
                    preview=f"{env_match.group(1)}=[REDACTED]",
                )
            )

    if not env_flagged:
        for fernet_match in _PATTERN_FERNET_ASSIGN.finditer(line):
            candidate = fernet_match.group(1)
            if _shannon_entropy(candidate) >= 3.5 and not (
                allow_historical_hashes and _sha256_hex(candidate) in _HISTORICAL_ALLOWLIST_SHA256
            ):
                findings.append(
                    SecretFinding(
                        rule_id="INLINE_FERNET_KEY",
                        path=path,
                        line_no=line_no,
                        source=source,
                        preview=_redact_preview(candidate),
                    )
                )

    return findings


def scan_content(
    content: str,
    *,
    path: str,
    source: str = "working-tree",
    allow_historical_hashes: bool = False,
) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        findings.extend(
            scan_line(
                line,
                path=path,
                line_no=idx,
                source=source,
                allow_historical_hashes=allow_historical_hashes,
            )
        )
    return findings


def _git_tracked_files(repo: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "-z"],
        text=False,
    )
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]


def scan_repository(repo: Path) -> list[SecretFinding]:
    """Scan all git-tracked files in `repo`."""
    findings: list[SecretFinding] = []
    for rel_path in _git_tracked_files(repo):
        if rel_path in _SKIP_FILES:
            continue
        file_path = repo / rel_path
        if not file_path.is_file() or file_path.suffix.lower() in _SKIP_EXTENSIONS:
            continue
        raw = file_path.read_bytes()
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.extend(scan_content(text, path=rel_path, source="tracked-file"))
    return findings


def scan_git_history(repo: Path) -> list[SecretFinding]:
    """Scan added lines across full Git commit history (`git log -p --all`)."""
    findings: list[SecretFinding] = []
    output = subprocess.check_output(
        ["git", "-C", str(repo), "log", "-p", "--all"],
        text=False,
    ).decode("utf-8", errors="replace")

    current_commit = "unknown"
    current_file = "unknown"
    new_line_no = 0
    hunk_header = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in output.splitlines():
        if line.startswith("commit "):
            current_commit = line.split()[1][:12]
            continue
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith("+++ /dev/null"):
            current_file = "/dev/null"
            continue
        hunk = hunk_header.match(line)
        if hunk is not None:
            new_line_no = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if (
                current_file not in _SKIP_FILES
                and current_file != "/dev/null"
                and Path(current_file).suffix.lower() not in _SKIP_EXTENSIONS
            ):
                findings.extend(
                    scan_line(
                        line[1:],
                        path=current_file,
                        line_no=new_line_no,
                        source=f"git-history:{current_commit}",
                        allow_historical_hashes=True,
                    )
                )
            new_line_no += 1
        elif not line.startswith("-"):
            new_line_no += 1

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="secret_scan",
        description="Scan tracked files (and optionally Git history) for leaked secrets.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the repository root (defaults to project root).",
    )
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="Also scan added lines across all commits in Git history.",
    )
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    findings = scan_repository(repo)
    if args.git_history:
        findings.extend(scan_git_history(repo))

    if findings:
        print(f"SECRET SCAN FAILED: {len(findings)} potential secret(s) detected:", file=sys.stderr)
        for item in findings:
            print(
                f"  [{item.rule_id}] {item.path}:{item.line_no} ({item.source}) -> {item.preview}",
                file=sys.stderr,
            )
        return 1

    scope = "tracked files + Git history" if args.git_history else "tracked files"
    print(f"Secret scan passed ({scope}): 0 findings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
