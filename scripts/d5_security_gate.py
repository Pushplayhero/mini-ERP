"""Fail-closed runner for the bounded D.5 security shard scan."""

# The contract validator is intentionally explicit and branch-heavy; retain
# the policy vocabulary in readable guards rather than compressing it.
# ruff: noqa: E501, E702, F811, N818, PLR0911, PLR0915, RUF100

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "docs" / "security" / "d5-shards.json"
SHARD_IDS = (
    "a_identity_runtime",
    "b_sales_inventory_plugins",
    "c_ledger_receivables",
    "d_masterdata_ops_migrations",
    "e_docs_governance",
)
LEGACY_PROFILE_ID = "legacy-v1"
MICRO_PROFILE_ID = "micro-v2"
MAX_COST_USD = 15.0
LEGACY_MAX_COST_USD = 11.75
TOTAL_MAX_COST_USD = 58.75
MAX_JSON_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_JSON_NESTING = 128
LEGACY_MICRO_PROFILE_DIGESTS = {"e48a10dbaa82d03fd8465244b47bcf7ef8bb3565a8443748c5afe7d8bd80eb86"}
COST_QUANTUM = Decimal("0.000001")
RETENTION_DAYS = 30
_WINDOWS_DIRECTORY_LOCKS: dict[str, list[int]] = {}
_WINDOWS_DIRECTORY_LOCKS_GUARD = threading.Lock()
BASELINE_FILENAME = "baseline.json"
DRY_RUN_ROOT = Path.home() / ".codex" / "security-results" / "d5-dry-runs"
COMMAND_PROVENANCE = {
    "package": "@openai/codex-security@0.1.18",
    "auth": "chatgpt",
    "model": "gpt-5.6-sol",
    "effort": "high",
    "approval_policy": "never",
    "dry_run": False,
}
COMMON_KNOWLEDGE_BASE = [
    "SECURITY.md",
    "docs/security/threat-model.md",
    "docs/security/scan-policy.md",
]


class GateError(RuntimeError):
    """A configuration or evidence error that must fail closed."""


class BoundedScannerError(GateError):
    def __init__(self, reason: str, output: str) -> None:
        super().__init__(reason)
        self.output = output


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_JSON_ARTIFACT_BYTES:
            raise ValueError("manifest exceeds its configured size limit")
        value = _parse_bounded_json(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GateError(f"cannot read manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise GateError("manifest schema_version must be 1.0")
    return value


def _parse_bounded_json(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    text = raw.decode("utf-8")
    nesting = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            nesting += 1
            if nesting > MAX_JSON_NESTING:
                raise ValueError("JSON nesting exceeds its configured limit")
        elif character in "]}":
            nesting -= 1
    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _shards(manifest: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    data = manifest or load_manifest()
    result: dict[str, dict[str, Any]] = {}
    raw_shards = data.get("shards")
    if not isinstance(raw_shards, list):
        raise GateError("manifest shards must be a list")
    for shard in raw_shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("id"), str):
            raise GateError("manifest shard is malformed")
        if shard["id"] in result:
            raise GateError(f"duplicate shard id: {shard['id']}")
        result[shard["id"]] = shard
    return result


SHARDS = _shards()


def _profile_config(profile: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    data = manifest or load_manifest()
    if profile == LEGACY_PROFILE_ID:
        return {
            "schema_version": "1.0",
            "activation": "legacy",
            "expected_total_files": data.get("expected_total_files"),
            "model": COMMAND_PROVENANCE["model"],
            "effort": COMMAND_PROVENANCE["effort"],
            "session_capacity": None,
            "outer_concurrency": 1,
            "max_cost_usd": LEGACY_MAX_COST_USD,
            "common_knowledge_base": data.get("common_knowledge_base"),
            "units": data.get("shards"),
        }
    profiles = data.get("candidate_profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        raise GateError(f"unknown profile: {profile}")
    value = profiles[profile]
    if not isinstance(value, dict) or value.get("schema_version") != "2.0":
        raise GateError("micro profile schema_version must be 2.0")
    if value.get("activation") not in ("offline_only", "pilot_only", "active"):
        raise GateError("micro profile activation is unsupported")
    units = value.get("units")
    if not isinstance(units, dict) or not units:
        raise GateError("micro profile units are required")
    return value


def _authorized_micro_units(config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    value = dict(config) if config is not None else _profile_config(MICRO_PROFILE_ID)
    activation = value.get("activation")
    if activation == "active":
        units = value.get("units")
        return tuple(units) if isinstance(units, dict) else ()
    pilot_units = value.get("pilot_units")
    if activation == "pilot_only" and isinstance(pilot_units, list):
        return tuple(pilot_units)
    return ()


def _profile_units(
    profile: str, manifest: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    config = _profile_config(profile, manifest)
    raw = config.get("units")
    if profile == LEGACY_PROFILE_ID:
        return _shards(manifest)
    if not isinstance(raw, dict):
        raise GateError("profile units are malformed")
    result: dict[str, dict[str, Any]] = {}
    for unit_id, unit in raw.items():
        if not isinstance(unit_id, str) or not isinstance(unit, dict) or unit_id in result:
            raise GateError("profile unit is malformed")
        result[unit_id] = unit
    return result


def profile_unit_ids(
    profile: str = LEGACY_PROFILE_ID, manifest: dict[str, Any] | None = None
) -> tuple[str, ...]:
    return tuple(_profile_units(profile, manifest))


MICRO_UNIT_IDS = profile_unit_ids(MICRO_PROFILE_ID)
ALL_UNIT_IDS = tuple(dict.fromkeys((*SHARD_IDS, *MICRO_UNIT_IDS)))


def profile_digest(profile: str = LEGACY_PROFILE_ID, manifest: dict[str, Any] | None = None) -> str:
    encoded = json.dumps(_profile_config(profile, manifest), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _inventory_digest(files: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(files)).encode("utf-8")).hexdigest()


def _command_provenance(profile: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if profile == LEGACY_PROFILE_ID:
        return COMMAND_PROVENANCE
    config = _profile_config(profile, manifest)
    return {
        **COMMAND_PROVENANCE,
        "session_capacity": config["session_capacity"],
        "profile": profile,
    }


def _path_has_reparse_component(path: Path) -> bool:
    candidate = Path(os.path.abspath(path))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            return True
        if getattr(current, "is_junction", lambda: False)():
            return True
    return False


def _validated_repo_input(repo: Path, relative: str, *, allow_directory: bool) -> Path:
    raw_repo = Path(os.path.abspath(repo))
    candidate = raw_repo / relative
    if (
        not relative
        or Path(relative).is_absolute()
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or _path_has_reparse_component(raw_repo)
        or _path_has_reparse_component(candidate)
    ):
        raise GateError("repository scan input cannot contain links or reparse points")
    try:
        root = raw_repo.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        metadata = resolved.stat()
    except (OSError, ValueError) as exc:
        raise GateError("repository scan input must exist beneath the repository") from exc
    if not stat.S_ISREG(metadata.st_mode) and not (
        allow_directory and stat.S_ISDIR(metadata.st_mode)
    ):
        raise GateError("repository scan input has an unsupported file type")
    return resolved


def _trusted_git_executable(repo: Path) -> Path:
    configured = os.environ.get("D5_TRUSTED_GIT_EXECUTABLE")
    if not configured:
        raise GateError("D5_TRUSTED_GIT_EXECUTABLE must name a trusted absolute Git executable")
    executable = Path(configured)
    if not executable.is_absolute():
        raise GateError("trusted Git executable path must be absolute")
    try:
        if _path_has_reparse_component(executable):
            raise GateError("trusted Git executable path cannot contain links or reparse points")
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise GateError("trusted Git executable is unavailable") from exc
    if not resolved.is_file() or resolved == repo.resolve() or repo.resolve() in resolved.parents:
        raise GateError("trusted Git executable must be a file outside the repository")
    return resolved


def _git_process(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = _trusted_git_executable(repo)
    git_env = _git_environment()
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", *args],
            cwd=repo,
            executable=str(executable),
            env=git_env,
            check=check,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateError(f"git command failed: {executable} {' '.join(args)}") from exc


def _git(repo: Path, *args: str) -> str:
    return _git_process(repo, *args).stdout.strip()


def _git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _git_blob(repo: Path, revision: str, relative_path: str) -> bytes:
    if not _SHA40.fullmatch(revision):
        raise GateError("manifest revision must be a full commit SHA")
    if relative_path != "docs/security/d5-shards.json":
        raise GateError("unsupported trusted Git object path")
    object_name = f"{revision}:{relative_path}"
    executable = _trusted_git_executable(repo)
    git_env = _git_environment()
    try:
        commit_type = subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "-t", revision],
            cwd=repo,
            executable=str(executable),
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit_type.stdout.strip() != "commit":
            raise GateError("manifest revision is not a commit object")
        size_result = subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "-s", object_name],
            cwd=repo,
            executable=str(executable),
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        size = int(size_result.stdout.strip())
        if size < 0 or size > MAX_JSON_ARTIFACT_BYTES:
            raise GateError("exact-revision manifest exceeds its configured size limit")
        result = subprocess.run(
            ["git", "--no-replace-objects", "show", object_name],
            cwd=repo,
            executable=str(executable),
            env=git_env,
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        raise GateError("cannot read exact-revision manifest from Git object") from exc
    if len(result.stdout) != size:
        raise GateError("exact-revision manifest object size changed")
    return result.stdout


def require_clean_revision(repo: Path) -> str:
    repo = repo.resolve()
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise GateError("repository must have a clean worktree")
    attached = _git_process(repo, "symbolic-ref", "--quiet", "HEAD", check=False)
    if attached.returncode == 0:
        raise GateError("repository must use a detached HEAD")
    sha = _git(repo, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateError("HEAD is not a full commit SHA")
    return sha


def _tracked_files(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "-z")
    return [item for item in raw.split("\0") if item]


def validate_manifest(
    repo: Path,
    path: Path = MANIFEST_PATH,
    *,
    profile: str = LEGACY_PROFILE_ID,
    manifest_data: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    manifest = manifest_data if manifest_data is not None else load_manifest(path)
    config = _profile_config(profile, manifest)
    tracked = _tracked_files(repo)
    tracked_set = set(tracked)
    units = _profile_units(profile, manifest)
    unit_ids = tuple(units)
    assignments: dict[str, list[str]] = {unit_id: [] for unit_id in unit_ids}
    if profile == LEGACY_PROFILE_ID:
        if set(units) != set(SHARD_IDS):
            raise GateError("manifest must contain exactly the five configured shards")
        if manifest.get("aggregate_max_cost_usd") != TOTAL_MAX_COST_USD:
            raise GateError(f"aggregate_max_cost_usd must be {TOTAL_MAX_COST_USD}")
        if manifest.get("operational_ceiling_usd") != 60.0:
            raise GateError("operational_ceiling_usd must be 60.0")
    else:
        if config.get("session_capacity") != 3 or config.get("outer_concurrency") != 1:
            raise GateError("micro profile concurrency policy is fixed")
        if config.get("max_cost_usd") != MAX_COST_USD:
            raise GateError(f"micro profile max_cost_usd must be {MAX_COST_USD}")
        pilot_units = config.get("pilot_units")
        activation = config.get("activation")
        if (
            not isinstance(pilot_units, list)
            or not pilot_units
            or any(not isinstance(unit_id, str) for unit_id in pilot_units)
            or len(pilot_units) != len(set(pilot_units))
            or any(unit_id not in units for unit_id in pilot_units)
            or config.get("pilot_unit") not in pilot_units
            or (activation == "active" and set(pilot_units) != set(units))
            or "operational_ceiling_usd" not in config
            or (
                config.get("operational_ceiling_usd") is not None
                and _decimal_cost(config.get("operational_ceiling_usd")) is None
            )
        ):
            raise GateError("micro profile pilot authorization policy is malformed")
    for unit_id in unit_ids:
        unit = units[unit_id]
        if profile == LEGACY_PROFILE_ID and unit.get("max_cost_usd") != LEGACY_MAX_COST_USD:
            raise GateError(f"{unit_id}: max_cost_usd must be {LEGACY_MAX_COST_USD}")
        paths = unit.get("paths")
        if not isinstance(paths, list) or not paths:
            raise GateError(f"{unit_id}: paths are required")
        seen_specs: set[str] = set()
        for spec in paths:
            if (
                not isinstance(spec, str)
                or not spec
                or "\\" in spec
                or spec.startswith("/")
                or ":" in spec
                or "//" in spec
                or any(part in {"", ".", ".."} for part in spec.split("/"))
            ):
                raise GateError(f"{unit_id}: malformed path spec")
            if spec in seen_specs:
                raise GateError(f"{unit_id}: duplicate path spec {spec}")
            seen_specs.add(spec)
            prefix = spec.rstrip("/") + "/"
            matches = [item for item in tracked if item == spec or item.startswith(prefix)]
            if not matches:
                raise GateError(f"{unit_id}: missing path spec {spec}")
            assignments[unit_id].extend(matches)
        if len(assignments[unit_id]) != len(set(assignments[unit_id])):
            message = (
                "duplicate file assignment"
                if profile == LEGACY_PROFILE_ID
                else "duplicate assignment"
            )
            raise GateError(f"{unit_id}: {message}")
        expected = unit.get("expected_file_count")
        if expected != len(assignments[unit_id]):
            raise GateError(
                f"{unit_id}: expected {expected} files, got {len(assignments[unit_id])}"
            )
    owners: dict[str, str] = {}
    for unit_id, files in assignments.items():
        for file in files:
            if file in owners:
                raise GateError(f"duplicate assignment: {file}")
            owners[file] = unit_id
    unassigned = sorted(tracked_set - set(owners))
    extra = sorted(set(owners) - tracked_set)
    if unassigned:
        raise GateError(f"unassigned tracked files: {', '.join(unassigned)}")
    if extra:
        raise GateError(f"manifest assigns non-tracked files: {', '.join(extra)}")
    expected_total = config.get("expected_total_files")
    if expected_total != len(tracked):
        raise GateError(f"expected {expected_total} tracked files, got {len(tracked)}")
    if sum(len(files) for files in assignments.values()) != expected_total:
        raise GateError("unit counts do not sum to expected_total_files")
    knowledge = config.get("common_knowledge_base")
    if knowledge != COMMON_KNOWLEDGE_BASE:
        raise GateError("common knowledge base is not the approved set")
    return assignments


def _validate_repo_manifest(
    repo: Path, profile: str, manifest: dict[str, Any] | None = None
) -> dict[str, list[str]]:
    if manifest is None:
        sha = require_clean_revision(repo)
        manifest = _load_repo_manifest(repo, sha)
    return validate_manifest(repo, profile=profile, manifest_data=manifest)


def _worktree_roots(repo: Path) -> list[Path]:
    """Discover the main checkout and every linked worktree from Git metadata."""
    repo = Path(repo).resolve()
    try:
        common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    except GateError:
        raise GateError("unable to discover Git worktree metadata") from None
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    if not common.is_dir():
        raise GateError("Git common directory is unavailable")
    roots = [repo, common.parent.resolve()]
    linked_admin = common / "worktrees"
    if not linked_admin.exists():
        return list(dict.fromkeys(roots))
    if not linked_admin.is_dir():
        raise GateError("Git linked-worktree metadata is malformed")
    try:
        entries = list(linked_admin.iterdir())
    except OSError:
        raise GateError("cannot inspect Git linked-worktree metadata") from None
    for entry in entries:
        if not entry.is_dir():
            continue
        marker = entry / "gitdir"
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            raise GateError("cannot read Git linked-worktree metadata") from None
        if text.startswith("gitdir:"):
            text = text.partition(":")[2].strip()
        if not text:
            raise GateError("Git linked-worktree metadata is malformed")
        linked_git = Path(text)
        if not linked_git.is_absolute():
            linked_git = (entry / linked_git).resolve()
        else:
            linked_git = linked_git.resolve()
        if linked_git.name != ".git" or not linked_git.is_dir() and not linked_git.is_file():
            raise GateError("Git linked-worktree metadata is malformed")
        roots.append(linked_git.parent.resolve())
    return list(dict.fromkeys(roots))


def prepare_output(
    repo: Path,
    results_root: Path,
    sha: str,
    shard_id: str,
    *,
    profile: str = LEGACY_PROFILE_ID,
    manifest: dict[str, Any] | None = None,
) -> Path:
    if shard_id not in _profile_units(profile, manifest):
        raise GateError(f"unknown shard: {shard_id}")
    root = Path(os.path.abspath(results_root))
    if _path_has_reparse_component(root):
        raise GateError("results root cannot contain links or reparse points")
    for forbidden in _worktree_roots(repo):
        if root == forbidden or forbidden in root.parents:
            raise GateError("results root must be outside repository and enclosing worktree")
    output = _safe_create_descendant(root, Path(sha) / shard_id)
    if any(output.iterdir()):
        raise GateError("refusing non-empty output directory")
    return output


def _safe_create_descendant(root: Path, relative: Path) -> Path:
    """Create directories one component at a time without traversing links."""
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise GateError("output path must be a safe relative path")
    base = Path(os.path.abspath(root))
    if _path_has_reparse_component(base):
        raise GateError("output root cannot contain links or reparse points")
    current = base
    try:
        current.mkdir(parents=True, exist_ok=True)
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise GateError("output path cannot contain links or reparse points")
            if not stat.S_ISDIR(metadata.st_mode):
                raise GateError("output path component is not a directory")
            current.resolve(strict=True).relative_to(base.resolve(strict=True))
    except GateError:
        raise
    except (OSError, ValueError) as exc:
        raise GateError("cannot create safe output directory") from exc
    return current


def _materialize_scan_snapshot(repo: Path, sha: str, parent: Path) -> tuple[Path, Path]:
    """Create a detached private snapshot so the scanner never reads a mutable checkout."""
    snapshot_parent = Path(os.path.abspath(parent)).parent
    if (
        _path_has_reparse_component(snapshot_parent)
        or parent.resolve() == snapshot_parent.resolve()
    ):
        raise GateError("scan snapshot must be outside scanner writable state")
    root = Path(tempfile.mkdtemp(prefix="d5-scan-snapshot-", dir=snapshot_parent))
    snapshot = root / "repo"
    try:
        _git(repo, "clone", "--no-hardlinks", "--no-checkout", str(repo.resolve()), str(snapshot))
        _git(snapshot, "checkout", "--detach", sha)
        if require_clean_revision(snapshot) != sha:
            raise GateError("scan snapshot does not match the authorized exact SHA")
        return root, snapshot
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def build_scanner_argv(
    repo: Path,
    shard_id: str,
    output_dir: Path,
    *,
    dry_run: bool = False,
    profile: str = LEGACY_PROFILE_ID,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    shard = _profile_units(profile, manifest).get(shard_id)
    if shard is None:
        raise GateError(f"unknown shard: {shard_id}")
    repo = Path(repo)
    for path in shard["paths"]:
        _validated_repo_input(repo, path.rstrip("/"), allow_directory=True)
    argv = [
        "npx",
        "--yes",
        "--registry=https://registry.npmjs.org/",
        "--ignore-scripts",
        "@openai/codex-security@0.1.18",
        "scan",
        str(repo.resolve()),
    ]
    for path in shard["paths"]:
        argv.extend(["--path", path])
    for path in COMMON_KNOWLEDGE_BASE:
        knowledge_base = _validated_repo_input(repo, path, allow_directory=False)
        argv.extend(["--knowledge-base", str(knowledge_base)])
    argv.extend(
        [
            "--auth",
            "chatgpt",
            "--model",
            "gpt-5.6-sol",
            "--effort",
            "high",
            "--output-dir",
            str(output_dir.resolve()),
            "--fail-on-severity",
            "high",
            "--max-cost",
            str(_profile_config(profile, manifest).get("max_cost_usd", MAX_COST_USD)),
            "--codex",
            'approval_policy="never"',
        ]
    )
    if profile == MICRO_PROFILE_ID:
        argv.extend(
            [
                "--codex",
                "features.multi_agent_v2.max_concurrent_threads_per_session=3",
            ]
        )
    if dry_run:
        argv.append("--dry-run")
    return argv


def plan_unit(repo: Path, unit_id: str, *, profile: str = MICRO_PROFILE_ID) -> dict[str, Any]:
    """Return an offline unit preview without creating output or starting a process."""
    if profile != MICRO_PROFILE_ID:
        raise GateError("plan-unit is only available for micro-v2")
    sha = require_clean_revision(repo)
    manifest = _load_repo_manifest(repo, sha)
    assignments = _validate_repo_manifest(repo, profile, manifest)
    if unit_id not in assignments:
        raise GateError(f"unknown unit: {unit_id}")
    config = _profile_config(profile, manifest)
    files = sorted(assignments[unit_id])
    output_label = f"<external-results>/<sha>/{unit_id}"
    argv = build_scanner_argv(
        repo, unit_id, repo / "preview-output", profile=profile, manifest=manifest
    )
    argv[6] = "<repo>"
    argv[argv.index("--output-dir") + 1] = output_label
    return {
        "profile": profile,
        "profile_digest": profile_digest(profile, manifest),
        "unit": unit_id,
        "sha": sha,
        "assigned_paths": files,
        "file_count": len(files),
        "bytes": sum((repo / file).stat().st_size for file in files),
        "capacity": config["session_capacity"],
        "worker_slots": config["session_capacity"] - 1,
        "max_cost_usd": config["max_cost_usd"],
        "outer_concurrency": config["outer_concurrency"],
        "output_dir": output_label,
        "scanner_argv": argv,
        "scanner_invoked": False,
    }


_ENV_ALLOWLIST = {
    "APPDATA",
    "ComSpec",
    "LOCALAPPDATA",
    "NPM_CONFIG_CACHE",
    "PATHEXT",
    "Path",
    "PATH",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "SystemDrive",
    "SystemRoot",
    "TEMP",
    "TMP",
    "WINDIR",
}


LEGACY_ENVIRONMENT_POLICY_VERSION = "windows-casefold-v2-sterile-npm-cwd"
OUTPUT_WRITE_ENVIRONMENT_POLICY_VERSION = "windows-casefold-v3-scan-output-write-probe"
SCANNER_RUNTIME_ENVIRONMENT_POLICY_VERSION = "windows-casefold-v4-scanner-runtime-scope"
ENVIRONMENT_POLICY_VERSION = "windows-casefold-v5-bounded-process-tree"
PREFLIGHT_TIMEOUT_SECONDS = 10
SCANNER_TIMEOUT_SECONDS = 60 * 60
SCANNER_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
PREFLIGHT_OUTPUT_LIMIT_BYTES = 64 * 1024


def select_allowlisted_environment(source: Mapping[str, str], *, windows: bool) -> dict[str, str]:
    if not windows:
        return {key: value for key, value in source.items() if key in _ENV_ALLOWLIST}
    allowed = {key.upper() for key in _ENV_ALLOWLIST}
    selected: dict[str, str] = {}
    for key, value in source.items():
        canonical = key.upper()
        if canonical not in allowed:
            continue
        previous = selected.get(canonical)
        if previous is not None and previous != value:
            raise GateError(f"conflicting environment aliases for {canonical}")
        selected[canonical] = value
    return selected


def build_child_environment(
    state_dir: Path,
    source: Mapping[str, str] | None = None,
    *,
    codex_state_dir: Path | None = None,
    protected_root_dir: Path | None = None,
) -> dict[str, str]:
    values = source if source is not None else os.environ
    state = Path(os.path.abspath(state_dir))
    codex_state = Path(os.path.abspath(codex_state_dir or state_dir))
    npm_state: Path | None = None
    windows_directory_handles: list[int] = []

    def secure_directory(path: Path) -> None:
        handle = _secure_or_create_windows_directory(path)
        if handle is not None:
            windows_directory_handles.append(handle)

    try:
        if _path_has_reparse_component(state) or _path_has_reparse_component(codex_state):
            raise GateError("scanner state paths cannot contain links or reparse points")
        if os.name != "nt":
            state.mkdir(parents=True, exist_ok=True)
            codex_state.mkdir(parents=True, exist_ok=True)
            os.chmod(codex_state, 0o700)
        else:
            if protected_root_dir is None:
                codex_state.parent.mkdir(parents=True, exist_ok=True)
                secure_directory(codex_state.parent)
                secure_directory(codex_state)
                protected_state = codex_state
                try:
                    relative_state = state.relative_to(codex_state)
                except ValueError as exc:
                    raise GateError(
                        "scanner state must be beneath its protected results directory"
                    ) from exc
                for part in relative_state.parts:
                    protected_state = protected_state / part
                    secure_directory(protected_state)
            else:
                protected_root = Path(os.path.abspath(protected_root_dir))
                try:
                    relative_codex_state = codex_state.relative_to(protected_root)
                    relative_state = state.relative_to(codex_state)
                except ValueError as exc:
                    raise GateError(
                        "scanner state must be beneath its protected results directory"
                    ) from exc
                secure_directory(protected_root)
                protected_state = protected_root
                for part in relative_codex_state.parts:
                    protected_state = protected_state / part
                    secure_directory(protected_state)
                protected_state = codex_state
                for part in relative_state.parts:
                    protected_state = protected_state / part
                    secure_directory(protected_state)
            if _path_has_reparse_component(state) or _path_has_reparse_component(codex_state):
                raise GateError("scanner state paths cannot contain links or reparse points")
        npm_parent = codex_state
        npm_parent.mkdir(parents=True, exist_ok=True)
        if _path_has_reparse_component(npm_parent):
            raise GateError("scanner state paths cannot contain links or reparse points")
        for _attempt in range(32):
            npm_state = npm_parent / secrets.token_urlsafe(3)
            try:
                if os.name == "nt":
                    _create_private_windows_directory(npm_state)
                else:
                    npm_state.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("unable to allocate a unique scanner state directory")
        if os.name != "nt":
            os.chmod(npm_state, 0o700)
        npm_cache = npm_state
        user_config = npm_state / "npm-userconfig"
        global_config = npm_state / "npm-globalconfig"
        if any(
            _path_has_reparse_component(path) for path in (npm_cache, user_config, global_config)
        ):
            raise GateError("scanner state paths cannot contain links or reparse points")
        npm_cache.mkdir(parents=True, exist_ok=True)
        _create_exclusive_empty_file(user_config)
        _create_exclusive_empty_file(global_config)
    except GateError:
        _close_windows_directory_handles(windows_directory_handles)
        if npm_state is not None:
            shutil.rmtree(npm_state, ignore_errors=True)
        raise
    except OSError as exc:
        _close_windows_directory_handles(windows_directory_handles)
        if npm_state is not None:
            shutil.rmtree(npm_state, ignore_errors=True)
        raise GateError("unable to create isolated scanner state") from exc
    try:
        env = select_allowlisted_environment(values, windows=os.name == "nt")
        env["CODEX_SECURITY_STATE_DIR"] = str(codex_state)
        env["NPM_CONFIG_USERCONFIG"] = str(user_config)
        env["NPM_CONFIG_GLOBALCONFIG"] = str(global_config)
        env["NPM_CONFIG_CACHE"] = str(npm_cache)
    except Exception:
        _close_windows_directory_handles(windows_directory_handles)
        if npm_state is not None:
            shutil.rmtree(npm_state, ignore_errors=True)
        raise
    if windows_directory_handles:
        with _WINDOWS_DIRECTORY_LOCKS_GUARD:
            _WINDOWS_DIRECTORY_LOCKS[str(npm_state)] = windows_directory_handles
    return env


def cleanup_child_environment(
    env: Mapping[str, str], *, release_directory_locks: bool = True
) -> None:
    config = env.get("NPM_CONFIG_USERCONFIG")
    if not config:
        return
    root = Path(config).parent
    if release_directory_locks:
        with _WINDOWS_DIRECTORY_LOCKS_GUARD:
            directory_handles = _WINDOWS_DIRECTORY_LOCKS.pop(str(root), [])
        _close_windows_directory_handles(directory_handles)
    state_root = env.get("CODEX_SECURITY_STATE_DIR")
    try:
        expected_parent = Path(state_root).resolve() if state_root else None
        if (
            state_root
            and re.fullmatch(r"[A-Za-z0-9_-]{4}", root.name)
            and expected_parent is not None
            and root.parent.resolve() == expected_parent
            and not root.is_symlink()
        ):
            shutil.rmtree(root)
    except OSError:
        pass


def _create_exclusive_empty_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("configuration output is not a regular file")
    finally:
        os.close(descriptor)


def _create_private_windows_directory(path: Path) -> None:
    """Create a per-run directory with an owner-only DACL from the first instant."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_token.restype = wintypes.BOOL
    get_token_info = advapi32.GetTokenInformation
    get_token_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_info.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert_sid.restype = wintypes.BOOL
    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert_sddl.restype = wintypes.BOOL
    if not open_token(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "unable to inspect current Windows token")
    descriptor = ctypes.c_void_p()
    sid_text = ctypes.c_wchar_p()
    try:
        required = wintypes.DWORD()
        get_token_info(token, 1, None, 0, ctypes.byref(required))
        token_user = ctypes.create_string_buffer(required.value)
        if not get_token_info(token, 1, token_user, required, ctypes.byref(required)):
            raise OSError(ctypes.get_last_error(), "unable to read current Windows user SID")
        sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not convert_sid(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "unable to format current Windows user SID")
        sddl = f"D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        if not convert_sddl(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error(), "unable to build private Windows DACL")

        class SecurityAttributes(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("length", wintypes.DWORD),
                ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL),
            ]

        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        create_directory = kernel32.CreateDirectoryW
        create_directory.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        create_directory.restype = wintypes.BOOL
        if not create_directory(str(path), ctypes.byref(attributes)):
            error_code = ctypes.get_last_error()
            if error_code == 183:
                raise FileExistsError(error_code, "scanner state path already exists", str(path))
            raise OSError(error_code, "unable to create owner-only scanner state directory")
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        kernel32.CloseHandle(token)
    if _path_has_reparse_component(path):
        raise OSError("configuration output path changed during creation")


def _secure_or_create_windows_directory(path: Path) -> int | None:
    if _path_has_reparse_component(path):
        raise OSError("scanner results path cannot contain links or reparse points")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _create_private_windows_directory(path)
        return _secure_windows_directory(path)
    if not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise OSError("scanner results path is not a regular directory")
    return _secure_windows_directory(path)


def _secure_windows_directory(path: Path) -> int:
    """Protect an existing results parent from lower-trust replacement."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_token.restype = wintypes.BOOL
    if not open_token(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "unable to inspect current Windows token")
    descriptor = ctypes.c_void_p()
    sid_text = ctypes.c_wchar_p()
    handle = wintypes.HANDLE()
    try:
        required = wintypes.DWORD()
        get_token_info = advapi32.GetTokenInformation
        get_token_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_token_info.restype = wintypes.BOOL
        get_token_info(token, 1, None, 0, ctypes.byref(required))
        token_user = ctypes.create_string_buffer(required.value)
        if not get_token_info(token, 1, token_user, required, ctypes.byref(required)):
            raise OSError(ctypes.get_last_error(), "unable to read current Windows user SID")
        sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p))[0]
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert_sid.restype = wintypes.BOOL
        if not convert_sid(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "unable to format current Windows user SID")
        sddl = f"D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert_sddl.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert_sddl.restype = wintypes.BOOL
        if not convert_sddl(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error(), "unable to build private Windows DACL")
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise OSError(ctypes.get_last_error(), "unable to read private Windows DACL")
        if _path_has_reparse_component(path):
            raise OSError("scanner results directory cannot contain links or reparse points")
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        # READ_CONTROL is needed by SetSecurityInfo while replacing the DACL.
        # DELETE plus omitted FILE_SHARE_DELETE prevents another handle from
        # renaming/replacing this directory during the scan.
        handle = create_file(str(path), 0x00070000, 0x00000003, None, 3, 0x02200000, None)
        if int(handle) == ctypes.c_void_p(-1).value:
            handle = wintypes.HANDLE()
            raise OSError(ctypes.get_last_error(), "unable to open scanner results directory")

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("attributes", wintypes.DWORD),
                ("reparse_tag", wintypes.DWORD),
            ]

        attributes = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)) or (
            attributes.attributes & 0x00000400 or not attributes.attributes & 0x00000010
        ):
            raise OSError("scanner results path is not a regular directory")

        opened_identity = _windows_file_identity(_native_handle_value(handle))
        set_security = advapi32.SetSecurityInfo
        set_security.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_security.restype = wintypes.DWORD
        error_code = set_security(handle, 1, 0x80000004, None, None, dacl, None)
        if error_code:
            raise OSError(error_code, "unable to secure scanner results directory")
        verified_handle = create_file(str(path), 0x00040000, 0x00000003, None, 3, 0x02200000, None)
        if int(verified_handle) == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "unable to re-open scanner results directory")
        try:
            verified_attributes = FileAttributeTagInfo()
            if not get_info(
                verified_handle,
                9,
                ctypes.byref(verified_attributes),
                ctypes.sizeof(verified_attributes),
            ) or (
                verified_attributes.attributes & 0x00000400
                or not verified_attributes.attributes & 0x00000010
                or _windows_file_identity(_native_handle_value(verified_handle)) != opened_identity
                or _path_has_reparse_component(path)
            ):
                raise OSError("scanner results path changed while being secured")
        finally:
            kernel32.CloseHandle(verified_handle)
        if _path_has_reparse_component(path):
            raise OSError("scanner results path changed while being secured")
        held_handle = _native_handle_value(handle)
        handle = wintypes.HANDLE()
        return held_handle
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        if descriptor:
            kernel32.LocalFree(descriptor)
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        kernel32.CloseHandle(token)


def _close_windows_directory_handles(handles: list[int]) -> None:
    if os.name != "nt" or not handles:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    for handle in reversed(handles):
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _windows_file_identity(handle_value: int) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        ]

    class FileIdentity(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("access_time", FileTime),
            ("write_time", FileTime),
            ("volume_serial_number", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_identity = kernel32.GetFileInformationByHandle
    get_identity.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileIdentity)]
    get_identity.restype = wintypes.BOOL
    identity = FileIdentity()
    if not get_identity(wintypes.HANDLE(handle_value), ctypes.byref(identity)):
        raise OSError(ctypes.get_last_error(), "unable to identify scanner state directory")
    return (
        identity.volume_serial_number,
        identity.file_index_high,
        identity.file_index_low,
    )


def _native_handle_value(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        raise OSError("unable to retain scanner results directory handle")
    return int(value)


def _decimal_cost(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, str | int | float | Decimal):
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not cost.is_finite() or cost < 0:
        return None
    try:
        quantized = cost.quantize(COST_QUANTUM)
    except InvalidOperation:
        return None
    return quantized if cost == quantized else None


def _json_default(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _latest_cost(text: str) -> Decimal | None:
    values = re.findall(
        r"(?:estimated\s+cost|estimated_cost(?:_usd)?)\s*[:=]\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if not values:
        return None
    return _decimal_cost(values[-1])


def _run_bounded_scanner(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    executable: str,
    output_path: Path,
    timeout_seconds: float | None = None,
    output_limit_bytes: int | None = None,
) -> tuple[int, str]:
    baseline_children: set[int] | None = None
    prior_subreaper: bool | None = None
    if os.name != "nt":
        if sys.platform != "linux":
            raise GateError("bounded scanner process isolation requires Windows or Linux")
        prior_subreaper = _set_linux_child_subreaper(True)
    try:
        if prior_subreaper is not None:
            baseline_children = _linux_descendant_pids(os.getpid())
        return _run_bounded_scanner_impl(
            argv,
            cwd=cwd,
            env=env,
            executable=executable,
            output_path=output_path,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            baseline_children=baseline_children,
        )
    finally:
        if prior_subreaper is not None:
            _set_linux_child_subreaper(prior_subreaper)


def _set_linux_child_subreaper(enabled: bool) -> bool:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    state = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(state), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "unable to read Linux child subreaper state")
    previous = bool(state.value)
    if previous != enabled and libc.prctl(36, int(enabled), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "unable to configure Linux child subreaper")
    return previous


def _linux_descendant_pids(root_pid: int) -> set[int]:
    process_parents: dict[int, int] = {}
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdecimal():
            continue
        try:
            stat_line = (process_dir / "stat").read_bytes()
            closing = stat_line.rfind(b")")
            fields = stat_line[closing + 1 :].split()
            process_parents[int(process_dir.name)] = int(fields[1])
        except (IndexError, OSError, ValueError):
            continue
    descendants: set[int] = set()
    parents = {root_pid}
    while parents:
        children = {
            process_id
            for process_id, parent_id in process_parents.items()
            if parent_id in parents and process_id not in descendants
        }
        if not children:
            break
        descendants.update(children)
        parents = children
    return descendants


def _signal_linux_processes(process_ids: set[int], process_signal: int) -> None:
    for process_id in process_ids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process_id, process_signal)


def _reap_linux_children(process_ids: set[int], *, timeout_seconds: float = 1) -> set[int]:
    pending = set(process_ids)
    deadline = time.monotonic() + timeout_seconds
    while pending and time.monotonic() < deadline:
        for process_id in tuple(pending):
            try:
                waited, _ = os.waitpid(process_id, os.WNOHANG)  # type: ignore[attr-defined]
            except (ChildProcessError, ProcessLookupError):
                if not Path(f"/proc/{process_id}").exists():
                    pending.discard(process_id)
            else:
                if waited == process_id:
                    pending.discard(process_id)
        if pending:
            time.sleep(0.02)
    return pending


def _emergency_cleanup_scanner(
    process: subprocess.Popen[bytes] | None,
    *,
    output: Any | None,
    pipe: Any | None,
    reader: threading.Thread | None,
    chunks: queue.Queue[bytes | None],
    windows_job: int | None,
    baseline_children: set[int] | None,
    output_fd: int,
) -> None:
    """Best-effort bounded cleanup for every exception after resources are acquired."""
    if process is not None:
        with contextlib.suppress(BaseException):
            _terminate_scanner_process_tree(process, force=True, windows_job=windows_job)
    if windows_job is not None:
        with contextlib.suppress(BaseException):
            _close_windows_job(windows_job)
        windows_job = None
    if process is not None:
        if os.name == "nt":
            still_running = True
            with contextlib.suppress(BaseException):
                still_running = process.poll() is None
            if still_running:
                with contextlib.suppress(BaseException):
                    process.kill()
        if baseline_children is not None:
            with contextlib.suppress(BaseException):
                _signal_linux_processes(
                    _linux_descendant_pids(os.getpid()) - baseline_children,
                    signal.SIGKILL,  # type: ignore[attr-defined]
                )
        with contextlib.suppress(BaseException):
            process.wait(timeout=2)
        if baseline_children is not None:
            with contextlib.suppress(BaseException):
                remaining = _linux_descendant_pids(os.getpid()) - baseline_children
                _signal_linux_processes(remaining, signal.SIGKILL)  # type: ignore[attr-defined]
                _reap_linux_children(remaining)
    if reader is not None and reader.ident is not None:
        drain_deadline = time.monotonic() + 1
        while reader.is_alive() and time.monotonic() < drain_deadline:
            with contextlib.suppress(queue.Empty):
                chunks.get(timeout=0.05)
            reader.join(timeout=0.05)
    if pipe is not None and (reader is None or not reader.is_alive()):
        with contextlib.suppress(BaseException):
            pipe.close()
    if output is not None:
        with contextlib.suppress(BaseException):
            output.close()
    elif output_fd >= 0:
        with contextlib.suppress(BaseException):
            os.close(output_fd)


def _run_bounded_scanner_impl(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    executable: str,
    output_path: Path,
    timeout_seconds: float | None,
    output_limit_bytes: int | None,
    baseline_children: set[int] | None,
) -> tuple[int, str]:
    """Stream bounded scanner output and terminate its process tree on limits."""
    timeout_seconds = SCANNER_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    output_limit_bytes = (
        SCANNER_OUTPUT_LIMIT_BYTES if output_limit_bytes is None else output_limit_bytes
    )
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
    if _path_has_reparse_component(output_path):
        raise GateError("scanner output path cannot contain links or reparse points")
    try:
        output_fd = os.open(
            output_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except OSError as exc:
        raise GateError("scanner output file must be created exclusively") from exc
    output: Any | None = None
    process: subprocess.Popen[bytes] | None = None
    pipe: Any | None = None
    reader: threading.Thread | None = None
    windows_job: int | None = None
    completed = False
    try:
        output = os.fdopen(output_fd, "wb")
        output_fd = -1
        creation_options: dict[str, Any] = {}
        if os.name == "nt":
            windows_job = _create_windows_job()
            creation_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        else:
            creation_options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                executable=executable,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **creation_options,
            )
        except OSError as exc:
            raise GateError("unable to launch scanner") from exc
        if windows_job is not None:
            try:
                _assign_and_resume_windows_job(process, windows_job)
            except Exception as exc:
                raise GateError("unable to isolate scanner process tree") from exc
        pipe = process.stdout
        if pipe is None:
            raise GateError("scanner output pipe is unavailable")
        pipe_handle = pipe

        def pump() -> None:
            try:
                while True:
                    chunk = pipe_handle.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.put(chunk)
            except OSError:
                pass
            finally:
                chunks.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        started = time.monotonic()
        written = 0
        reason: str | None = None
        root_exit_checked = False
        sentinel = False
        stop_deadline: float | None = None
        force_kill_sent = False
        captured = bytearray()
        while not sentinel:
            now = time.monotonic()
            if not root_exit_checked and process.poll() is not None:
                root_exit_checked = True
                if windows_job is not None:
                    descendants_remaining = (
                        _windows_job_has_active_processes(windows_job) is not False
                    )
                    if descendants_remaining:
                        _close_windows_job(windows_job)
                        windows_job = None
                else:
                    process_descendants = _linux_descendant_pids(process.pid)
                    adopted_descendants = _linux_descendant_pids(os.getpid()) - (
                        baseline_children or set()
                    )
                    remaining_descendants = process_descendants | adopted_descendants
                    descendants_remaining = _scanner_process_group_exists(process.pid) or bool(
                        remaining_descendants
                    )
                    if descendants_remaining:
                        _terminate_scanner_process_tree(process, force=False)
                        _signal_linux_processes(remaining_descendants, signal.SIGTERM)
                if descendants_remaining:
                    if reason is None:
                        reason = "scanner_descendants_remaining"
                    stop_deadline = now + 2
            if reason is None and now - started > timeout_seconds:
                reason = "scanner_runtime_limit_exceeded"
                _terminate_scanner_process_tree(process, force=False, windows_job=windows_job)
                stop_deadline = now + 2
            elif reason is not None and stop_deadline is not None and now >= stop_deadline:
                if not force_kill_sent:
                    _terminate_scanner_process_tree(process, force=True, windows_job=windows_job)
                    if baseline_children is not None:
                        _signal_linux_processes(
                            _linux_descendant_pids(os.getpid()) - baseline_children,
                            signal.SIGKILL,  # type: ignore[attr-defined]
                        )
                    force_kill_sent = True
                    stop_deadline = now + 2
                else:
                    break
            try:
                chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if chunk is None:
                sentinel = True
                continue
            remaining = output_limit_bytes - written
            if remaining > 0:
                accepted = chunk[:remaining]
                output.write(accepted)
                captured.extend(accepted)
                written += len(accepted)
            if len(chunk) > remaining and reason is None:
                reason = "scanner_output_limit_exceeded"
                _terminate_scanner_process_tree(process, force=False, windows_job=windows_job)
                stop_deadline = time.monotonic() + 2
        if reason:
            marker = ("\n" + reason + "\n").encode("utf-8")
            remaining = max(0, output_limit_bytes - written)
            output.write(marker[:remaining])
        output.flush()
        output.close()
        output = None
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_scanner_process_tree(process, force=True, windows_job=windows_job)
            try:
                return_code = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return_code = 2
        if os.name != "nt" and _scanner_process_group_exists(process.pid):
            _terminate_scanner_process_tree(process, force=False)
            try:
                _wait_for_scanner_process_group(process.pid, timeout_seconds=1)
            except subprocess.TimeoutExpired:
                _terminate_scanner_process_tree(process, force=True)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    _wait_for_scanner_process_group(process.pid, timeout_seconds=1)
            if reason is None:
                reason = "scanner_descendants_remaining"
        if baseline_children is not None:
            remaining_descendants = _linux_descendant_pids(os.getpid()) - baseline_children
            if remaining_descendants:
                _signal_linux_processes(remaining_descendants, signal.SIGKILL)  # type: ignore[attr-defined]
                _reap_linux_children(remaining_descendants)
                if reason is None:
                    reason = "scanner_descendants_remaining"
        if windows_job is not None:
            _close_windows_job(windows_job)
            windows_job = None
        reader.join(timeout=0.2)
        drain_deadline = time.monotonic() + 1
        while reader.is_alive() and time.monotonic() < drain_deadline:
            with contextlib.suppress(queue.Empty):
                chunks.get(timeout=0.05)
            reader.join(timeout=0.05)
        if baseline_children is not None:
            lingering_descendants = _linux_descendant_pids(os.getpid()) - baseline_children
            if lingering_descendants:
                _signal_linux_processes(lingering_descendants, signal.SIGKILL)  # type: ignore[attr-defined]
                _reap_linux_children(lingering_descendants)
                if reason is None:
                    reason = "scanner_descendants_remaining"
        if reader.is_alive():
            if reason is None:
                reason = "scanner_output_reader_stalled"
        else:
            pipe.close()
            pipe = None
        completed = not reader.is_alive()
        if reason:
            raise BoundedScannerError(reason, captured.decode("utf-8", errors="replace"))
        return return_code, captured.decode("utf-8", errors="replace")

    finally:
        if not completed:
            _emergency_cleanup_scanner(
                process,
                output=output,
                pipe=pipe,
                reader=reader,
                chunks=chunks,
                windows_job=windows_job,
                baseline_children=baseline_children,
                output_fd=output_fd,
            )


def _create_windows_job() -> int:
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("per_process_user_time", ctypes.c_longlong),
            ("per_job_user_time", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    handle = create_job(None, None)
    if not handle:
        raise GateError("unable to create scanner process job")
    limits = ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    set_info = kernel32.SetInformationJobObject
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not set_info(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise GateError(f"unable to configure scanner process job ({error_code})")
    return int(handle)


def _assign_and_resume_windows_job(process: subprocess.Popen[bytes], job: int) -> None:
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("delta_priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    snapshot = create_snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "unable to enumerate scanner thread")
    entry = ThreadEntry32()
    entry.size = ctypes.sizeof(entry)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    thread_id: int | None = None
    first = kernel32.Thread32First
    next_thread = kernel32.Thread32Next
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    next_thread.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    if first(snapshot, ctypes.byref(entry)):
        while True:
            if entry.owner_process_id == process.pid:
                thread_id = entry.thread_id
                break
            if not next_thread(snapshot, ctypes.byref(entry)):
                break
    close_handle(snapshot)
    if thread_id is None:
        raise OSError("scanner primary thread was not found")
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign.restype = wintypes.BOOL
    if not assign(job, process._handle):  # type: ignore[attr-defined] # noqa: SLF001
        raise OSError(ctypes.get_last_error(), "unable to assign scanner process job")
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_thread.restype = wintypes.HANDLE
    thread = open_thread(0x0002, False, thread_id)
    if not thread:
        raise OSError(ctypes.get_last_error(), "unable to open scanner primary thread")
    try:
        resume_thread = kernel32.ResumeThread
        resume_thread.argtypes = [wintypes.HANDLE]
        resume_thread.restype = wintypes.DWORD
        if resume_thread(thread) == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "unable to resume scanner process")
    finally:
        close_handle(thread)


def _close_windows_job(job: int) -> None:
    import ctypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(job)


def _windows_job_has_active_processes(job: int) -> bool | None:
    import ctypes
    from ctypes import wintypes

    class BasicAccountingInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("this_period_total_user_time", ctypes.c_longlong),
            ("this_period_total_kernel_time", ctypes.c_longlong),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    information = BasicAccountingInformation()
    if not query(job, 1, ctypes.byref(information), ctypes.sizeof(information), None):
        return None
    return bool(information.active_processes > 0)


def _terminate_scanner_process_tree(
    process: subprocess.Popen[bytes], *, force: bool, windows_job: int | None = None
) -> bool:
    if os.name == "nt":
        if windows_job is None:
            return False
        import ctypes

        terminate = ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        terminate.restype = ctypes.c_int
        return bool(terminate(windows_job, 1))
    try:
        kill_group = os.killpg  # type: ignore[attr-defined]
        termination_signal = getattr(signal, "SIGKILL" if force else "SIGTERM")
        kill_group(process.pid, termination_signal)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        with contextlib.suppress(OSError):
            process.kill() if force else process.terminate()
        return False


def _scanner_process_group_exists(process_id: int) -> bool:
    if os.name == "nt":
        return False
    try:
        kill_group = os.killpg  # type: ignore[attr-defined]
        kill_group(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _wait_for_scanner_process_group(process_id: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _scanner_process_group_exists(process_id):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("scanner process group", timeout_seconds)
        time.sleep(0.05)


def _read_baseline(root: Path) -> dict[str, Any]:
    path = root / BASELINE_FILENAME
    try:
        value = _strict_json_document(_bounded_file_bytes(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GateError("missing or malformed shared baseline") from exc
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str):
        raise GateError("shared baseline is malformed")
    return value


def _ensure_baseline(root: Path, sha: str, shard_id: str) -> None:
    path = root / BASELINE_FILENAME
    if path.exists():
        if _read_baseline(root).get("sha") != sha:
            raise GateError("shared baseline revision mismatch")
        return
    payload = {
        "schema_version": "1.0",
        "sha": sha,
        "first_shard": shard_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, prefix=".baseline-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        if path.exists():
            if _read_baseline(root).get("sha") != sha:
                raise GateError("shared baseline revision mismatch")
        else:
            os.replace(temporary, path)
            temporary = None
    except OSError as exc:
        raise GateError("unable to create shared baseline") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class ScanResult:
    output_dir: Path
    exit_code: int
    cost_usd: Decimal | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    total_cost_usd: Decimal | None = None
    artifact_union_pass: bool | None = None


def _write_runner_result(output: Path, runner: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output, prefix=".runner-result-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(runner, handle, indent=2, default=_json_default)
            handle.write("\n")
        os.replace(temporary, output / "runner-result.json")
        temporary = None
    except OSError as exc:
        raise GateError("unable to write runner result") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


PREFLIGHT_CHECKS = {
    "node_crypto": (
        "node.exe" if os.name == "nt" else "node",
        ["-e", "require('node:crypto').randomBytes(32)"],
    ),
    "npx_version": (
        "npx.cmd" if os.name == "nt" else "npx",
        ["--version"],
    ),
}
PREFLIGHT_EVIDENCE_CHECKS = (*PREFLIGHT_CHECKS, "scan_output_write", "scanner_runtime_scope")
SUPPORTED_ENVIRONMENT_POLICY_VERSIONS = {
    LEGACY_ENVIRONMENT_POLICY_VERSION: tuple(PREFLIGHT_CHECKS),
    OUTPUT_WRITE_ENVIRONMENT_POLICY_VERSION: (*PREFLIGHT_CHECKS, "scan_output_write"),
    SCANNER_RUNTIME_ENVIRONMENT_POLICY_VERSION: PREFLIGHT_EVIDENCE_CHECKS,
    ENVIRONMENT_POLICY_VERSION: PREFLIGHT_EVIDENCE_CHECKS,
}


def _prepare_preflight_output(repo: Path, results_root: Path, sha: str) -> Path:
    root = Path(os.path.abspath(results_root))
    if _path_has_reparse_component(root):
        raise GateError("results root cannot contain links or reparse points")
    for forbidden in _worktree_roots(repo):
        if root == forbidden or forbidden in root.parents:
            raise GateError("results root must be outside repository and enclosing worktree")
    output = _safe_create_descendant(root, Path(sha) / "runtime-preflight")
    if any(output.iterdir()):
        raise GateError("refusing non-empty output directory")
    return output


def _empty_preflight() -> dict[str, dict[str, int | str | None]]:
    return {name: {"status": "not_run", "exit_code": None} for name in PREFLIGHT_EVIDENCE_CHECKS}


def _probe_scan_output_write(output_dir: Path, writable_root: Path | None) -> tuple[str, int]:
    """Verify the exact output directory can accept a temporary scanner artifact."""
    probe: Path | None = None
    try:
        if writable_root is None:
            return "failed", 2
        output_dir.resolve().relative_to(writable_root.resolve())
        if not output_dir.exists():
            output_dir.mkdir(parents=True)
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            return "failed", 2
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_dir, prefix=".d5-write-probe-", delete=False
        ) as handle:
            probe = Path(handle.name)
            handle.write(b"d5 scan output write probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        if probe.read_bytes() != b"d5 scan output write probe\n":
            return "failed", 2
        probe.unlink()
        probe = None
        return "passed", 0
    except OSError:
        return "failed", 2
    except ValueError:
        return "failed", 2
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)


def _probe_scanner_runtime_scope(cwd: Path, child_env: Mapping[str, str]) -> tuple[str, int]:
    """Ensure executable npm state and scanner cwd are under the SDK write grant."""
    state_value = child_env.get("CODEX_SECURITY_STATE_DIR")
    if not state_value:
        return "failed", 2
    writable_root = Path(state_value)
    paths = [cwd]
    for key in ("NPM_CONFIG_CACHE", "NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
        value = child_env.get(key)
        if not value:
            return "failed", 2
        paths.append(Path(value))
    try:
        root = writable_root.resolve()
        for path in paths[:1]:
            if not path.is_absolute():
                return "failed", 2
            path.resolve().relative_to(root)
        if os.name == "nt":
            npm_paths = [
                Path(child_env[key])
                for key in (
                    "NPM_CONFIG_CACHE",
                    "NPM_CONFIG_USERCONFIG",
                    "NPM_CONFIG_GLOBALCONFIG",
                )
            ]
            private_state = npm_paths[0].resolve()
            if (
                private_state.name == ""
                or not re.fullmatch(r"[A-Za-z0-9_-]{4}", private_state.name)
                or private_state.parent != root
                or any(path.resolve().parent != private_state for path in npm_paths[1:])
                or _path_has_reparse_component(private_state)
            ):
                return "failed", 2
        for path in paths[1:]:
            if not path.is_absolute():
                return "failed", 2
            path.resolve().relative_to(root)
        if os.name == "nt":
            code_mode_host = (
                Path(child_env["NPM_CONFIG_CACHE"])
                / "_npx"
                / ("0" * 16)
                / "node_modules"
                / "@openai"
                / "codex-win32-x64"
                / "vendor"
                / "x86_64-pc-windows-msvc"
                / "bin"
                / "codex-code-mode-host.exe"
            )
            if len(str(code_mode_host)) >= 260:
                return "failed", 2
    except (OSError, ValueError):
        return "failed", 2
    return "passed", 0


def _execute_preflight(
    cwd: Path, child_env: Mapping[str, str], output_dir: Path
) -> tuple[dict[str, dict[str, int | str | None]], list[str], int]:
    path_value = child_env.get("PATH") or child_env.get("Path")
    checks = _empty_preflight()
    for name, (command, args) in PREFLIGHT_CHECKS.items():
        executable = shutil.which(command, path=path_value)
        if executable is None:
            checks[name] = {"status": "unavailable", "exit_code": None}
            return checks, [f"{name} preflight unavailable"], 2
        try:
            output_path = cwd / f".d5-preflight-{uuid.uuid4().hex}.log"
            try:
                return_code, _ = _run_bounded_scanner(
                    [command, *args],
                    cwd=cwd,
                    env=child_env,
                    executable=executable,
                    output_path=output_path,
                    timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
                    output_limit_bytes=PREFLIGHT_OUTPUT_LIMIT_BYTES,
                )
            finally:
                with contextlib.suppress(FileNotFoundError):
                    output_path.unlink()
        except GateError as exc:
            if "runtime_limit_exceeded" in str(exc):
                checks[name] = {"status": "timeout", "exit_code": None}
                return checks, [f"{name} preflight timed out"], 2
            if "output_limit_exceeded" in str(exc):
                checks[name] = {"status": "failed", "exit_code": 2}
                return checks, [f"{name} preflight output limit exceeded"], 2
            checks[name] = {"status": "unavailable", "exit_code": None}
            return checks, [f"{name} preflight unavailable"], 2
        except subprocess.TimeoutExpired:
            checks[name] = {"status": "timeout", "exit_code": None}
            return checks, [f"{name} preflight timed out"], 2
        except OSError:
            checks[name] = {"status": "unavailable", "exit_code": None}
            return checks, [f"{name} preflight unavailable"], 2
        checks[name] = {
            "status": "passed" if return_code == 0 else "failed",
            "exit_code": return_code,
        }
        if return_code != 0:
            return checks, [f"{name} preflight failed"], return_code
    scope_status, scope_exit = _probe_scanner_runtime_scope(cwd, child_env)
    checks["scanner_runtime_scope"] = {"status": scope_status, "exit_code": scope_exit}
    if scope_status != "passed":
        return checks, ["scanner_runtime_scope preflight failed"], scope_exit
    state_value = child_env.get("CODEX_SECURITY_STATE_DIR")
    writable_root = Path(state_value) if state_value else None
    output_status, output_exit = _probe_scan_output_write(output_dir, writable_root)
    checks["scan_output_write"] = {"status": output_status, "exit_code": output_exit}
    if output_status != "passed":
        return checks, ["scan_output_write preflight failed"], output_exit
    return checks, [], 0


def _write_preflight_evidence(
    output: Path,
    sha: str,
    preflight: dict[str, dict[str, int | str | None]],
    present_environment_keys: list[str],
    exit_code: int,
    reasons: list[str],
    started: datetime,
    finished: datetime,
) -> None:
    payload = {
        "schema_version": "1.0",
        "sha": sha,
        "environment_policy_version": ENVIRONMENT_POLICY_VERSION,
        "present_environment_keys": present_environment_keys,
        "preflight": preflight,
        "exit_code": exit_code,
        "passed": not reasons,
        "scanner_started": False,
        "scanner_exit_code": None,
        "cost_usd": None,
        "reasons": reasons,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "retention_until": (finished + timedelta(days=RETENTION_DAYS)).isoformat(),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output, prefix=".preflight-result-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, output / "preflight-result.json")
        temporary = None
        lines = [
            f"{name}: {details['status']} (exit {details['exit_code']})"
            for name, details in preflight.items()
        ]
        (output / "preflight-output.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise GateError("unable to write preflight evidence") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_preflight(repo: Path, results_root: Path) -> ScanResult:
    sha = require_clean_revision(repo)
    output = _prepare_preflight_output(repo, results_root, sha)
    state_dir = output.parent / ".state" / "runtime-preflight"
    started = datetime.now(UTC)
    try:
        child_env = build_child_environment(
            state_dir, codex_state_dir=output.parent, protected_root_dir=results_root
        )
    except GateError:
        reasons = ["environment preparation failed"]
        finished = datetime.now(UTC)
        _write_preflight_evidence(
            output, sha, _empty_preflight(), [], 2, reasons, started, finished
        )
        return ScanResult(output, 2, None, False, reasons, None)
    try:
        preflight, reasons, exit_code = _execute_preflight(state_dir, child_env, output)
    except BaseException:
        cleanup_child_environment(child_env)
        raise
    finished = datetime.now(UTC)
    try:
        _write_preflight_evidence(
            output, sha, preflight, sorted(child_env), exit_code, reasons, started, finished
        )
    finally:
        cleanup_child_environment(child_env)
    return ScanResult(output, exit_code, None, not reasons, reasons, None)


def _record_scan_preflight_failure(
    output: Path,
    sha: str,
    shard_id: str,
    dry_run: bool,
    preflight: dict[str, dict[str, int | str | None]],
    present_environment_keys: list[str],
    reasons: list[str],
    exit_code: int,
    started: datetime,
) -> ScanResult:
    finished = datetime.now(UTC)
    runner = {
        "sha": sha,
        "shard": shard_id,
        "command": {**COMMAND_PROVENANCE, "dry_run": dry_run},
        "environment_policy_version": ENVIRONMENT_POLICY_VERSION,
        "present_environment_keys": present_environment_keys,
        "preflight": preflight,
        "scanner_started": False,
        "scanner_exit_code": None,
        "exit_code": exit_code,
        "passed": False,
        "reasons": reasons,
        "cost_usd": None,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "retention_until": (finished + timedelta(days=RETENTION_DAYS)).isoformat(),
        "artifact_paths": {
            "scan_manifest": "scan-manifest.json",
            "coverage": "coverage.json",
            "findings": "findings.json",
            "report": "report.md",
            "runner_output": "runner-output.log",
        },
    }
    _write_runner_result(output, runner)
    (output / "runner-output.log").write_text("\n".join(reasons) + "\n", encoding="utf-8")
    return ScanResult(output, exit_code, None, False, reasons, None)


def run_scan(
    repo: Path,
    results_root: Path,
    shard_id: str,
    *,
    dry_run: bool = False,
    profile: str = LEGACY_PROFILE_ID,
    authorization: Path | None = None,
    budget_ledger: Path | None = None,
) -> ScanResult:
    sha = require_clean_revision(repo)
    manifest = _load_repo_manifest(repo, sha)
    if shard_id not in _profile_units(profile, manifest):
        raise GateError(f"unknown shard: {shard_id}")
    output = Path(results_root).resolve() / sha / shard_id
    if (
        profile == MICRO_PROFILE_ID
        and _profile_config(profile, manifest)["activation"] == "offline_only"
    ):
        return ScanResult(output, 2, None, False, ["policy_not_activated"], None)
    assignments = _validate_repo_manifest(repo, profile, manifest)
    evidence: dict[str, Any] = {}
    claim: Path | None = None
    execution_root = results_root
    if profile == MICRO_PROFILE_ID:
        config = _profile_config(profile, manifest)
        if shard_id not in _authorized_micro_units(config):
            reason = (
                "pilot_unit_required"
                if config.get("activation") == "pilot_only"
                else "unit_not_authorized"
            )
            raise GateError(reason)
        evidence = validate_pilot_authorization(
            repo,
            results_root,
            sha,
            shard_id,
            assignments,
            authorization,
            budget_ledger,
            manifest,
        )
        assert authorization is not None
        if _pilot_claim_path(authorization, evidence).exists():
            raise GateError("attempt_already_claimed")
        if dry_run:
            execution_root = results_root / "dry-runs"
        output = execution_root.resolve() / sha / shard_id
    else:
        output = prepare_output(
            repo, results_root, sha, shard_id, profile=profile, manifest=manifest
        )
    state_dir = output.parent / ".state" / shard_id
    started = datetime.now(UTC)
    try:
        child_env = build_child_environment(
            state_dir, codex_state_dir=output.parent, protected_root_dir=results_root
        )
    except GateError:
        if profile == MICRO_PROFILE_ID:
            output = _pilot_failed_preflight_output(repo, results_root, sha, shard_id, manifest)
        return _record_scan_preflight_failure(
            output,
            sha,
            shard_id,
            dry_run,
            _empty_preflight(),
            [],
            ["environment preparation failed"],
            2,
            started,
        )
    try:
        preflight, preflight_reasons, preflight_exit = _execute_preflight(
            state_dir, child_env, output
        )
    except BaseException:
        cleanup_child_environment(child_env)
        raise
    if preflight_reasons:
        cleanup_child_environment(child_env, release_directory_locks=False)
        try:
            if profile == MICRO_PROFILE_ID:
                output = _pilot_failed_preflight_output(repo, results_root, sha, shard_id, manifest)
            return _record_scan_preflight_failure(
                output,
                sha,
                shard_id,
                dry_run,
                preflight,
                sorted(child_env),
                preflight_reasons,
                preflight_exit,
                started,
            )
        finally:
            cleanup_child_environment(child_env)
    try:
        if profile == MICRO_PROFILE_ID:
            assert authorization is not None and budget_ledger is not None
            if (
                validate_pilot_authorization(
                    repo,
                    results_root,
                    sha,
                    shard_id,
                    assignments,
                    authorization,
                    budget_ledger,
                    manifest,
                )
                != evidence
            ):
                raise GateError("authorization_invalid")
            if not dry_run:
                _pilot_check_output(output)
                claim = acquire_pilot_claim(authorization, evidence)
                _ensure_pilot_baseline(results_root, evidence, manifest)
            output = prepare_output(
                repo, execution_root, sha, shard_id, profile=profile, manifest=manifest
            )
        elif not dry_run:
            _ensure_baseline(results_root.resolve(), sha, shard_id)
        snapshot_root, scan_repo = _materialize_scan_snapshot(repo, sha, output.parent)
    except BaseException:
        cleanup_child_environment(child_env)
        raise
    scanner_limit_failure: str | None = None
    snapshot_integrity_failure = False
    try:
        snapshot_manifest = _load_repo_manifest(repo, sha)
        materialized_manifest = _parse_bounded_json(
            _bounded_file_bytes(scan_repo / "docs" / "security" / "d5-shards.json")
        )
        if snapshot_manifest != manifest:
            raise GateError("scan snapshot manifest mismatch")
        if materialized_manifest != manifest:
            raise GateError("scan snapshot manifest mismatch")
        argv = build_scanner_argv(
            scan_repo,
            shard_id,
            output,
            dry_run=dry_run,
            profile=profile,
            manifest=snapshot_manifest,
        )
        scanner_executable = shutil.which(
            "npx.cmd" if os.name == "nt" else "npx",
            path=child_env.get("PATH") or child_env.get("Path"),
        )
        if scanner_executable is None:
            raise GateError("npx executable is unavailable")
        runner_output_path = state_dir / "runner-output.log"
        try:
            scanner_exit_code, console = _run_bounded_scanner(
                argv,
                cwd=state_dir.resolve(),
                env=child_env,
                executable=scanner_executable,
                output_path=runner_output_path,
            )
        except GateError as exc:
            if str(exc) not in {
                "scanner_runtime_limit_exceeded",
                "scanner_output_limit_exceeded",
                "scanner_descendants_remaining",
                "scanner_output_reader_stalled",
            }:
                raise
            scanner_exit_code = 2
            scanner_limit_failure = str(exc)
            console = exc.output if isinstance(exc, BoundedScannerError) else ""
        finally:
            if runner_output_path.is_file():
                os.replace(runner_output_path, output / "runner-output.log")
    except BaseException:
        cleanup_child_environment(child_env)
        raise
    finally:
        try:
            snapshot_integrity_failure = require_clean_revision(scan_repo) != sha
        except GateError:
            snapshot_integrity_failure = True
        shutil.rmtree(snapshot_root, ignore_errors=True)
        cleanup_child_environment(child_env, release_directory_locks=False)
    if snapshot_integrity_failure:
        scanner_exit_code = 2
        scanner_limit_failure = "scan_snapshot_integrity_failed"
    finished = datetime.now(UTC)
    retention_until = finished + timedelta(days=RETENTION_DAYS)
    cost = _latest_cost(console)
    reasons: list[str] = [scanner_limit_failure] if scanner_limit_failure else []
    if dry_run and scanner_exit_code != 0:
        reasons.append("scanner_nonzero_exit")
    runner = {
        "sha": sha,
        "shard": shard_id,
        "command": {**_command_provenance(profile, manifest), "dry_run": dry_run},
        "environment_policy_version": ENVIRONMENT_POLICY_VERSION,
        "present_environment_keys": sorted(child_env),
        "preflight": preflight,
        "scanner_started": True,
        "scanner_exit_code": scanner_exit_code,
        "exit_code": 0,
        "passed": False,
        "reasons": [],
        "cost_usd": cost,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "retention_until": retention_until.isoformat(),
        "artifact_paths": {
            "scan_manifest": "scan-manifest.json",
            "coverage": "coverage.json",
            "findings": "findings.json",
            "report": "report.md",
            "runner_output": "runner-output.log",
        },
    }
    if profile == MICRO_PROFILE_ID:
        runner.update(
            {
                "profile": profile,
                "unit": shard_id,
                "profile_digest": evidence["profile_digest"],
                "inventory_digest": evidence["inventory_digest"],
                "assigned_paths": sorted(assignments[shard_id]),
                "session_capacity": 3,
                "max_cost_usd": MAX_COST_USD,
                "authorization_sha256": evidence["authorization_sha256"],
                "authorization_path": str(authorization),
                "budget_ledger_path": str(budget_ledger),
                "claim_path": str(claim) if claim else None,
                "budget_ledger_sha256": evidence["budget_ledger_sha256"],
                "claim_sha256": _sha256(claim) if claim else None,
            }
        )
        try:
            sealed = _strict_json_bytes(output / "scan-manifest.json")
            runner["scan_id"] = sealed["scan"]["id"]
        except (OSError, ValueError, TypeError, KeyError):
            runner["scan_id"] = None
    try:
        _write_runner_result(output, runner)
        if not dry_run:
            evaluation = evaluate_artifacts(
                output,
                shard=shard_id,
                expected_sha=sha,
                dispositions=None,
                require_dispositions=False,
                validate_runner_outcome=False,
                profile=profile,
                expected_inventory_digest=evidence.get("inventory_digest"),
                expected_assigned_paths=sorted(assignments[shard_id]) if evidence else None,
                profile_manifest=manifest,
            )
            reasons.extend(evaluation["reason_codes"])
        effective_exit = scanner_exit_code if scanner_exit_code != 0 else (0 if not reasons else 2)
        runner["exit_code"] = effective_exit
        runner["passed"] = not reasons
        runner["reasons"] = reasons
        _write_runner_result(output, runner)
    except BaseException:
        cleanup_child_environment(child_env)
        raise
    cleanup_child_environment(child_env)
    return ScanResult(
        output,
        effective_exit,
        cost,
        not reasons,
        reasons,
        cost if cost is not None else None,
    )


def _is_zero_exit(value: Any) -> bool:
    return type(value) is int and value == 0


def _valid_retention_boundary(runner: dict[str, Any]) -> bool:
    try:
        finished_at = datetime.fromisoformat(runner["finished_at"])
        retention_until = datetime.fromisoformat(runner["retention_until"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        finished_at.tzinfo is not None
        and retention_until.tzinfo is not None
        and retention_until - finished_at == timedelta(days=RETENTION_DAYS)
    )


def _preflight_passes(runner: dict[str, Any]) -> bool:
    policy_version = runner.get("environment_policy_version")
    if not isinstance(policy_version, str):
        return False
    required_checks = SUPPORTED_ENVIRONMENT_POLICY_VERSIONS.get(policy_version)
    if required_checks is None:
        return False
    if runner.get("scanner_started") is not True:
        return False
    preflight = runner.get("preflight")
    if not isinstance(preflight, dict):
        return False
    for name in required_checks:
        check = preflight.get(name)
        if (
            not isinstance(check, dict)
            or check.get("status") != "passed"
            or not _is_zero_exit(check.get("exit_code"))
        ):
            return False
    return True


def _sha256(path: Path) -> str:
    return _bounded_file_digest(path, MAX_JSON_ARTIFACT_BYTES)[0]


def _open_regular_file_no_follow(path: Path) -> int:
    """Open one regular file without following a final symlink/reparse point."""
    if _path_has_reparse_component(path):
        raise OSError("file path contains a link or reparse point")
    if os.name != "nt":
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError("file is not regular")
        return descriptor

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AttributeTagInfo(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,
        0x00000001,
        None,
        3,
        0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "unable to open evidence file")
    attributes = AttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)) or (
        attributes.attributes & (0x00000400 | 0x00000010)
    ):
        kernel32.CloseHandle(handle)
        raise OSError("evidence path is a reparse point or directory")
    return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)


def _bounded_file_digest(path: Path, max_bytes: int) -> tuple[str, int]:
    descriptor = _open_regular_file_no_follow(path)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0 or before.st_size > max_bytes:
            raise OSError("evidence file exceeds its size limit")
        total = 0
        while chunk := handle.read(min(64 * 1024, max_bytes - total + 1)):
            total += len(chunk)
            if total > max_bytes:
                raise OSError("evidence file exceeds its size limit")
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("evidence file changed while being verified")
    if _path_has_reparse_component(path):
        raise OSError("evidence path changed while being verified")
    return digest.hexdigest(), total


def _bounded_file_bytes(path: Path, max_bytes: int = MAX_JSON_ARTIFACT_BYTES) -> bytes:
    descriptor = _open_regular_file_no_follow(path)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise OSError("file exceeds its configured size limit")
        raw = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
        if (
            len(raw) > max_bytes
            or len(raw) != before.st_size
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            raise OSError("file changed or exceeds its configured size limit")
    if _path_has_reparse_component(path):
        raise OSError("file path changed while being verified")
    return raw


def _load_repo_manifest(repo: Path, revision: str | None = None) -> dict[str, Any]:
    path = Path(repo) / "docs" / "security" / "d5-shards.json"
    try:
        exact_revision = revision if revision is not None else require_clean_revision(repo)
        value = _parse_bounded_json(_git_blob(repo, exact_revision, "docs/security/d5-shards.json"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GateError(f"cannot read exact-revision manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise GateError("manifest schema_version must be 1.0")
    return value


_REASON_ORDER = (
    "invalid_arguments",
    "missing_required_artifact",
    "malformed_json",
    "duplicate_json_key",
    "unsupported_schema",
    "manifest_invalid",
    "target_mismatch",
    "scope_mismatch",
    "artifact_path_invalid",
    "artifact_missing",
    "artifact_hash_mismatch",
    "scan_id_mismatch",
    "coverage_invalid",
    "finding_invalid",
    "runner_invalid",
    "scanner_not_started",
    "scanner_nonzero_exit",
    "runner_outcome_failed",
    "cost_invalid",
    "cost_cap_hit",
    "budget_ledger_invalid",
    "retention_invalid",
    "scan_not_completed",
    "coverage_incomplete",
    "critical_or_high_finding",
    "dispositions_missing",
    "disposition_path_invalid",
    "disposition_invalid",
    "disposition_binding_invalid",
    "disposition_occurrence_invalid",
    "disposition_incomplete",
    "disposition_needs_evidence",
    "disposition_reviewer_untrusted",
)
_KNOWN_SEVERITIES = ("critical", "high", "medium", "low", "informational")
_SAFE_ARTIFACT = re.compile(r"^[^/\\:]+(?:/[^/\\:]+)*$")
_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_FINDING_ID = re.compile(r"^csf_[a-f0-9]{24}$")
_OCCURRENCE_ID = re.compile(r"^occ_[a-f0-9]{24}$")
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_SNAPSHOT = re.compile(r"^codex-security-snapshot/v1:sha256:[a-f0-9]{64}$")


class _DuplicateJSONKey(ValueError):
    pass


MAX_ARTIFACT_COUNT = 128
MAX_FINDING_COUNT = 10_000
MAX_SURFACE_COUNT = 256
MAX_DISPOSITION_EVIDENCE_BYTES = 1024 * 1024
MAX_DISPOSITION_ENTRY_COUNT = 1024
MAX_EVIDENCE_REFS_PER_ENTRY = 8
MAX_DISPOSITION_EVIDENCE_TOTAL_BYTES = 8 * 1024 * 1024


def _strict_json_document(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJSONKey(key)
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(value)

    def parse_decimal(value: str) -> Decimal:
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("JSON number exceeds parser limits") from exc

    text = raw.decode("utf-8")
    nesting = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            nesting += 1
            if nesting > MAX_JSON_NESTING:
                raise ValueError("JSON artifact nesting exceeds the configured limit")
        elif character in "]}":
            nesting -= 1
    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_float=parse_decimal,
            parse_constant=reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON artifact nesting exceeds parser limits") from exc


def _strict_json_bytes(path: Path, *, max_bytes: int = MAX_JSON_ARTIFACT_BYTES) -> Any:
    raw = _bounded_file_bytes(path, max_bytes)
    return _strict_json_document(raw)


def _safe_relative_artifact(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if "\\" in value or ":" in value or value.startswith(("/", "//")):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts) and bool(
        _SAFE_ARTIFACT.fullmatch(value)
    )


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _protected_worktree_roots() -> list[Path]:
    """Discover protected checkout roots using only Git metadata files."""
    current = Path(__file__).resolve().parents[1]
    marker = current / ".git"
    try:
        if marker.is_dir():
            common = marker.resolve()
        elif marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                raise GateError("Git worktree marker is malformed")
            admin = Path(text.partition(":")[2].strip())
            admin = (current / admin).resolve() if not admin.is_absolute() else admin.resolve()
            common_file = admin / "commondir"
            if not common_file.is_file():
                raise GateError("Git common directory metadata is unavailable")
            common_text = common_file.read_text(encoding="utf-8").strip()
            if not common_text:
                raise GateError("Git common directory metadata is malformed")
            common = (admin / common_text).resolve()
            if admin.parent.resolve() != (common / "worktrees").resolve():
                raise GateError("Git common directory metadata is malformed")
        else:
            raise GateError("Git metadata is unavailable")
        if not common.is_dir():
            raise GateError("Git common directory is unavailable")
        roots = [current, common.parent.resolve()]
        linked_admin = common / "worktrees"
        if not linked_admin.exists():
            return list(dict.fromkeys(roots))
        if not linked_admin.is_dir():
            raise GateError("Git linked-worktree metadata is malformed")
        entries = list(linked_admin.iterdir())
        for entry in entries:
            if not entry.is_dir():
                raise GateError("Git linked-worktree metadata is malformed")
            gitdir = entry / "gitdir"
            text = gitdir.read_text(encoding="utf-8").strip()
            if not text:
                raise GateError("Git linked-worktree metadata is malformed")
            linked = Path(text)
            linked = (entry / linked).resolve() if not linked.is_absolute() else linked.resolve()
            if linked.name != ".git" or not linked.is_file():
                raise GateError("Git linked-worktree metadata is malformed")
            backlink = linked.read_text(encoding="utf-8").strip()
            if not backlink.startswith("gitdir:"):
                raise GateError("Git linked-worktree metadata is malformed")
            backlink_admin = Path(backlink.partition(":")[2].strip())
            backlink_admin = (
                (linked.parent / backlink_admin).resolve()
                if not backlink_admin.is_absolute()
                else backlink_admin.resolve()
            )
            if backlink_admin != entry.resolve():
                raise GateError("Git linked-worktree metadata is malformed")
            roots.append(linked.parent.resolve())
        return list(dict.fromkeys(roots))
    except (OSError, ValueError) as exc:
        raise GateError("unable to discover protected Git worktrees") from exc


def _outside_protected_roots(path: Path) -> bool:
    try:
        resolved = path.resolve()
        return all(
            resolved != root and root not in resolved.parents
            for root in _protected_worktree_roots()
        )
    except (GateError, OSError, ValueError):
        return False


def _descendant_file(output: Path, relative: str) -> Path | None:
    if not _safe_relative_artifact(relative):
        return None
    raw_candidate = Path(os.path.abspath(output / relative))
    if _path_has_reparse_component(raw_candidate):
        return None
    candidate = raw_candidate.resolve()
    base = output.resolve()
    if (
        candidate == base
        or base not in candidate.parents
        or not candidate.is_file()
        or candidate.stat().st_size > MAX_JSON_ARTIFACT_BYTES
    ):
        return None
    return candidate


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_nonempty_string(item) for item in value)


def _valid_optional_finding_fields(value: dict[str, Any]) -> bool:  # noqa: PLR0915
    writeup = value.get("writeup")
    if "writeup" in value and (
        not isinstance(writeup, dict)
        or not isinstance(writeup.get("reportPath"), str)
        or re.fullmatch(r"findings/([a-z0-9][a-z0-9._-]*)/\1\.md", writeup["reportPath"]) is None
    ):
        return False

    code_evidence = value.get("codeEvidence")
    if "codeEvidence" in value:
        if not isinstance(code_evidence, list):
            return False
        for item in code_evidence:
            if (
                not isinstance(item, dict)
                or not _matches(_IDENTITY, item.get("id"))
                or not all(
                    _nonempty_string(item.get(key))
                    for key in ("label", "path", "code", "explanation")
                )
                or type(item.get("startLine")) is not int
                or item["startLine"] < 1
            ):
                return False
            if "endLine" in item and (type(item["endLine"]) is not int or item["endLine"] < 1):
                return False
            if any(key in item and not _nonempty_string(item[key]) for key in ("language", "role")):
                return False

    legacy_code_evidence = value.get("code_evidence")
    if "code_evidence" in value:
        if legacy_code_evidence is not None and not isinstance(legacy_code_evidence, list):
            return False
        if isinstance(legacy_code_evidence, list) and any(
            not isinstance(item, dict)
            or not _nonempty_string(item.get("id"))
            or not _nonempty_string(item.get("code"))
            for item in legacy_code_evidence
        ):
            return False

    for key in ("rootCause", "root_cause"):
        if key not in value:
            continue
        item = value[key]
        allowed = (str, dict) if key == "rootCause" else (str, dict, type(None))
        if not isinstance(item, allowed):
            return False
        if isinstance(item, dict):
            if key == "rootCause" and not _nonempty_string(item.get("summary")):
                return False
            if "summary" in item and not _nonempty_string(item["summary"]):
                return False
            if any(
                field in item and not _nonempty_string_list(item[field])
                for field in ("evidenceRefs", "evidence_refs")
            ):
                return False
            if any(
                field in item and not _nonempty_string(item[field])
                for field in ("code", "language")
            ):
                return False

    validation = value.get("validation")
    if "validation" in value:
        if validation is not None and not isinstance(validation, dict):
            return False
        if isinstance(validation, dict):
            if any(
                key in validation and not _nonempty_string_list(validation[key])
                for key in (
                    "assertions",
                    "counterEvidence",
                    "evidenceRefs",
                    "evidence_refs",
                    "limitations",
                )
            ):
                return False
            evidence = validation.get("evidence")
            if "evidence" in validation and not (
                _nonempty_string(evidence) or _nonempty_string_list(evidence)
            ):
                return False
            if any(
                key in validation
                and validation[key] is not None
                and not _nonempty_string(validation[key])
                for key in ("method", "summary")
            ):
                return False
            if any(
                key in validation
                and validation[key] is not None
                and not _nonempty_string(validation[key])
                for key in ("status", "disposition", "result")
            ):
                return False

    attack = value.get("attackPath")
    if "attackPath" in value:
        if attack is not None and not isinstance(attack, dict):
            return False
        if isinstance(attack, dict):
            if any(
                key in attack and not _nonempty_string_list(attack[key])
                for key in (
                    "assumptions",
                    "blindspots",
                    "controls",
                    "evidenceRefs",
                    "evidence_refs",
                    "limitations",
                    "preconditions",
                    "steps",
                )
            ):
                return False
            for key in ("dataFlow", "data_flow", "dataflow"):
                if key in attack and not _valid_attack_object(attack[key], _ATTACK_FLOW_FIELDS):
                    return False
            for key in ("impact", "likelihood"):
                if key in attack and not _valid_attack_object(
                    attack[key], _ATTACK_RATING_FIELDS, allow_none=True
                ):
                    return False
            if "reachability" in attack and not _valid_attack_object(
                attack["reachability"], _ATTACK_REACHABILITY_FIELDS
            ):
                return False
            if any(key in attack and not _nonempty_string(attack[key]) for key in ("summary",)):
                return False

    for key in ("remediationTests", "preventiveControls"):
        if key in value and not _nonempty_string_list(value[key]):
            return False

    extensions = value.get("extensions")
    if "extensions" in value:
        if not isinstance(extensions, dict):
            return False
        if any(
            key in extensions and not _nonempty_string(extensions[key])
            for key in ("candidateId", "ledgerRowId", "reportId")
        ):
            return False
    return True


_ATTACK_FLOW_FIELDS = (
    "summary",
    "source",
    "sink",
    "outcome",
    "transformations",
    "evidenceRefs",
    "evidence_refs",
)
_ATTACK_RATING_FIELDS = ("level", "rationale", "why")
_ATTACK_REACHABILITY_FIELDS = (
    "summary",
    "attacker",
    "entrypoint",
    "source",
    "sink",
    "outcome",
    "preconditions",
    "evidenceRefs",
    "evidence_refs",
)


def _valid_attack_object(value: Any, fields: tuple[str, ...], *, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    if _nonempty_string(value):
        return True
    if not isinstance(value, dict):
        return False
    for key in fields:
        if key not in value:
            continue
        item = value[key]
        if key in {"transformations", "preconditions", "evidenceRefs", "evidence_refs"}:
            if not _nonempty_string_list(item):
                return False
        elif not _nonempty_string(item):
            return False
    return True


def _canonical_finding(value: Any) -> tuple[bool, str | None, str | None]:  # noqa: PLR0915
    if not isinstance(value, dict):
        return False, None, None
    if not (
        _matches(_FINDING_ID, value.get("findingId"))
        and _matches(_OCCURRENCE_ID, value.get("occurrenceId"))
    ):
        return False, None, None
    if not _matches(_IDENTITY, value.get("ruleId")):
        return False, None, None
    identity = value.get("identity")
    fingerprints = value.get("fingerprints")
    severity = value.get("severity")
    confidence = value.get("confidence")
    taxonomy = value.get("taxonomy")
    locations = value.get("locations")
    provenance = value.get("provenance")
    if not isinstance(identity, dict) or not _matches(_IDENTITY, identity.get("anchor")):
        return False, None, None
    if "instance" in identity and not _matches(_IDENTITY, identity.get("instance")):
        return False, None, None
    if not isinstance(fingerprints, dict) or fingerprints.get("algorithm") != "codex-security/v1":
        return False, None, None
    primary = fingerprints.get("primary")
    if not isinstance(primary, str) or not re.fullmatch(
        r"^codex-security/v1:sha256:[a-f0-9]{64}$", primary
    ):
        return False, None, None
    if not all(_nonempty_string(value.get(name)) for name in ("title", "summary", "remediation")):
        return False, None, None
    if not isinstance(severity, dict) or severity.get("level") not in _KNOWN_SEVERITIES:
        return False, None, None
    for key in ("score",):
        if key in severity and (
            isinstance(severity[key], bool)
            or not isinstance(severity[key], int | float)
            or not math.isfinite(float(severity[key]))
            or not 0 <= severity[key] <= 10
        ):
            return False, None, None
    for key in ("scoringSystem", "vector", "rationale", "changeConditions"):
        if key in severity and not _nonempty_string(severity[key]):
            return False, None, None
    if (
        not isinstance(confidence, dict)
        or not isinstance(confidence.get("level"), str)
        or confidence.get("level") not in ("high", "medium", "low")
        or not _nonempty_string(confidence.get("rationale"))
    ):
        return False, None, None
    if (
        not isinstance(taxonomy, dict)
        or not _nonempty_string(taxonomy.get("category"))
        or not isinstance(taxonomy.get("cwe"), list)
        or not all(_nonempty_string(item) for item in taxonomy["cwe"])
    ):
        return False, None, None
    if not isinstance(locations, list) or not locations:
        return False, None, None
    for location in locations:
        if not isinstance(location, dict) or not _safe_relative_artifact(location.get("path")):
            return False, None, None
        start = location.get("startLine")
        end = location.get("endLine", start)
        if type(start) is not int or start < 1 or type(end) is not int or end < start:
            return False, None, None
        if "role" in location and not _nonempty_string(location["role"]):
            return False, None, None
    if not isinstance(provenance, dict) or not _nonempty_string(provenance.get("source")):
        return False, None, None
    if not _valid_optional_finding_fields(value):
        return False, None, None
    return True, value["occurrenceId"], severity["level"]


def _validate_sidecar(
    path: Path,
    output: Path,
    expected_sha: str,
    shard: str,
    scan_id: str,
    findings_digest: str,
    medium_low: set[str],
    severity_by_occurrence: dict[str, str],
) -> set[str]:  # noqa: PLR0915
    codes: set[str] = set()
    resolved = path.resolve()
    output_resolved = output.resolve()
    if (
        output_resolved == resolved
        or output_resolved in resolved.parents
        or not _outside_protected_roots(resolved)
    ):
        return {"disposition_path_invalid"}
    if not resolved.is_file():
        return {"disposition_path_invalid"}
    try:
        sidecar = _strict_json_bytes(resolved)
    except _DuplicateJSONKey:
        return {"disposition_invalid"}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"disposition_invalid"}
    if not isinstance(sidecar, dict) or sidecar.get("schema_version") != "1.0":
        return {"disposition_invalid"}
    if (
        not isinstance(sidecar.get("sha"), str)
        or not _SHA40.fullmatch(sidecar["sha"])
        or sidecar.get("sha") != expected_sha
        or sidecar.get("shard") != shard
        or sidecar.get("scan_id") != scan_id
        or not isinstance(sidecar.get("findings_sha256"), str)
        or not _SHA64.fullmatch(sidecar["findings_sha256"])
        or sidecar.get("findings_sha256") != findings_digest
    ):
        codes.add("disposition_binding_invalid")
    entries = sidecar.get("findings")
    if not isinstance(entries, list) or len(entries) > MAX_DISPOSITION_ENTRY_COUNT:
        return codes | {"disposition_invalid"}
    seen: set[str] = set()
    digest_cache: dict[Path, tuple[str, int]] = {}
    evidence_total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            codes.add("disposition_occurrence_invalid")
            continue
        occurrence = entry.get("occurrenceId")
        if not isinstance(occurrence, str) or occurrence in seen or occurrence not in medium_low:
            codes.add("disposition_occurrence_invalid")
            continue
        seen.add(occurrence)
        classification = entry.get("classification")
        if not isinstance(classification, str) or classification not in (
            "confirmed",
            "not_reproduced",
            "needs_evidence",
        ):
            codes.add("disposition_invalid")
        if not _nonempty_string(entry.get("rationale")) or not _nonempty_string(
            entry.get("remediation_acceptance")
        ):
            codes.add("disposition_incomplete")
        if entry.get("classification") == "not_reproduced":
            codes.add("disposition_reviewer_untrusted")
            if not _nonempty_string(entry.get("reviewer")):
                codes.add("disposition_incomplete")
            evidence_refs = entry.get("evidenceRefs")
            if (
                not isinstance(evidence_refs, list)
                or not evidence_refs
                or len(evidence_refs) > MAX_EVIDENCE_REFS_PER_ENTRY
            ):
                codes.add("disposition_incomplete")
            else:
                entry_evidence_paths: set[Path] = set()
                for evidence_ref in evidence_refs:
                    if not isinstance(evidence_ref, dict):
                        codes.add("disposition_incomplete")
                        continue
                    evidence_name = evidence_ref.get("path")
                    expected_digest = evidence_ref.get("sha256")
                    if (
                        not isinstance(evidence_name, str)
                        or not _safe_relative_artifact(evidence_name)
                        or not _matches(_SHA64, expected_digest)
                    ):
                        codes.add("disposition_incomplete")
                        continue
                    evidence_path = resolved.parent / evidence_name
                    if _path_has_reparse_component(evidence_path) or not _outside_protected_roots(
                        evidence_path
                    ):
                        codes.add("disposition_incomplete")
                        continue
                    try:
                        canonical_evidence = evidence_path.resolve(strict=True)
                        canonical_evidence.relative_to(resolved.parent.resolve(strict=True))
                        if not _outside_protected_roots(canonical_evidence):
                            raise OSError("evidence path resolves outside its sidecar directory")
                        if canonical_evidence in entry_evidence_paths:
                            codes.add("disposition_incomplete")
                            continue
                        entry_evidence_paths.add(canonical_evidence)
                        cached = digest_cache.get(canonical_evidence)
                        if cached is None:
                            remaining = MAX_DISPOSITION_EVIDENCE_TOTAL_BYTES - evidence_total
                            digest, size = _bounded_file_digest(
                                canonical_evidence,
                                min(MAX_DISPOSITION_EVIDENCE_BYTES, remaining),
                            )
                            evidence_total += size
                            cached = (digest, size)
                            digest_cache[canonical_evidence] = cached
                        if cached[0] != expected_digest:
                            codes.add("disposition_incomplete")
                    except OSError:
                        codes.add("disposition_incomplete")
        if entry.get("classification") == "needs_evidence":
            codes.add("disposition_needs_evidence")
        if occurrence in medium_low:
            severity = severity_by_occurrence.get(occurrence)
            if severity == "medium":
                if not all(
                    _nonempty_string(entry.get(key)) for key in ("owner", "disposition", "due_date")
                ):
                    codes.add("disposition_incomplete")
                else:
                    try:
                        parsed_due = date.fromisoformat(entry["due_date"])
                    except (TypeError, ValueError):
                        codes.add("disposition_incomplete")
                    else:
                        if parsed_due.isoformat() != entry["due_date"]:
                            codes.add("disposition_incomplete")
                if not (
                    _nonempty_string(entry.get("compensating_control"))
                    or _nonempty_string(entry.get("risk_acceptance"))
                ):
                    codes.add("disposition_incomplete")
            elif severity == "low" and not _nonempty_string(entry.get("backlog_reference")):
                codes.add("disposition_incomplete")
    if seen != medium_low:
        codes.add("disposition_occurrence_invalid")
    return codes


def _evaluate_runner_document(
    runner: Any,
    *,
    expected_sha: str,
    shard: str,
    profile: str,
    validate_runner_outcome: bool,
    expected_inventory_digest: str | None = None,
    expected_assigned_paths: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[set[str], Decimal | None]:
    flags: set[str] = set()
    cost_usd: Decimal | None = None
    if not isinstance(runner, dict):
        return {"runner_invalid"}, None
    if (
        runner.get("sha") != expected_sha
        or runner.get("shard") != shard
        or runner.get("command") != _command_provenance(profile, manifest)
        or runner.get("environment_policy_version") not in SUPPORTED_ENVIRONMENT_POLICY_VERSIONS
    ):
        flags.add("runner_invalid")
    if profile == MICRO_PROFILE_ID:
        config = _profile_config(profile, manifest)
        if any(
            not _matches(_SHA64, runner.get(key))
            for key in ("authorization_sha256", "budget_ledger_sha256", "claim_sha256")
        ):
            flags.add("runner_invalid")
        assigned_paths = runner.get("assigned_paths")
        valid_assigned_paths = isinstance(assigned_paths, list) and all(
            isinstance(path, str) and bool(path) for path in assigned_paths
        )
        if (
            runner.get("profile") != profile
            or runner.get("unit") != shard
            or runner.get("unit") not in _authorized_micro_units(config)
            or (
                runner.get("profile_digest") != profile_digest(profile, manifest)
                and runner.get("profile_digest") not in LEGACY_MICRO_PROFILE_DIGESTS
            )
            or not _matches(_SHA64, runner.get("inventory_digest"))
            or runner.get("session_capacity") != config.get("session_capacity")
            or runner.get("max_cost_usd") != config.get("max_cost_usd")
            or not valid_assigned_paths
            or (
                isinstance(assigned_paths, list)
                and valid_assigned_paths
                and assigned_paths != sorted(assigned_paths)
            )
        ):
            flags.add("runner_invalid")
        if (
            expected_inventory_digest is not None
            and runner.get("inventory_digest") != expected_inventory_digest
        ):
            flags.add("runner_invalid")
        if (
            expected_assigned_paths is not None
            and runner.get("assigned_paths") != expected_assigned_paths
        ):
            flags.add("runner_invalid")
    if runner.get("scanner_started") is not True:
        flags.add("scanner_not_started")
    if not _is_zero_exit(runner.get("scanner_exit_code")):
        flags.add("scanner_nonzero_exit")
    if not _preflight_passes(runner):
        flags.add("runner_invalid")
    effective_exit = runner.get("exit_code")
    passed = runner.get("passed")
    reasons = runner.get("reasons")
    if (
        type(effective_exit) is not int
        or not isinstance(passed, bool)
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
    ):
        flags.add("runner_invalid")
    elif validate_runner_outcome and (effective_exit != 0 or passed is not True or reasons != []):
        flags.add("runner_outcome_failed")
    cost = _decimal_cost(runner.get("cost_usd"))
    if cost is None:
        flags.add("cost_invalid")
    else:
        cost_usd = cost
        max_cost = _decimal_cost(
            _profile_config(profile, manifest).get("max_cost_usd", MAX_COST_USD)
        )
        if max_cost is None or cost_usd >= max_cost:
            flags.add("cost_cap_hit")
    if not _valid_retention_boundary(runner):
        flags.add("retention_invalid")
    return flags, cost_usd


def evaluate_artifacts(
    output: Path,
    *,
    shard: str,
    expected_sha: str,
    dispositions: Path | None,
    require_dispositions: bool,
    validate_runner_outcome: bool,
    profile: str = LEGACY_PROFILE_ID,
    expected_inventory_digest: str | None = None,
    expected_assigned_paths: list[str] | None = None,
    profile_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:  # noqa: PLR0915
    """The sole read-only authority for D.5 artifact disposition decisions."""
    scope_manifest = profile_manifest
    flags: set[str] = set()
    scan_id: str | None = None
    scan_status: str | None = None
    cost_usd: Decimal | None = None
    counts: dict[str, int] = {}
    findings_by_occurrence: dict[str, str] = {}
    output = Path(output)
    if (
        not isinstance(expected_sha, str)
        or not _SHA40.fullmatch(expected_sha)
        or shard not in _profile_units(profile, profile_manifest)
    ):
        flags.add("invalid_arguments")
    required = (
        "runner-result.json",
        "scan-manifest.json",
        "coverage.json",
        "findings.json",
        "report.md",
    )
    fixed_files: dict[str, Path] = {}
    for name in required:
        candidate = _descendant_file(output, name)
        if candidate is not None:
            fixed_files[name] = candidate
        elif (output / name).exists() or (output / name).is_symlink():
            flags.add("artifact_path_invalid")
        else:
            flags.add("missing_required_artifact")
    runner: Any = None
    if "runner-result.json" in fixed_files:
        try:
            runner = _strict_json_bytes(fixed_files["runner-result.json"])
        except _DuplicateJSONKey:
            flags.add("duplicate_json_key")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            flags.add("malformed_json")
        runner_flags, cost_usd = _evaluate_runner_document(
            runner,
            expected_sha=expected_sha,
            shard=shard,
            profile=profile,
            validate_runner_outcome=validate_runner_outcome,
            expected_inventory_digest=expected_inventory_digest,
            expected_assigned_paths=expected_assigned_paths,
            manifest=profile_manifest,
        )
        flags.update(runner_flags)
        if profile == MICRO_PROFILE_ID and not _pilot_attempt_bindings_valid(
            output, runner, profile_manifest
        ):
            flags.add("runner_invalid")
    if flags.intersection({"missing_required_artifact", "artifact_path_invalid"}):
        return {
            "contract_valid": False,
            "execution_pass": False,
            "governance_pass": False,
            "gate_pass": False,
            "scan_id": None,
            "scan_status": None,
            "cost_usd": cost_usd,
            "severity_counts": {},
            "reason_codes": [code for code in _REASON_ORDER if code in flags],
        }
    manifest: Any = None
    coverage: Any = None
    findings_doc: Any = None
    raw_findings: bytes | None = None
    try:
        manifest = _strict_json_bytes(fixed_files["scan-manifest.json"])
        coverage = _strict_json_bytes(fixed_files["coverage.json"])
        raw_findings = _bounded_file_bytes(fixed_files["findings.json"])
        findings_doc = _strict_json_document(raw_findings)
    except _DuplicateJSONKey:
        flags.add("duplicate_json_key")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        flags.add("malformed_json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("documentType") != "codex-security.scan-manifest"
        or manifest.get("schemaVersion") != "1.0"
    ):
        flags.add("manifest_invalid")
    if isinstance(manifest, dict) and manifest.get("schemaVersion") != "1.0":
        flags.add("unsupported_schema")
    scan = manifest.get("scan") if isinstance(manifest, dict) else None
    if not isinstance(scan, dict):
        flags.add("manifest_invalid")
        scan = {}
    scan_id_value = scan.get("id")
    if _nonempty_string(scan_id_value):
        scan_id = scan_id_value
    else:
        flags.add("manifest_invalid")
    if (
        profile == MICRO_PROFILE_ID
        and isinstance(runner, dict)
        and runner.get("scan_id") != scan_id
    ):
        flags.add("scan_id_mismatch")
    producer = scan.get("producer")
    if (
        not isinstance(producer, dict)
        or producer.get("name") != "codex-security-plugin"
        or producer.get("version") != "0.1.22"
    ):
        flags.add("manifest_invalid")
    scan_status_value = scan.get("status")
    if isinstance(scan_status_value, str) and scan_status_value in (
        "completed",
        "failed",
        "canceled",
        "interrupted",
    ):
        scan_status = scan_status_value
    else:
        flags.add("manifest_invalid")
    started = _timestamp(scan.get("startedAt"))
    completed = _timestamp(scan.get("completedAt"))
    sealed = _timestamp(scan.get("sealedAt"))
    if not started or not completed or not sealed or not (started <= completed <= sealed):
        flags.add("manifest_invalid")
    target = scan.get("target")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "git_revision"
        or not _nonempty_string(target.get("targetId"))
        or not _nonempty_string(target.get("displayName"))
        or target.get("revision") != expected_sha
    ):
        flags.add("target_mismatch")
    scope = scan.get("scope")
    expected_paths = _profile_units(profile, scope_manifest).get(shard, {}).get("paths")
    if (
        shard not in _profile_units(profile, scope_manifest)
        or not isinstance(scope, dict)
        or scope.get("includePaths") != expected_paths
        or scope.get("excludePaths") != []
    ):
        flags.add("scope_mismatch")
    if scan.get("coverageRef") != "coverage.json" or scan.get("findingsRef") != "findings.json":
        flags.add("manifest_invalid")
    artifacts = scan.get("artifacts")
    artifact_paths: set[str] = set()
    artifact_files: dict[str, Path] = {}
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACT_COUNT:
        flags.add("manifest_invalid")
    else:
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not _safe_relative_artifact(artifact.get("path")):
                flags.add("artifact_path_invalid")
                continue
            path_text = artifact["path"]
            folded = path_text.casefold()
            if (
                folded in {item.casefold() for item in artifact_paths}
                or path_text == "scan-manifest.json"
            ):
                flags.add("artifact_path_invalid")
            artifact_paths.add(path_text)
            candidate = _descendant_file(output, path_text)
            if candidate is None:
                flags.add(
                    "artifact_missing"
                    if _safe_relative_artifact(path_text)
                    else "artifact_path_invalid"
                )
                continue
            artifact_files[path_text] = candidate
            digest = artifact.get("sha256")
            try:
                actual_digest = _sha256(candidate)
            except (OSError, ValueError):
                actual_digest = None
            if (
                not isinstance(digest, str)
                or not _SHA64.fullmatch(digest)
                or digest != actual_digest
            ):
                flags.add("artifact_hash_mismatch")
            if not _nonempty_string(artifact.get("mediaType")) or (
                path_text in {"coverage.json", "findings.json"}
                and artifact.get("mediaType") != "application/json"
            ):
                flags.add("manifest_invalid")
        if artifact_paths.intersection({"coverage.json", "findings.json"}) != {
            "coverage.json",
            "findings.json",
        }:
            flags.add("manifest_invalid")
    if (
        not isinstance(coverage, dict)
        or coverage.get("documentType") != "codex-security.coverage"
        or coverage.get("schemaVersion") != "1.0"
    ):
        flags.add("coverage_invalid")
    if isinstance(coverage, dict):
        if not _nonempty_string(coverage.get("scanId")) or coverage.get("scanId") != scan_id:
            flags.add("scan_id_mismatch")
        if (
            coverage.get("mode") != "scoped_path"
            or coverage.get("inventoryStrategy") != "scoped_path"
            or coverage.get("includePaths") != expected_paths
            or coverage.get("excludePaths") != []
        ):
            flags.add("scope_mismatch")
        completeness = coverage.get("completeness")
        if not isinstance(completeness, str) or completeness not in (
            "complete",
            "partial",
            "unknown",
        ):
            flags.add("coverage_invalid")
        explicit_exclusions = coverage.get("explicitExclusions")
        if (
            not isinstance(coverage.get("surfaces"), list)
            or len(coverage.get("surfaces", [])) > MAX_SURFACE_COUNT
            or not isinstance(explicit_exclusions, list)
            or any(
                not (
                    (isinstance(note, str) and bool(note.strip()))
                    or (
                        isinstance(note, dict)
                        and _nonempty_string(note.get("pattern"))
                        and _nonempty_string(note.get("reason"))
                    )
                )
                for note in explicit_exclusions
            )
            or not isinstance(coverage.get("deferred"), list)
        ):
            flags.add("coverage_invalid")
        deferred = coverage.get("deferred")
        if isinstance(deferred, list):
            deferred_ids: set[str] = set()
            for item in deferred:
                if (
                    not isinstance(item, dict)
                    or not _nonempty_string(item.get("id"))
                    or not _nonempty_string(item.get("reason"))
                    or (
                        "paths" in item
                        and (
                            not isinstance(item["paths"], list)
                            or not all(_nonempty_string(path) for path in item["paths"])
                        )
                    )
                    or (
                        "surfaceIds" in item
                        and (
                            not isinstance(item["surfaceIds"], list)
                            or not all(
                                _nonempty_string(surface_id) for surface_id in item["surfaceIds"]
                            )
                        )
                    )
                ):
                    flags.add("coverage_invalid")
                    continue
                if item["id"] in deferred_ids:
                    flags.add("coverage_invalid")
                deferred_ids.add(item["id"])
            if deferred:
                flags.add("coverage_incomplete")
        if "openQuestions" in coverage:
            questions = coverage.get("openQuestions")
            if not isinstance(questions, list) or any(
                not isinstance(question, dict)
                or not _nonempty_string(question.get("question"))
                or (
                    "followUpPrompt" in question
                    and not _nonempty_string(question.get("followUpPrompt"))
                )
                for question in questions
            ):
                flags.add("coverage_invalid")
        surfaces = coverage.get("surfaces")
        if isinstance(surfaces, list):
            expected_path_set = (
                set(expected_assigned_paths) if expected_assigned_paths is not None else None
            )
            covered_paths: set[str] = set()
            covered_receipts: set[str] = set()
            if expected_path_set is not None and (
                coverage.get("coveredPaths") != sorted(expected_path_set)
            ):
                flags.add("coverage_invalid")
            if completeness == "complete" and not surfaces:
                flags.add("coverage_invalid")
            surface_ids: set[str] = set()
            for surface in surfaces:
                disposition = surface.get("disposition") if isinstance(surface, dict) else None
                receipt_refs = surface.get("receiptRefs") if isinstance(surface, dict) else None
                surface_paths = surface.get("paths") if isinstance(surface, dict) else None
                if isinstance(receipt_refs, str):
                    receipt_refs = [] if receipt_refs == "" else None
                if (
                    not isinstance(surface, dict)
                    or not _nonempty_string(surface.get("id"))
                    or not _nonempty_string(surface.get("label"))
                    or not isinstance(disposition, str)
                    or disposition
                    not in (
                        "reported",
                        "no_issue_found",
                        "rejected",
                        "not_applicable",
                        "needs_follow_up",
                    )
                    or not isinstance(receipt_refs, list)
                    or (completeness == "complete" and not receipt_refs)
                    or not all(isinstance(ref, str) for ref in receipt_refs)
                    or (completeness == "complete" and not _nonempty_string(surface.get("notes")))
                    or (
                        expected_path_set is not None
                        and (
                            not isinstance(surface_paths, list)
                            or (expected_path_set and len(surface_paths) != 1)
                            or not all(isinstance(path, str) for path in surface_paths)
                            or any(path not in expected_path_set for path in surface_paths)
                            or (completeness == "complete" and not receipt_refs)
                            or (expected_path_set and len(receipt_refs) != 1)
                        )
                    )
                ):
                    flags.add("coverage_invalid")
                    continue
                if surface["id"] in surface_ids or (
                    coverage.get("completeness") == "complete"
                    and surface["disposition"] == "needs_follow_up"
                ):
                    flags.add("coverage_invalid")
                if any(
                    not _safe_relative_artifact(ref)
                    or not ref.startswith("artifacts/")
                    or ref not in artifact_paths
                    for ref in receipt_refs
                ):
                    flags.add("coverage_invalid")
                if expected_path_set is not None and isinstance(surface_paths, list):
                    for path in surface_paths:
                        if path in covered_paths:
                            flags.add("coverage_invalid")
                        covered_paths.add(path)
                    for receipt_ref in receipt_refs:
                        if receipt_ref in covered_receipts:
                            flags.add("coverage_invalid")
                        covered_receipts.add(receipt_ref)
                surface_ids.add(surface["id"])
            if (
                expected_path_set is not None
                and completeness == "complete"
                and (
                    covered_paths != expected_path_set
                    or len(covered_receipts) != len(expected_path_set)
                )
            ):
                flags.add("coverage_invalid")
    if (
        not isinstance(findings_doc, dict)
        or findings_doc.get("documentType") != "codex-security.findings"
        or findings_doc.get("schemaVersion") != "1.0"
        or not isinstance(findings_doc.get("findings"), list)
        or len(findings_doc.get("findings", [])) > MAX_FINDING_COUNT
    ):
        flags.add("finding_invalid")
    if isinstance(findings_doc, dict) and (
        not _nonempty_string(findings_doc.get("scanId")) or findings_doc.get("scanId") != scan_id
    ):
        flags.add("scan_id_mismatch")
    if isinstance(findings_doc, dict) and isinstance(findings_doc.get("findings"), list):
        finding_ids: set[str] = set()
        primary_ids: set[str] = set()
        for item in findings_doc["findings"]:
            valid, occurrence, severity = _canonical_finding(item)
            if not valid or occurrence is None or severity is None:
                flags.add("finding_invalid")
                continue
            if (
                item["findingId"] in finding_ids
                or occurrence in findings_by_occurrence
                or item["fingerprints"]["primary"] in primary_ids
            ):
                flags.add("finding_invalid")
            finding_ids.add(item["findingId"])
            findings_by_occurrence[occurrence] = severity
            primary_ids.add(item["fingerprints"]["primary"])
            counts[severity] = counts.get(severity, 0) + 1
    if (
        not findings_by_occurrence
        and isinstance(findings_doc, dict)
        and findings_doc.get("findings") not in ([], None)
        and "finding_invalid" in flags
    ):
        counts = {}
    medium_low = {
        occurrence
        for occurrence, severity in findings_by_occurrence.items()
        if severity in {"medium", "low"}
    }
    for severity in findings_by_occurrence.values():
        if severity in {"critical", "high"}:
            flags.add("critical_or_high_finding")
    if scan_status != "completed":
        flags.add("scan_not_completed")
    if isinstance(coverage, dict) and coverage.get("completeness") != "complete":
        flags.add("coverage_incomplete")
    if (
        "finding_invalid" not in flags
        and dispositions is not None
        and raw_findings is not None
        and scan_id is not None
    ):
        try:
            resolved_disposition = dispositions.resolve()
            output_resolved = output.resolve()
            if (
                resolved_disposition == output_resolved
                or output_resolved in resolved_disposition.parents
                or not _outside_protected_roots(resolved_disposition)
            ):
                flags.add("disposition_path_invalid")
            elif not resolved_disposition.exists():
                flags.add("dispositions_missing")
            else:
                flags.update(
                    _validate_sidecar(
                        dispositions,
                        output,
                        expected_sha,
                        shard,
                        scan_id,
                        hashlib.sha256(raw_findings).hexdigest(),
                        medium_low,
                        findings_by_occurrence,
                    )
                )
        except (OSError, ValueError, TypeError):
            flags.add("disposition_invalid")
    elif "finding_invalid" not in flags and medium_low and require_dispositions:
        flags.add("dispositions_missing")
    contract_blockers = {
        "invalid_arguments",
        "missing_required_artifact",
        "malformed_json",
        "duplicate_json_key",
        "unsupported_schema",
        "manifest_invalid",
        "target_mismatch",
        "scope_mismatch",
        "artifact_path_invalid",
        "artifact_missing",
        "artifact_hash_mismatch",
        "scan_id_mismatch",
        "coverage_invalid",
        "finding_invalid",
    }
    contract_valid = not flags.intersection(contract_blockers)
    execution_blockers = contract_blockers | {
        "runner_invalid",
        "scanner_not_started",
        "scanner_nonzero_exit",
        "runner_outcome_failed",
        "cost_invalid",
        "cost_cap_hit",
        "retention_invalid",
        "scan_not_completed",
        "coverage_incomplete",
    }
    execution_pass = not flags.intersection(execution_blockers)
    governance_pass = not flags.intersection(
        {
            "invalid_arguments",
            "finding_invalid",
            "critical_or_high_finding",
            "dispositions_missing",
            "disposition_path_invalid",
            "disposition_invalid",
            "disposition_binding_invalid",
            "disposition_occurrence_invalid",
            "disposition_incomplete",
            "disposition_needs_evidence",
            "disposition_reviewer_untrusted",
        }
    )
    reasons = [code for code in _REASON_ORDER if code in flags]
    return {
        "contract_valid": contract_valid,
        "execution_pass": execution_pass,
        "governance_pass": governance_pass,
        "gate_pass": contract_valid and execution_pass and governance_pass,
        "scan_id": scan_id,
        "scan_status": scan_status,
        "cost_usd": cost_usd,
        "severity_counts": counts
        if not flags.intersection({"finding_invalid", "malformed_json", "duplicate_json_key"})
        else {},
        "reason_codes": reasons,
    }


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = item
    return value


def inspect_artifacts(
    scan_dir: Path,
    expected_sha: str,
    shard: str,
    dispositions: Path | None = None,
    *,
    profile: str = LEGACY_PROFILE_ID,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Evaluate one external sealed scan directory without side effects."""
    if not _SHA40.fullmatch(expected_sha) or (profile == MICRO_PROFILE_ID and repo is None):
        return {
            "contract_valid": False,
            "execution_pass": False,
            "governance_pass": False,
            "gate_pass": False,
            "scan_id": None,
            "scan_status": None,
            "cost_usd": None,
            "severity_counts": {},
            "reason_codes": ["invalid_arguments"],
        }
    output = Path(scan_dir).resolve()
    repo_root = Path(__file__).resolve().parents[1]
    if (
        not output.is_dir()
        or output.name != shard
        or output.parent.name != expected_sha
        or output == repo_root
        or repo_root in output.parents
        or not _outside_protected_roots(output)
    ):
        return {
            "contract_valid": False,
            "execution_pass": False,
            "governance_pass": False,
            "gate_pass": False,
            "scan_id": None,
            "scan_status": None,
            "cost_usd": None,
            "severity_counts": {},
            "reason_codes": ["invalid_arguments"],
        }
    expected_inventory_digest: str | None = None
    expected_assigned_paths: list[str] | None = None
    manifest: dict[str, Any] | None = None
    if repo is not None:
        try:
            if require_clean_revision(repo) != expected_sha:
                raise GateError("inspection repository does not match expected SHA")
            manifest = _load_repo_manifest(repo, expected_sha)
            if shard not in profile_unit_ids(profile, manifest):
                raise GateError("inspection shard is not in exact-SHA manifest")
        except (GateError, OSError, ValueError):
            return {
                "contract_valid": False,
                "execution_pass": False,
                "governance_pass": False,
                "gate_pass": False,
                "scan_id": None,
                "scan_status": None,
                "cost_usd": None,
                "severity_counts": {},
                "reason_codes": ["invalid_arguments"],
            }
    if profile == MICRO_PROFILE_ID:
        try:
            assert repo is not None and manifest is not None
            assignments = _validate_repo_manifest(repo, profile, manifest)
            expected_assigned_paths = sorted(assignments[shard])
            expected_inventory_digest = _inventory_digest(
                sorted(file for paths in assignments.values() for file in paths)
            )
        except (GateError, OSError, ValueError):
            return {
                "contract_valid": False,
                "execution_pass": False,
                "governance_pass": False,
                "gate_pass": False,
                "scan_id": None,
                "scan_status": None,
                "cost_usd": None,
                "severity_counts": {},
                "reason_codes": ["invalid_arguments"],
            }
    return evaluate_artifacts(
        output,
        shard=shard,
        expected_sha=expected_sha,
        dispositions=dispositions,
        require_dispositions=True,
        validate_runner_outcome=True,
        profile=profile,
        expected_inventory_digest=expected_inventory_digest,
        expected_assigned_paths=expected_assigned_paths,
        profile_manifest=manifest,
    )


_RECEIPT_STATUSES = {"succeeded", "failed", "canceled", "interrupted"}


def _read_receipt_cost(path: Path) -> tuple[dict[str, Any], Decimal, str] | None:
    try:
        raw = _bounded_file_bytes(path)
        value = _strict_json_document(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if status is not None and (not isinstance(status, str) or status not in _RECEIPT_STATUSES):
        return None
    cost = _decimal_cost(value.get("cost_usd"))
    if cost is None:
        return None
    return value, cost, hashlib.sha256(raw).hexdigest()


def _validate_budget_ledger(
    path: Path,
    _evidence_root: Path,
    total_cost: Decimal | float,
    *,
    required_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    ledger_bytes: bytes | None = None,
) -> list[str]:
    try:
        if ledger_bytes is not None and len(ledger_bytes) > MAX_JSON_ARTIFACT_BYTES:
            raise ValueError("budget ledger exceeds its configured size limit")
        value = (
            _strict_json_document(ledger_bytes)
            if ledger_bytes is not None
            else _strict_json_bytes(path)
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return ["budget_ledger_invalid"]
    receipts = value.get("receipts") if isinstance(value, dict) else value
    if not isinstance(receipts, list):
        return ["budget_ledger_invalid"]
    seen: set[str] = set()
    summed = Decimal("0")
    listed: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return ["budget_ledger_invalid"]
        receipt_path = receipt.get("receipt_path")
        digest = receipt.get("receipt_sha256")
        cost = receipt.get("cost_usd")
        if (
            not isinstance(receipt_path, str)
            or not isinstance(digest, str)
            or _decimal_cost(cost) is None
            or not _SHA64.fullmatch(digest)
        ):
            return ["budget_ledger_invalid"]
        try:
            resolved = Path(receipt_path).resolve()
            if not _outside_protected_roots(resolved):
                return ["budget_ledger_invalid"]
            source = _read_receipt_cost(resolved)
        except (OSError, ValueError):
            return ["budget_ledger_invalid"]
        key = str(resolved)
        if key in seen or source is None:
            return ["budget_ledger_invalid"]
        source_value, source_cost, actual = source
        if actual != digest:
            return ["budget_ledger_invalid"]
        ledger_cost = _decimal_cost(cost)
        if ledger_cost is None or source_cost != ledger_cost:
            return ["budget_ledger_invalid"]
        seen.add(key)
        listed[key] = source_value
        summed += ledger_cost
    if required_receipts is None:
        expected = _decimal_cost(total_cost)
        if expected is None or summed != expected:
            return ["budget_ledger_invalid"]
    else:
        for required_path, required_fields in required_receipts.items():
            resolved_key = str(Path(required_path).resolve())
            source_fields = listed.get(resolved_key)
            if source_fields is None or any(
                source_fields.get(key) != value for key, value in required_fields.items()
            ):
                return ["budget_ledger_invalid"]
    return []


_PILOT_BASELINE_RECEIPT_COSTS = (
    Decimal("11.830354"),
    Decimal("11.817578"),
    Decimal("11.791004"),
)


def _pilot_budget_history(budget: Any) -> tuple[Decimal, list[Decimal]] | None:
    if not isinstance(budget, dict) or not isinstance(budget.get("receipts"), list):
        return None
    costs: list[Decimal] = []
    for item in budget["receipts"]:
        if not isinstance(item, dict):
            return None
        cost = _decimal_cost(item.get("cost_usd"))
        if cost is None:
            return None
        costs.append(cost)
    if len(costs) < len(_PILOT_BASELINE_RECEIPT_COSTS) or any(
        costs.count(required) < 1 for required in _PILOT_BASELINE_RECEIPT_COSTS
    ):
        return None
    return sum(costs, Decimal("0")), costs


def _pilot_budget_ceiling(budget: Any, config: Mapping[str, Any]) -> tuple[bool, Decimal | None]:
    """Bind the ledger to either an explicit finite ceiling or explicit uncapped mode."""
    if (
        not isinstance(budget, dict)
        or "ceiling_usd" not in budget
        or "operational_ceiling_usd" not in config
    ):
        return False, None
    ledger_value = budget["ceiling_usd"]
    configured_value = config["operational_ceiling_usd"]
    if ledger_value is None and configured_value is None:
        return True, None
    ceiling = _decimal_cost(ledger_value)
    configured_ceiling = _decimal_cost(configured_value)
    if ceiling is None or configured_ceiling is None or ceiling > configured_ceiling:
        return False, None
    return True, ceiling


def _pilot_attempt_bindings_valid(
    output: Path, runner: Any, manifest: dict[str, Any] | None = None
) -> bool:
    if not isinstance(runner, dict):
        return False
    documents: dict[str, Any] = {}
    raw_documents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    try:
        for kind in ("authorization", "budget_ledger", "claim"):
            value = runner.get(f"{kind}_path")
            if not isinstance(value, str):
                return False
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path.resolve()) != value
                or not _outside_protected_roots(path)
            ):
                return False
            raw = _bounded_file_bytes(path)
            if hashlib.sha256(raw).hexdigest() != runner.get(f"{kind}_sha256"):
                return False
            document = _strict_json_document(raw)
            if not isinstance(document, dict):
                return False
            documents[kind], paths[kind] = document, path
            raw_documents[kind] = raw
        auth, claim = documents["authorization"], documents["claim"]
        if any(
            auth.get(key) != runner.get(key)
            for key in (
                "sha",
                "profile",
                "profile_digest",
                "inventory_digest",
                "unit",
                "budget_ledger_path",
                "budget_ledger_sha256",
            )
        ):
            return False
        command = {key: value for key, value in COMMAND_PROVENANCE.items() if key != "dry_run"}
        command.update({"session_capacity": 3, "outer_concurrency": 1, "max_cost_usd": "15.000000"})
        if (
            auth.get("schema_version") != "1.0"
            or auth.get("authorized_action")
            != (
                "unit_scan"
                if _profile_config(MICRO_PROFILE_ID, manifest).get("activation") == "active"
                else "pilot_scan"
            )
            or auth.get("command") != command
            or not _nonempty_string(auth.get("authorization_id"))
            or runner.get("unit")
            not in _authorized_micro_units(_profile_config(MICRO_PROFILE_ID, manifest))
            or auth.get("authorized_action")
            != (
                "unit_scan"
                if _profile_config(MICRO_PROFILE_ID, manifest).get("activation") == "active"
                else "pilot_scan"
            )
            or runner.get("profile_digest") != profile_digest(MICRO_PROFILE_ID, manifest)
            and runner.get("profile_digest") not in LEGACY_MICRO_PROFILE_DIGESTS
        ):
            return False
        root = output.parent.parent.resolve()
        if auth.get("results_root") != str(root):
            return False
        if any(root == path or root in path.parents for path in paths.values()):
            return False
        if any(claim.get(key) != value for key, value in auth.items()):
            return False
        if claim.get("authorization_sha256") != runner["authorization_sha256"] or not _timestamp(
            claim.get("created_at")
        ):
            return False
        if paths["claim"] != _pilot_claim_path(paths["authorization"], claim):
            return False
        budget = documents["budget_ledger"]
        history = _pilot_budget_history(budget)
        ceiling_valid, ceiling = _pilot_budget_ceiling(
            budget, _profile_config(MICRO_PROFILE_ID, manifest)
        )
        if (
            budget.get("schema_version") != "1.0"
            or _decimal_cost(budget.get("reservation_usd")) != Decimal("15")
            or not ceiling_valid
            or history is None
            or _validate_budget_ledger(
                paths["budget_ledger"],
                root,
                history[0],
                ledger_bytes=raw_documents["budget_ledger"],
            )
        ):
            return False
        reservation = _decimal_cost(budget["reservation_usd"])
        return reservation is not None and (ceiling is None or history[0] + reservation <= ceiling)
    except (OSError, ValueError, TypeError, KeyError, GateError):
        return False


def _pilot_external_path(value: Any, repo: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise GateError("authorization_invalid")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise GateError("authorization_invalid")
    if not _outside_protected_roots(path) or any(
        path == root or root in path.parents for root in _worktree_roots(repo)
    ):
        raise GateError("authorization_invalid")
    return path


def _pilot_check_output(output: Path) -> None:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise GateError("refusing non-empty output directory")


def _pilot_failed_preflight_output(
    repo: Path, root: Path, sha: str, unit: str, manifest: dict[str, Any] | None = None
) -> Path:
    diagnostics = root / "preflight-failures"
    _pilot_external_path(str(diagnostics), repo)
    diagnostics.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=diagnostics))
    return prepare_output(repo, attempt, sha, unit, profile=MICRO_PROFILE_ID, manifest=manifest)


def validate_pilot_authorization(
    repo: Path,
    results_root: Path,
    sha: str,
    unit: str,
    assignments: dict[str, list[str]],
    authorization: Path | None,
    budget_ledger: Path | None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read immutable owner bindings; never issue authorization or write evidence."""
    if authorization is None or budget_ledger is None:
        raise GateError("authorization_required")
    try:
        auth_path = _pilot_external_path(str(authorization), repo)
        ledger_path = _pilot_external_path(str(budget_ledger), repo)
        root = _pilot_external_path(str(results_root), repo)
        claims = (auth_path.parent / "claims").resolve()
        if root == claims or root in claims.parents or claims in root.parents:
            raise GateError("authorization_invalid")
        for child in (
            root / sha / unit,
            root / ".state" / sha / unit,
            root / "dry-runs" / sha / unit,
            root / "preflight-failures",
            root / BASELINE_FILENAME,
        ):
            _pilot_external_path(str(child), repo)
        for config_name in ("npm-userconfig", "npm-globalconfig"):
            _pilot_external_path(str(root / ".state" / sha / unit / config_name), repo)
        if any(path == root or root in path.parents for path in (auth_path, ledger_path)):
            raise GateError("authorization_invalid")
        raw = _bounded_file_bytes(auth_path)
        ledger_raw = _bounded_file_bytes(ledger_path)
        ledger_digest = hashlib.sha256(ledger_raw).hexdigest()
        document = _strict_json_document(raw)
        digest = hashlib.sha256(raw).hexdigest()
        inventory = _inventory_digest([path for paths in assignments.values() for path in paths])
        command = {key: value for key, value in COMMAND_PROVENANCE.items() if key != "dry_run"} | {
            "session_capacity": 3,
            "outer_concurrency": 1,
            "max_cost_usd": "15.000000",
        }
        expected = {
            "schema_version": "1.0",
            "authorized_action": (
                "unit_scan"
                if _profile_config(MICRO_PROFILE_ID, manifest).get("activation") == "active"
                else "pilot_scan"
            ),
            "sha": sha,
            "profile": MICRO_PROFILE_ID,
            "profile_digest": profile_digest(MICRO_PROFILE_ID, manifest),
            "inventory_digest": inventory,
            "unit": unit,
            "results_root": str(root),
            "budget_ledger_path": str(ledger_path),
            "budget_ledger_sha256": ledger_digest,
            "command": command,
        }
        if (
            unit not in _authorized_micro_units(_profile_config(MICRO_PROFILE_ID, manifest))
            or not isinstance(document, dict)
            or set(document) != set(expected) | {"authorization_id"}
            or not _nonempty_string(document.get("authorization_id"))
            or any(document.get(key) != value for key, value in expected.items())
            or type(document["command"].get("session_capacity")) is not int
            or type(document["command"].get("outer_concurrency")) is not int
        ):
            raise GateError("authorization_invalid")
    except (OSError, ValueError, TypeError) as exc:
        raise GateError("authorization_invalid") from exc
    try:
        budget = _strict_json_document(ledger_raw)
        if not isinstance(budget, dict):
            raise GateError("budget_ledger_invalid")
        history = _pilot_budget_history(budget)
        ceiling_valid, ceiling = _pilot_budget_ceiling(
            budget, _profile_config(MICRO_PROFILE_ID, manifest)
        )
        if (
            not isinstance(budget, dict)
            or budget.get("schema_version") != "1.0"
            or not ceiling_valid
            or _decimal_cost(budget.get("reservation_usd")) != Decimal("15")
            or history is None
            or _validate_budget_ledger(ledger_path, root, history[0], ledger_bytes=ledger_raw)
        ):
            raise GateError("budget_ledger_invalid")
        for receipt in budget["receipts"]:
            source = _pilot_external_path(receipt["receipt_path"], repo)
            if source == root or root in source.parents:
                raise GateError("budget_ledger_invalid")
        historical_total, costs = history
        reservation = _decimal_cost(budget["reservation_usd"])
        if reservation is None or (
            ceiling is not None and historical_total + reservation > ceiling
        ):
            raise GateError("budget_ledger_invalid")
        if hashlib.sha256(ledger_raw).hexdigest() != document["budget_ledger_sha256"]:
            raise GateError("budget_ledger_invalid")
    except (OSError, ValueError, TypeError, KeyError, GateError) as exc:
        raise GateError("budget_ledger_invalid") from exc
    return {
        **document,
        "authorization_sha256": digest,
        "historical_cost_usd": f"{historical_total:.6f}",
    }


def _pilot_claim_path(authorization: Path, evidence: dict[str, Any]) -> Path:
    directory = authorization.parent / "claims"
    if directory.resolve().parent != authorization.parent.resolve():
        raise GateError("claim_path_invalid")
    name = hashlib.sha256(
        (evidence["authorization_sha256"] + ":" + evidence["unit"]).encode("utf-8")
    ).hexdigest()
    return directory / f"{name}.json"


def acquire_pilot_claim(authorization: Path, evidence: dict[str, Any]) -> Path:
    """Exclusive creation is the attempt boundary, including incomplete writes."""
    path = _pilot_claim_path(authorization, evidence)
    try:
        path.parent.mkdir(exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump({**evidence, "created_at": datetime.now(UTC).isoformat()}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise GateError("attempt_already_claimed") from exc
    except OSError as exc:
        raise GateError("claim_creation_failed") from exc
    return path


def _ensure_pilot_baseline(
    root: Path, evidence: dict[str, Any], manifest: dict[str, Any] | None = None
) -> None:
    expected = {
        key: evidence[key] for key in ("sha", "profile", "profile_digest", "inventory_digest")
    }
    expected["schema_version"] = "2.0"
    root.mkdir(parents=True, exist_ok=True)
    path = root / BASELINE_FILENAME
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    **expected,
                    "first_unit": evidence["unit"],
                    "created_at": datetime.now(UTC).isoformat(),
                },
                handle,
                indent=2,
            )
    except FileExistsError:
        value = _strict_json_bytes(path)
        if (
            not isinstance(value, dict)
            or any(value.get(key) != val for key, val in expected.items())
            or value.get("first_unit")
            not in _authorized_micro_units(_profile_config(MICRO_PROFILE_ID, manifest))
            or not _timestamp(value.get("created_at"))
        ):
            raise GateError("shared baseline binding mismatch") from None
    except OSError as exc:
        raise GateError("unable to create shared baseline") from exc


def finalize(
    repo: Path,
    results_root: Path,
    dispositions_root: Path,
    *,
    profile: str = LEGACY_PROFILE_ID,
    budget_ledger: Path | None = None,
) -> ScanResult:
    sha = require_clean_revision(repo)
    manifest = _load_repo_manifest(repo, sha)
    assignments = _validate_repo_manifest(repo, profile, manifest)
    expected_inventory_digest = _inventory_digest(
        sorted(file for files in assignments.values() for file in files)
    )
    root = results_root.resolve()
    disp_root = dispositions_root.resolve()
    for forbidden in _worktree_roots(repo):
        if (
            root == forbidden
            or forbidden in root.parents
            or disp_root == forbidden
            or forbidden in disp_root.parents
        ):
            raise GateError(
                "results and dispositions roots must be outside repository and enclosing worktree"
            )
    if disp_root == root or root in disp_root.parents or disp_root in root.parents:
        raise GateError("dispositions root must differ from results root")
    if budget_ledger is not None:
        ledger_path = budget_ledger.resolve()
        if not _outside_protected_roots(ledger_path):
            raise GateError("budget ledger must be external")
    baseline = _read_baseline(root)
    if baseline.get("sha") != sha:
        raise GateError("shared baseline revision mismatch")
    if profile == MICRO_PROFILE_ID:
        if baseline.get("profile") != profile:
            raise GateError("shared baseline profile mismatch")
        if (
            baseline.get("schema_version") != "2.0"
            or baseline.get("first_unit")
            not in _authorized_micro_units(_profile_config(MICRO_PROFILE_ID, manifest))
            or not _timestamp(baseline.get("created_at"))
        ):
            raise GateError("shared baseline metadata invalid")
        if baseline.get("profile_digest") != profile_digest(profile, manifest):
            raise GateError("shared baseline profile digest mismatch")
        if baseline.get("inventory_digest") != expected_inventory_digest:
            raise GateError("shared baseline inventory digest mismatch")
    failures: list[str] = []
    total_cost = Decimal("0")
    complete_cost = True
    required_receipts: dict[str, dict[str, Any]] = {}
    seen_scan_ids: set[str] = set()
    unit_root = root / sha
    if profile == MICRO_PROFILE_ID and unit_root.is_dir():
        actual_units = {entry.name for entry in unit_root.iterdir() if entry.is_dir()}
        unexpected_units = actual_units - set(profile_unit_ids(profile, manifest))
        if unexpected_units:
            failures.append("artifact_path_invalid")
    for shard_id in profile_unit_ids(profile, manifest):
        output = root / sha / shard_id
        disposition = disp_root / sha / f"{shard_id}.json"
        result = evaluate_artifacts(
            output,
            shard=shard_id,
            expected_sha=sha,
            dispositions=disposition,
            require_dispositions=True,
            validate_runner_outcome=True,
            profile=profile,
            expected_inventory_digest=(
                expected_inventory_digest if profile == MICRO_PROFILE_ID else None
            ),
            expected_assigned_paths=(
                sorted(assignments.get(shard_id, [])) if profile == MICRO_PROFILE_ID else None
            ),
            profile_manifest=manifest,
        )
        failures.extend(result["reason_codes"])
        scan_id = result.get("scan_id")
        if profile == MICRO_PROFILE_ID:
            if scan_id is not None and scan_id in seen_scan_ids:
                failures.append("scan_id_mismatch")
            if isinstance(scan_id, str):
                seen_scan_ids.add(scan_id)
            required_receipts[str(output / "runner-result.json")] = {
                "sha": sha,
                "profile": profile,
                "shard": shard_id,
                "unit": shard_id,
                "scan_id": scan_id,
                "profile_digest": profile_digest(profile, manifest),
                "inventory_digest": expected_inventory_digest,
                "assigned_paths": sorted(assignments.get(shard_id, [])),
                "session_capacity": _profile_config(profile, manifest).get("session_capacity"),
                "max_cost_usd": _profile_config(profile, manifest).get("max_cost_usd"),
                "command": _command_provenance(profile, manifest),
            }
        if result["cost_usd"] is None:
            complete_cost = False
        else:
            total_cost += result["cost_usd"]
    total = total_cost if complete_cost else None
    if not complete_cost:
        failures.append("cost_invalid")
    elif profile == LEGACY_PROFILE_ID and total_cost >= Decimal(str(TOTAL_MAX_COST_USD)):
        failures.append("cost_cap_hit")
    if profile == MICRO_PROFILE_ID:
        if budget_ledger is None:
            failures.append("budget_ledger_invalid")
        else:
            ledger_path = budget_ledger.resolve()
            failures.extend(
                _validate_budget_ledger(
                    ledger_path,
                    root / sha,
                    total_cost,
                    required_receipts=required_receipts,
                )
            )
    elif budget_ledger is not None:
        failures.extend(_validate_budget_ledger(budget_ledger, root / sha, total_cost))
    deduped = list(dict.fromkeys(failures))
    ordered = [code for code in _REASON_ORDER if code in deduped]
    artifact_union_pass = not ordered
    if (
        profile == MICRO_PROFILE_ID
        and _profile_config(profile, manifest).get("activation") != "active"
    ):
        ordered.append("policy_not_activated")
    return ScanResult(
        root / sha,
        0 if not ordered else 2,
        total,
        not ordered,
        ordered,
        total,
        artifact_union_pass,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "dry-run"):
        command = subs.add_parser(name)
        command.add_argument("--repo", type=Path, required=True)
        command.add_argument(
            "--profile", choices=(LEGACY_PROFILE_ID, MICRO_PROFILE_ID), default=LEGACY_PROFILE_ID
        )
        if name == "dry-run":
            command.add_argument("--shard", choices=ALL_UNIT_IDS, required=True)
            command.add_argument("--results-root", type=Path)
            command.add_argument("--authorization", type=Path)
            command.add_argument("--budget-ledger", type=Path)
    plan = subs.add_parser("plan-unit")
    plan.add_argument("--repo", type=Path, required=True)
    plan.add_argument("--profile", choices=(MICRO_PROFILE_ID,), default=MICRO_PROFILE_ID)
    plan.add_argument("--shard", choices=MICRO_UNIT_IDS, required=True)
    scan = subs.add_parser("scan")
    scan.add_argument("--repo", type=Path, required=True)
    scan.add_argument(
        "--profile", choices=(LEGACY_PROFILE_ID, MICRO_PROFILE_ID), default=LEGACY_PROFILE_ID
    )
    scan.add_argument("--shard", choices=ALL_UNIT_IDS, required=True)
    scan.add_argument("--results-root", type=Path, required=True)
    scan.add_argument("--authorization", type=Path)
    scan.add_argument("--budget-ledger", type=Path)
    preflight = subs.add_parser("preflight")
    preflight.add_argument("--repo", type=Path, required=True)
    preflight.add_argument("--results-root", type=Path, required=True)
    final = subs.add_parser("finalize")
    final.add_argument("--repo", type=Path, required=True)
    final.add_argument(
        "--profile", choices=(LEGACY_PROFILE_ID, MICRO_PROFILE_ID), default=LEGACY_PROFILE_ID
    )
    final.add_argument("--results-root", type=Path, required=True)
    final.add_argument("--dispositions-root", type=Path, required=True)
    final.add_argument("--budget-ledger", type=Path)
    inspect = subs.add_parser("inspect-artifacts")
    inspect.add_argument("--scan-dir", type=Path, required=True)
    inspect.add_argument("--repo", type=Path)
    inspect.add_argument("--expected-sha", required=True)
    inspect.add_argument(
        "--profile", choices=(LEGACY_PROFILE_ID, MICRO_PROFILE_ID), default=LEGACY_PROFILE_ID
    )
    inspect.add_argument("--shard", choices=ALL_UNIT_IDS, required=True)
    inspect.add_argument("--dispositions", type=Path)
    return parser


def _report_scan_result(label: str, result: ScanResult) -> None:
    print(f"d5-security-gate: {label} evidence: {result.output_dir}")
    print(f"d5-security-gate: runner result: {result.output_dir / 'runner-result.json'}")
    for reason in result.reasons:
        print(f"d5-security-gate: {reason}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            sha = require_clean_revision(args.repo)
            manifest = _load_repo_manifest(args.repo, sha)
            _validate_repo_manifest(args.repo, args.profile, manifest)
            return 0
        if args.command == "plan-unit":
            print(
                json.dumps(
                    plan_unit(args.repo, args.shard, profile=args.profile),
                    sort_keys=True,
                    default=_json_default,
                )
            )
            return 0
        if args.command in {"dry-run", "preflight", "scan"}:
            if args.command == "dry-run":
                if args.profile == MICRO_PROFILE_ID and args.results_root is None:
                    raise GateError("micro results root is required")
                result = run_scan(
                    args.repo,
                    args.results_root or DRY_RUN_ROOT,
                    args.shard,
                    dry_run=True,
                    profile=args.profile,
                    authorization=args.authorization,
                    budget_ledger=args.budget_ledger,
                )
                _report_scan_result("dry-run", result)
            elif args.command == "preflight":
                result = run_preflight(args.repo, args.results_root)
                print(f"d5-security-gate: preflight evidence: {result.output_dir}")
                for reason in result.reasons:
                    print(f"d5-security-gate: {reason}", file=sys.stderr)
            else:
                result = run_scan(
                    args.repo,
                    args.results_root,
                    args.shard,
                    profile=args.profile,
                    authorization=args.authorization,
                    budget_ledger=args.budget_ledger,
                )
                _report_scan_result("scan", result)
            return result.exit_code
        if args.command == "inspect-artifacts":
            inspection = inspect_artifacts(
                args.scan_dir,
                args.expected_sha,
                args.shard,
                dispositions=args.dispositions,
                profile=args.profile,
                repo=args.repo,
            )
            print(json.dumps(inspection, sort_keys=True, default=_json_default))
            return 0 if inspection["gate_pass"] else 2
        if args.command == "finalize":
            result = finalize(
                args.repo,
                args.results_root,
                args.dispositions_root,
                profile=args.profile,
                budget_ledger=args.budget_ledger,
            )
            if result.artifact_union_pass is not None:
                print(
                    json.dumps(
                        {
                            "artifact_union_pass": result.artifact_union_pass,
                            "d5_pass": result.passed,
                        }
                    )
                )
            if not result.passed:
                for reason in result.reasons:
                    print(reason, file=sys.stderr)
            return 0 if result.passed else 2
    except GateError as exc:
        print(f"d5-security-gate: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
