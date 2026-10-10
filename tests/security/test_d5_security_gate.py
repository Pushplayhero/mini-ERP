"""Contract tests for the bounded D.5 security gate.

The tests intentionally use temporary, real git repositories for policy checks;
only scanner subprocess execution is mocked.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
gate: Any = importlib.import_module("d5_security_gate")


def test_pilot_authorization_requires_external_documents(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(gate.GateError, match="authorization_required"):
        gate.validate_pilot_authorization(
            repo, tmp_path / "results", "a" * 40, "u03_events_hooks", {}, None, None
        )
    assert not (tmp_path / "results").exists()


def _pilot_documents(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    repo = _repo(tmp_path)
    receipts = []
    for index, cost in enumerate(["11.830354", "11.817578", "11.791004"]):
        path = tmp_path / f"historical-{index}.json"
        path.write_text(json.dumps({"cost_usd": cost}), encoding="utf-8")
        receipts.append(
            {
                "receipt_path": str(path),
                "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "cost_usd": cost,
            }
        )
    ledger = tmp_path / "budget.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "ceiling_usd": None,
                "reservation_usd": "15.000000",
                "receipts": receipts,
            }
        ),
        encoding="utf-8",
    )
    value = {
        "schema_version": "1.0",
        "authorization_id": "pilot-test",
        "authorized_action": "unit_scan",
        "sha": "a" * 40,
        "profile": "micro-v2",
        "profile_digest": gate.profile_digest("micro-v2"),
        "inventory_digest": hashlib.sha256(b"README.md").hexdigest(),
        "unit": "u03_events_hooks",
        "results_root": str(tmp_path / "results"),
        "budget_ledger_path": str(ledger),
        "budget_ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "command": {
            "package": "@openai/codex-security@0.1.18",
            "auth": "chatgpt",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "approval_policy": "never",
            "session_capacity": 3,
            "outer_concurrency": 1,
            "max_cost_usd": "15.000000",
        },
    }
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(value), encoding="utf-8")
    return repo, authorization, ledger, value


def _validate_pilot(repo: Path, authorization: Path, ledger: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        gate.validate_pilot_authorization(
            repo,
            authorization.parent / "results",
            "a" * 40,
            "u03_events_hooks",
            {"u03_events_hooks": ["README.md"]},
            authorization,
            ledger,
        ),
    )


def test_pilot_authorization_reads_bound_historical_costs(tmp_path: Path) -> None:
    repo, authorization, ledger, _value = _pilot_documents(tmp_path)
    evidence = _validate_pilot(repo, authorization, ledger)
    assert evidence["historical_cost_usd"] == "35.438936"
    assert not (tmp_path / "results").exists()


def test_u02_pilot_authorization_allows_uncapped_cumulative_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    original_config = gate._profile_config
    monkeypatch.setattr(
        gate,
        "_profile_config",
        lambda profile, manifest=None: {
            **original_config(profile, manifest),
            "pilot_units": ["u03_events_hooks", "u01_identity", "u02_runtime_config"],
            "operational_ceiling_usd": None,
        }
        if profile == "micro-v2"
        else original_config(profile, manifest),
    )
    budget = json.loads(ledger.read_text(encoding="utf-8"))
    budget["ceiling_usd"] = None
    ledger.write_text(json.dumps(budget), encoding="utf-8")
    document.update(
        {
            "unit": "u02_runtime_config",
            "profile_digest": gate.profile_digest("micro-v2"),
            "budget_ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        }
    )
    authorization.write_text(json.dumps(document), encoding="utf-8")

    evidence = gate.validate_pilot_authorization(
        repo,
        tmp_path / "results",
        "a" * 40,
        "u02_runtime_config",
        {"u02_runtime_config": ["README.md"]},
        authorization,
        ledger,
    )

    assert evidence["historical_cost_usd"] == "35.438936"
    assert evidence["unit"] == "u02_runtime_config"
    assert not (tmp_path / "results").exists()


def test_active_micro_profile_authorizes_any_manifest_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    manifest = gate.load_manifest()
    config = manifest["candidate_profiles"]["micro-v2"]
    config["activation"] = "active"
    config["pilot_units"] = list(gate.MICRO_UNIT_IDS)
    monkeypatch.setattr(gate, "load_manifest", lambda *_args: manifest)
    document.update(
        {
            "authorized_action": "unit_scan",
            "unit": "u14_dependencies",
            "profile_digest": gate.profile_digest("micro-v2"),
        }
    )
    authorization.write_text(json.dumps(document), encoding="utf-8")

    evidence = gate.validate_pilot_authorization(
        repo,
        tmp_path / "results",
        "a" * 40,
        "u14_dependencies",
        {"u14_dependencies": ["README.md"]},
        authorization,
        ledger,
    )

    assert evidence["unit"] == "u14_dependencies"
    assert not (tmp_path / "results").exists()


def test_active_micro_dry_run_accepts_nonpilot_manifest_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest places this fixture beneath the real checkout in the constrained
    # workspace; the pilot validator's separate external-path guard is outside
    # the behavior this subprocess-boundary test exercises.
    monkeypatch.setattr(gate, "_protected_worktree_roots", lambda: [])
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    sha = gate._git(repo, "rev-parse", "HEAD")
    document.update(
        {
            "sha": sha,
            "unit": "u14_dependencies",
            "profile_digest": gate.profile_digest("micro-v2"),
        }
    )
    authorization.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        gate,
        "_validate_repo_manifest",
        lambda *_args: {"u14_dependencies": ["README.md"]},
    )
    monkeypatch.setattr(
        gate,
        "build_child_environment",
        lambda state, *_args, **_kwargs: {
            "PATH": str(tmp_path),
            "CODEX_SECURITY_STATE_DIR": str(state.resolve()),
        },
    )
    monkeypatch.setattr(
        gate,
        "_execute_preflight",
        lambda *_args: (
            {
                name: {"status": "passed", "exit_code": 0}
                for name in gate.PREFLIGHT_EVIDENCE_CHECKS
            },
            [],
            0,
        ),
    )
    monkeypatch.setattr(gate.shutil, "which", lambda *_args, **_kwargs: "npx")
    real_run = subprocess.run

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "npx":
            assert "u14_dependencies" in " ".join(argv)
            assert "--dry-run" in argv
            assert (
                Path(kwargs["cwd"])
                == (tmp_path / "results" / ".state" / sha / "u14_dependencies").resolve()
            )
            assert Path(argv[6]) == repo.resolve()
            return subprocess.CompletedProcess(argv, 0, "estimated cost: 0.50", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gate.subprocess, "run", run)

    result = gate.run_scan(
        repo,
        tmp_path / "results",
        "u14_dependencies",
        dry_run=True,
        profile="micro-v2",
        authorization=authorization,
        budget_ledger=ledger,
    )

    assert result.exit_code == 0
    assert result.output_dir.parts[-1] == "u14_dependencies"
    assert not (authorization.parent / "claims").exists()


def test_active_micro_profile_has_no_pilot_allowlist() -> None:
    manifest = gate.load_manifest()
    config = manifest["candidate_profiles"]["micro-v2"]
    config["activation"] = "active"
    config["pilot_units"] = ["u03_events_hooks"]
    assert gate._authorized_micro_units(config) == gate.MICRO_UNIT_IDS


def test_active_micro_finalize_is_not_blocked_as_unactivated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = gate.load_manifest()
    config = manifest["candidate_profiles"]["micro-v2"]
    config["activation"] = "active"
    config["pilot_units"] = list(gate.MICRO_UNIT_IDS)
    monkeypatch.setattr(gate, "load_manifest", lambda *_args: manifest)
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    results = tmp_path / "results"
    results.mkdir()
    (results / gate.BASELINE_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "sha": sha,
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": gate._inventory_digest([]),
                "first_unit": "u01_identity",
                "created_at": "2026-10-09T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )

    result = gate.finalize(repo, results, tmp_path / "dispositions", profile="micro-v2")

    assert "policy_not_activated" not in result.reasons


def test_active_micro_baseline_accepts_later_units_without_rebinding_first_unit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "results"
    first = {
        "sha": "a" * 40,
        "profile": "micro-v2",
        "profile_digest": gate.profile_digest("micro-v2"),
        "inventory_digest": "b" * 64,
        "unit": "u01_identity",
    }
    gate._ensure_pilot_baseline(root, first)

    gate._ensure_pilot_baseline(root, {**first, "unit": "u02_runtime_config"})

    baseline = json.loads((root / gate.BASELINE_FILENAME).read_text(encoding="utf-8"))
    assert baseline["first_unit"] == "u01_identity"


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha", "b" * 40),
        ("profile", "legacy-v1"),
        ("unit", "u01_identity"),
        ("profile_digest", "b" * 64),
        ("inventory_digest", "b" * 64),
        ("results_root", "relative"),
        ("budget_ledger_path", "relative"),
        ("budget_ledger_sha256", "b" * 64),
        ("schema_version", "2.0"),
        ("authorized_action", "scan"),
        ("command", {}),
        ("authorization_id", ""),
    ],
)
def test_pilot_authorization_rejects_binding_drift(tmp_path: Path, field: str, value: Any) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    document[field] = value
    authorization.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(gate.GateError, match="authorization_invalid"):
        _validate_pilot(repo, authorization, ledger)
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "changed_receipt",
        "duplicate",
        "missing",
        "unknown",
        "ceiling",
        "reservation",
        "precision",
        "nan",
    ],
)
def test_pilot_authorization_rejects_bad_budget(tmp_path: Path, damage: str) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    budget = json.loads(ledger.read_text(encoding="utf-8"))
    if damage == "changed_receipt":
        (tmp_path / "historical-0.json").write_text('{"cost_usd":0}', encoding="utf-8")
    elif damage == "duplicate":
        budget["receipts"][1] = budget["receipts"][0]
    elif damage == "missing":
        budget["receipts"].pop()
    elif damage == "unknown":
        budget["receipts"][0]["cost_usd"] = None
    elif damage == "ceiling":
        budget["ceiling_usd"] = "40.000000"
    elif damage == "reservation":
        budget["reservation_usd"] = "0.000000"
    else:
        budget["receipts"][0]["cost_usd"] = "1.0000001" if damage == "precision" else "NaN"
    ledger.write_text(json.dumps(budget), encoding="utf-8")
    document["budget_ledger_sha256"] = hashlib.sha256(ledger.read_bytes()).hexdigest()
    authorization.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(gate.GateError, match="budget_ledger_invalid"):
        _validate_pilot(repo, authorization, ledger)


def test_pilot_authorization_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    repo, authorization, ledger, _document = _pilot_documents(tmp_path)
    authorization.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(gate.GateError, match="authorization_invalid"):
        _validate_pilot(repo, authorization, ledger)


def test_pilot_claim_is_exclusive_and_survives_output_loss(tmp_path: Path) -> None:
    repo, authorization, ledger, _document = _pilot_documents(tmp_path)
    evidence = _validate_pilot(repo, authorization, ledger)
    claim = gate.acquire_pilot_claim(authorization, evidence)
    assert claim.is_file()
    assert claim.parent == authorization.parent / "claims"
    before = claim.read_bytes()
    with pytest.raises(gate.GateError, match="attempt_already_claimed"):
        gate.acquire_pilot_claim(authorization, evidence)
    assert claim.read_bytes() == before


@pytest.mark.parametrize("mode", ["dry", "failed", "preflight", "nonpilot", "success"])
# Keep the preflight/claim/scanner sequence visible in one integration scenario.
def test_pilot_scan_orders_claim_and_preserves_failed_attempt(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    document["sha"] = _git(repo, "rev-parse", "HEAD")
    authorization.write_text(json.dumps(document), encoding="utf-8")
    original_config = gate._profile_config
    monkeypatch.setattr(
        gate,
        "_profile_config",
        lambda profile, manifest=None: {
            **original_config(profile, manifest),
            "activation": "pilot_only",
            "pilot_units": ["u03_events_hooks", "u01_identity", "u02_runtime_config"],
        }
        if profile == "micro-v2"
        else original_config(profile, manifest),
    )
    document["profile_digest"] = gate.profile_digest("micro-v2")
    document["authorized_action"] = "pilot_scan"
    authorization.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        gate, "_validate_repo_manifest", lambda *_args: {"u03_events_hooks": ["README.md"]}
    )

    def preflight(*_args: Any) -> tuple[dict[str, Any], list[str], int]:
        assert not (tmp_path / "claims").exists()
        return (
            {
                name: {"status": "passed", "exit_code": 0}
                for name in gate.PREFLIGHT_EVIDENCE_CHECKS
            },
            ["node_crypto preflight failed"] if mode == "preflight" else [],
            2 if mode == "preflight" else 0,
        )

    monkeypatch.setattr(gate, "_execute_preflight", preflight)
    real_run = subprocess.run
    launches = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "npx":
            return real_run(argv, **kwargs)
        launches.append(argv)
        assert (tmp_path / "claims").exists() is (mode != "dry")
        if mode == "success":
            _artifacts(
                tmp_path / "results", "u03_events_hooks", document["sha"], profile="micro-v2"
            )
        return subprocess.CompletedProcess(
            argv, 0 if mode in {"dry", "success"} else 2, "estimated cost: 1.25", ""
        )

    monkeypatch.setattr(gate.subprocess, "run", run)
    root = tmp_path / "results"
    args = {"profile": "micro-v2", "authorization": authorization, "budget_ledger": ledger}
    if mode == "nonpilot":
        with pytest.raises(gate.GateError, match="pilot_unit_required"):
            gate.run_scan(repo, root, "u14_dependencies", **args)
        assert not root.exists()
        assert not launches
        return
    result = gate.run_scan(repo, root, "u03_events_hooks", dry_run=mode == "dry", **args)
    assert len(launches) == (0 if mode == "preflight" else 1)
    if mode in {"dry", "preflight"}:
        assert not (tmp_path / "claims").exists()
    if mode == "dry":
        assert "--dry-run" in launches[0]
        assert result.exit_code == 0
    if mode == "preflight":
        assert not (root / document["sha"] / "u03_events_hooks").exists()
        mode = "dry"
        recovery = gate.run_scan(repo, root, "u03_events_hooks", dry_run=True, **args)
        assert recovery.exit_code == 0
        assert not (tmp_path / "claims").exists()
    if mode == "success":
        assert result.exit_code == 0
        assert result.passed is True
    if mode == "failed":
        assert result.passed is False
        receipt = json.loads((result.output_dir / "runner-result.json").read_text(encoding="utf-8"))
        assert (
            receipt["authorization_sha256"]
            == hashlib.sha256(authorization.read_bytes()).hexdigest()
        )
        assert receipt["assigned_paths"] == ["README.md"]
        assert receipt["claim_sha256"]
        baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
        assert baseline["inventory_digest"] == document["inventory_digest"]
        with pytest.raises(gate.GateError, match="attempt_already_claimed"):
            gate.run_scan(repo, root, "u03_events_hooks", **args)
        assert len(launches) == 1


@pytest.mark.parametrize("field", ["authorization_sha256", "budget_ledger_sha256", "claim_sha256"])
def test_pilot_receipt_requires_attempt_bindings(tmp_path: Path, field: str) -> None:
    output = _artifacts(tmp_path, "u03_events_hooks", "a" * 40, profile="micro-v2")
    path = output / "runner-result.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop(field, None)
    path.write_text(json.dumps(value), encoding="utf-8")
    result = gate.inspect_artifacts(output, "a" * 40, "u03_events_hooks", profile="micro-v2")
    assert "runner_invalid" in result["reason_codes"]


@pytest.mark.parametrize("command", ["scan", "dry-run"])
def test_pilot_cli_accepts_explicit_authorization_and_destination(command: str) -> None:
    args = gate._parser().parse_args(
        [
            command,
            "--repo",
            "repo",
            "--profile",
            "micro-v2",
            "--shard",
            "u03_events_hooks",
            "--results-root",
            "results",
            "--authorization",
            "auth.json",
            "--budget-ledger",
            "budget.json",
        ]
    )
    assert args.authorization == Path("auth.json")
    assert args.budget_ledger == Path("budget.json")
    assert args.results_root == Path("results")


def test_pilot_results_cannot_contain_claims(tmp_path: Path) -> None:
    repo, authorization, ledger, document = _pilot_documents(tmp_path)
    document["results_root"] = str(tmp_path / "claims")
    authorization.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(gate.GateError, match="authorization_invalid"):
        gate.validate_pilot_authorization(
            repo,
            tmp_path / "claims",
            "a" * 40,
            "u03_events_hooks",
            {"u03_events_hooks": ["README.md"]},
            authorization,
            ledger,
        )


@pytest.mark.parametrize("field", ["authorization_sha256", "budget_ledger_sha256", "claim_sha256"])
def test_pilot_receipt_rejects_well_formed_but_unbound_digest(tmp_path: Path, field: str) -> None:
    output = _artifacts(tmp_path, "u03_events_hooks", "a" * 40, profile="micro-v2")
    path = output / "runner-result.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value[field] = "f" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    result = gate.inspect_artifacts(output, "a" * 40, "u03_events_hooks", profile="micro-v2")
    assert "runner_invalid" in result["reason_codes"]


@pytest.mark.parametrize("relative", [".state", "a" * 40, "dry-runs"])
def test_pilot_rejects_redirected_result_descendants(tmp_path: Path, relative: str) -> None:
    repo, authorization, ledger, _document = _pilot_documents(tmp_path)
    root = tmp_path / "results"
    root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = root / relative
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(gate.GateError, match="authorization_invalid"):
        _validate_pilot(repo, authorization, ledger)
    assert list(target.iterdir()) == []


def test_pilot_finalize_rejects_incomplete_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "_validate_repo_manifest", lambda *_args: {})
    root = tmp_path / "results"
    root.mkdir()
    (root / "baseline.json").write_text(
        json.dumps(
            {
                "sha": _git(repo, "rev-parse", "HEAD"),
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": hashlib.sha256(b"").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateError, match="baseline metadata"):
        gate.finalize(repo, root, tmp_path / "dispositions", profile="micro-v2")


def test_pilot_concurrent_claims_have_exactly_one_winner(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    repo, authorization, ledger, _value = _pilot_documents(tmp_path)
    evidence = _validate_pilot(repo, authorization, ledger)

    def acquire(_index: int) -> bool:
        try:
            gate.acquire_pilot_claim(authorization, evidence)
            return True
        except gate.GateError as exc:
            assert "attempt_already_claimed" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(acquire, range(8))).count(True) == 1


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in (files or {"README.md": "ok\n"}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    _git(repo, "checkout", "--detach", "-q")
    return repo


def _write_findings(path: Path, value: object, *, update_digest: bool = False) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    if update_digest:
        manifest_path = path.parent / "scan-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["scan"]["artifacts"]:
            if artifact["path"] == "findings.json":
                artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _update_artifact_digest(output: Path, name: str) -> None:
    artifact_path = output / name
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["scan"]["artifacts"]:
        if artifact["path"] == name:
            artifact["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _canonical_finding(level: str, *, occurrence_digit: str = "2") -> dict[str, Any]:
    return {
        "findingId": "csf_" + "1" * 24,
        "occurrenceId": "occ_" + occurrence_digit * 24,
        "ruleId": "rule.test",
        "identity": {"anchor": "anchor.test"},
        "fingerprints": {
            "algorithm": "codex-security/v1",
            "primary": "codex-security/v1:sha256:" + "3" * 64,
        },
        "title": "test",
        "summary": "test",
        "remediation": "test",
        "severity": {"level": level},
        "confidence": {"level": "high", "rationale": "test"},
        "taxonomy": {"category": "test", "cwe": []},
        "locations": [{"path": "README.md", "startLine": 1}],
        "provenance": {"source": "test"},
    }


def _write_sidecar(
    root: Path, sha: str, shard: str, findings_path: Path, entries: list[dict[str, Any]]
) -> Path:
    destination = root / sha / f"{shard}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sha": sha,
                "shard": shard,
                "scan_id": f"scan-{shard}",
                "findings_sha256": hashlib.sha256(findings_path.read_bytes()).hexdigest(),
                "findings": entries,
            }
        ),
        encoding="utf-8",
    )
    return destination


def _finalize(repo: Path, root: Path, tmp_path: Path) -> Any:
    return gate.finalize(repo, root, tmp_path / "dispositions")


def _artifacts(
    root: Path,
    shard: str,
    sha: str,
    *,
    complete: bool = True,
    cost_usd: float = 1.0,
    profile: str = "legacy-v1",
) -> Path:
    output = root / sha / shard
    output.mkdir(parents=True, exist_ok=True)
    scan_id = f"scan-{shard}"
    include_paths = gate._profile_units(profile)[shard]["paths"]
    coverage = {
        "documentType": "codex-security.coverage",
        "schemaVersion": "1.0",
        "scanId": scan_id,
        "mode": "scoped_path",
        "completeness": "complete" if complete else "partial",
        "inventoryStrategy": "scoped_path",
        "includePaths": include_paths,
        "excludePaths": [],
        "surfaces": [],
        "explicitExclusions": [],
        "deferred": [] if complete else [{"id": "deferred", "reason": "test"}],
    }
    findings = {
        "documentType": "codex-security.findings",
        "schemaVersion": "1.0",
        "scanId": scan_id,
        "findings": [],
    }
    for name, value in (("coverage.json", coverage), ("findings.json", findings)):
        (output / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = {
        "documentType": "codex-security.scan-manifest",
        "schemaVersion": "1.0",
        "scan": {
            "id": scan_id,
            "producer": {"name": "codex-security-plugin", "version": "0.1.22"},
            "status": "completed",
            "startedAt": "2026-08-26T00:00:00Z",
            "completedAt": "2026-08-26T00:01:00Z",
            "sealedAt": "2026-08-26T00:01:00Z",
            "target": {
                "kind": "git_revision",
                "targetId": "test",
                "displayName": "test",
                "revision": sha,
            },
            "scope": {"includePaths": include_paths, "excludePaths": []},
            "coverageRef": "coverage.json",
            "findingsRef": "findings.json",
            "artifacts": [
                {
                    "path": name,
                    "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest(),
                    "mediaType": "application/json",
                }
                for name in ("coverage.json", "findings.json")
            ],
        },
    }
    (output / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (output / "report.md").write_text("sealed\n", encoding="utf-8")
    (output / "runner-result.json").write_text(
        json.dumps(
            {
                "sha": sha,
                "shard": shard,
                **(
                    {
                        "profile": profile,
                        "unit": shard,
                        "scan_id": scan_id,
                        "profile_digest": gate.profile_digest(profile),
                        "inventory_digest": gate._inventory_digest([]),
                        "assigned_paths": [],
                        "session_capacity": 3,
                        "max_cost_usd": gate.MAX_COST_USD,
                        "authorization_sha256": "1" * 64,
                        "budget_ledger_sha256": "2" * 64,
                        "claim_sha256": "3" * 64,
                    }
                    if profile == "micro-v2"
                    else {}
                ),
                "exit_code": 0,
                "scanner_exit_code": 0,
                "scanner_started": True,
                "environment_policy_version": gate.ENVIRONMENT_POLICY_VERSION,
                "present_environment_keys": [],
                "preflight": {
                    name: {"status": "passed", "exit_code": 0}
                    for name in gate.PREFLIGHT_EVIDENCE_CHECKS
                },
                "passed": True,
                "reasons": [],
                "cost_usd": cost_usd,
                "started_at": "2026-08-25T23:59:00+00:00",
                "finished_at": "2026-08-26T00:00:00+00:00",
                "retention_until": "2026-09-25T00:00:00+00:00",
                "command": gate._command_provenance(profile),
            }
        ),
        encoding="utf-8",
    )
    if profile == "micro-v2":
        _write_attempt_fixture(output)
    return output


def _write_attempt_fixture(output: Path) -> None:
    root = output.parent.parent
    directory = root.parent / (root.name + "-authorization") / output.name
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "runner-result.json"
    runner = json.loads(receipt_path.read_text(encoding="utf-8"))
    sources = []
    for index, cost in enumerate(["11.830354", "11.817578", "11.791004"]):
        path = directory / f"history-{index}.json"
        path.write_text(json.dumps({"cost_usd": cost}), encoding="utf-8")
        sources.append(
            {
                "receipt_path": str(path),
                "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "cost_usd": cost,
            }
        )
    ledger = directory / "budget.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "ceiling_usd": None,
                "reservation_usd": "15.000000",
                "receipts": sources,
            }
        ),
        encoding="utf-8",
    )
    auth = {
        key: runner[key] for key in ("sha", "profile", "profile_digest", "inventory_digest", "unit")
    }
    auth.update(
        {
            "schema_version": "1.0",
            "authorization_id": "fixture",
            "authorized_action": "unit_scan",
            "results_root": str(root),
            "budget_ledger_path": str(ledger),
            "budget_ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
            "command": {
                "package": "@openai/codex-security@0.1.18",
                "auth": "chatgpt",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "approval_policy": "never",
                "session_capacity": 3,
                "outer_concurrency": 1,
                "max_cost_usd": "15.000000",
            },
        }
    )
    authorization = directory / "authorization.json"
    authorization.write_text(json.dumps(auth), encoding="utf-8")
    digest = hashlib.sha256(authorization.read_bytes()).hexdigest()
    claims = directory / "claims"
    claims.mkdir(exist_ok=True)
    claim = claims / (hashlib.sha256((digest + ":" + output.name).encode()).hexdigest() + ".json")
    claim.write_text(
        json.dumps(
            {**auth, "authorization_sha256": digest, "created_at": "2026-09-26T00:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    for kind, path in (
        ("authorization", authorization),
        ("budget_ledger", ledger),
        ("claim", claim),
    ):
        runner[f"{kind}_path"] = str(path)
        runner[f"{kind}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(runner), encoding="utf-8")


def _micro_budget_ledger(root: Path, sha: str, units: tuple[str, ...], destination: Path) -> Path:
    receipts = []
    for unit in units:
        receipt = root / sha / unit / "runner-result.json"
        receipts.append(
            {
                "receipt_path": str(receipt),
                "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                "cost_usd": 1.0,
            }
        )
    ledger = destination / "micro-ledger.json"
    ledger.write_text(json.dumps({"receipts": receipts}), encoding="utf-8")
    return ledger


def test_manifest_assigns_exactly_175_current_tracked_files() -> None:
    repo = Path(__file__).resolve().parents[2]
    assignments = gate.validate_manifest(repo)
    assert [len(assignments[shard]) for shard in gate.SHARD_IDS] == [32, 38, 41, 42, 22]
    assert sum(map(len, assignments.values())) == 175


def test_micro_profile_assigns_all_175_current_files_once() -> None:
    repo = Path(__file__).resolve().parents[2]
    assignments = gate.validate_manifest(repo, profile="micro-v2")
    config = gate._profile_config("micro-v2")
    assert config["activation"] == "active"
    assert config["pilot_units"] == list(gate.MICRO_UNIT_IDS)
    assert config["operational_ceiling_usd"] is None
    assert list(assignments) == list(gate.MICRO_UNIT_IDS)
    assert [len(assignments[unit]) for unit in gate.MICRO_UNIT_IDS] == [
        20,
        13,
        7,
        19,
        14,
        16,
        8,
        15,
        13,
        15,
        11,
        2,
        20,
        2,
    ]
    assert sum(map(len, assignments.values())) == 175
    assert {"scripts/d5_security_gate.py"}.issubset(assignments["u12_gate"])
    assert {"tests/security/test_d5_security_gate.py"}.issubset(assignments["u12_gate"])
    assert {"docs/security/d5-shards.json"}.issubset(assignments["u13_governance"])


def test_micro_profile_digest_is_stable_and_distinct() -> None:
    assert gate.profile_digest("micro-v2") == gate.profile_digest("micro-v2")
    assert gate.profile_digest("micro-v2") != gate.profile_digest("legacy-v1")


def test_micro_plan_unit_is_offline_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(gate, "require_clean_revision", lambda _repo: "a" * 40)
    preview = gate.plan_unit(repo, "u03_events_hooks", profile="micro-v2")
    assert preview["profile"] == "micro-v2"
    assert preview["unit"] == "u03_events_hooks"
    assert preview["file_count"] == 7
    assert preview["capacity"] == 3
    assert preview["worker_slots"] == 2
    assert preview["output_dir"] == "<external-results>/<sha>/u03_events_hooks"
    assert preview["scanner_invoked"] is False


def test_micro_preview_includes_scope_budget_and_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(gate, "require_clean_revision", lambda _repo: "a" * 40)
    preview = gate.plan_unit(repo, "u03_events_hooks", profile="micro-v2")
    argv = preview["scanner_argv"]
    assert argv[argv.index("--max-cost") + 1] == "15.0"
    assert argv[argv.index("--auth") + 1] == "chatgpt"
    assert argv[argv.index("--output-dir") + 1] == preview["output_dir"]
    assert "app/core/events.py" in argv
    knowledge_bases = [
        Path(argv[index + 1]) for index, value in enumerate(argv) if value == "--knowledge-base"
    ]
    assert (repo / "docs/security/scan-policy.md").resolve() in knowledge_bases


def test_micro_scanner_argv_preserves_capacity_and_never_changes_approval() -> None:
    repo = Path("C:/repo")
    argv = gate.build_scanner_argv(
        repo,
        "u03_events_hooks",
        Path("C:/results"),
        profile="micro-v2",
    )
    codex_values = [argv[index + 1] for index, value in enumerate(argv) if value == "--codex"]
    assert 'approval_policy="never"' in codex_values
    assert "features.multi_agent_v2.max_concurrent_threads_per_session=3" in codex_values
    knowledge_bases = [
        Path(argv[index + 1]) for index, value in enumerate(argv) if value == "--knowledge-base"
    ]
    assert knowledge_bases == [(repo / path).resolve() for path in gate.COMMON_KNOWLEDGE_BASE]
    assert "--dry-run" not in argv


def test_micro_scan_is_locked_before_output_or_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = gate._profile_config
    monkeypatch.setattr(
        gate,
        "_profile_config",
        lambda profile, manifest=None: {
            **original(profile, manifest),
            "activation": "offline_only",
        },
    )
    repo = _repo(tmp_path)
    root = tmp_path / "results"
    result = gate.run_scan(repo, root, "u03_events_hooks", profile="micro-v2")
    assert result.exit_code == 2
    assert result.reasons == ["policy_not_activated"]
    assert result.passed is False
    assert not root.exists()


def test_validate_rejects_micro_profile_path_overlap(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    manifest = json.loads(gate.MANIFEST_PATH.read_text(encoding="utf-8"))
    units = manifest["candidate_profiles"]["micro-v2"]["units"]
    units["u03_events_hooks"]["paths"].append("app/core")
    path = tmp_path / "bad-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(gate.GateError, match="duplicate assignment"):
        gate.validate_manifest(repo, path, profile="micro-v2")


def test_active_micro_union_passes_with_all_units_and_bound_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "micro-results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps(
            {
                "sha": sha,
                "schema_version": "2.0",
                "first_unit": "u03_events_hooks",
                "created_at": "2026-09-26T00:00:00+00:00",
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": gate._inventory_digest([]),
            }
        ),
        encoding="utf-8",
    )
    dispositions = tmp_path / "micro-dispositions"
    for unit in gate.MICRO_UNIT_IDS:
        _artifacts(root, unit, sha, profile="micro-v2")
        _write_sidecar(dispositions, sha, unit, root / sha / unit / "findings.json", [])
    ledger = _micro_budget_ledger(root, sha, gate.MICRO_UNIT_IDS, tmp_path)
    result = gate.finalize(repo, root, dispositions, profile="micro-v2", budget_ledger=ledger)
    assert result.artifact_union_pass is True
    assert result.passed is True
    assert result.reasons == []


def test_micro_union_missing_unit_is_not_artifact_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "micro-results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps(
            {
                "sha": sha,
                "schema_version": "2.0",
                "first_unit": "u03_events_hooks",
                "created_at": "2026-09-26T00:00:00+00:00",
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": gate._inventory_digest([]),
            }
        ),
        encoding="utf-8",
    )
    dispositions = tmp_path / "micro-dispositions"
    for unit in gate.MICRO_UNIT_IDS[:-1]:
        _artifacts(root, unit, sha, profile="micro-v2")
        _write_sidecar(dispositions, sha, unit, root / sha / unit / "findings.json", [])
    result = gate.finalize(repo, root, dispositions, profile="micro-v2")
    assert result.artifact_union_pass is False
    assert "missing_required_artifact" in result.reasons


def test_micro_finalize_requires_external_budget_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "micro-results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps(
            {
                "sha": sha,
                "schema_version": "2.0",
                "first_unit": "u03_events_hooks",
                "created_at": "2026-09-26T00:00:00+00:00",
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": gate._inventory_digest([]),
            }
        ),
        encoding="utf-8",
    )
    result = gate.finalize(repo, root, tmp_path / "dispositions", profile="micro-v2")
    assert result.artifact_union_pass is False
    assert "budget_ledger_invalid" in result.reasons


def test_micro_finalize_rejects_baseline_without_exact_profile_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "micro-results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(json.dumps({"sha": sha}), encoding="utf-8")
    with pytest.raises(gate.GateError, match="profile"):
        gate.finalize(repo, root, tmp_path / "dispositions", profile="micro-v2")


def test_micro_finalize_rejects_unexpected_unit_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate,
        "validate_manifest",
        lambda _repo, **_kwargs: {unit: [] for unit in gate.MICRO_UNIT_IDS},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "micro-results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps(
            {
                "sha": sha,
                "schema_version": "2.0",
                "first_unit": "u03_events_hooks",
                "created_at": "2026-09-26T00:00:00+00:00",
                "profile": "micro-v2",
                "profile_digest": gate.profile_digest("micro-v2"),
                "inventory_digest": gate._inventory_digest([]),
            }
        ),
        encoding="utf-8",
    )
    dispositions = tmp_path / "micro-dispositions"
    for unit in gate.MICRO_UNIT_IDS:
        _artifacts(root, unit, sha, profile="micro-v2")
        _write_sidecar(dispositions, sha, unit, root / sha / unit / "findings.json", [])
    (root / sha / "unexpected-unit").mkdir()
    ledger = _micro_budget_ledger(root, sha, gate.MICRO_UNIT_IDS, tmp_path)
    result = gate.finalize(repo, root, dispositions, profile="micro-v2", budget_ledger=ledger)
    assert result.artifact_union_pass is False
    assert "artifact_path_invalid" in result.reasons


def test_external_budget_ledger_binds_receipt_digest_and_deduplicates(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"cost_usd": 1.25}\n', encoding="utf-8")
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "receipts": [
                    {"receipt_path": str(receipt), "receipt_sha256": digest, "cost_usd": 1.25}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert gate._validate_budget_ledger(ledger, tmp_path, 1.25) == []
    ledger.write_text(
        json.dumps(
            {
                "receipts": [
                    {"receipt_path": str(receipt), "receipt_sha256": digest, "cost_usd": 1.25},
                    {"receipt_path": str(receipt), "receipt_sha256": digest, "cost_usd": 1.25},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert gate._validate_budget_ledger(ledger, tmp_path, 2.5) == ["budget_ledger_invalid"]


def test_budget_ledger_uses_six_place_decimal_and_reads_source_receipt_cost(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "failed-receipt.json"
    receipt.write_text(json.dumps({"status": "failed", "cost_usd": "1.250000"}), encoding="utf-8")
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "receipt_path": str(receipt),
                        "receipt_sha256": digest,
                        "cost_usd": "1.250000",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert gate._validate_budget_ledger(ledger, tmp_path, 1.25) == []

    receipt.write_text(json.dumps({"status": "failed", "cost_usd": "1.250001"}), encoding="utf-8")
    assert gate._validate_budget_ledger(ledger, tmp_path, 1.25) == ["budget_ledger_invalid"]


@pytest.mark.parametrize(
    "cost",
    [True, -0.000001, float("nan"), "1.0000001", None],
)
def test_budget_ledger_rejects_non_finite_negative_and_unknown_costs(
    tmp_path: Path, cost: object
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"cost_usd": "1.000000"}), encoding="utf-8")
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "receipts": [
                    {"receipt_path": str(receipt), "receipt_sha256": digest, "cost_usd": cost}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert gate._validate_budget_ledger(ledger, tmp_path, 1.0) == ["budget_ledger_invalid"]


@pytest.mark.parametrize("status", ["unknown", [], {}])
def test_budget_ledger_rejects_unknown_source_receipt_status(
    tmp_path: Path, status: object
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": status, "cost_usd": "1.000000"}), encoding="utf-8")
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "receipt_path": str(receipt),
                        "receipt_sha256": digest,
                        "cost_usd": "1.000000",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert gate._validate_budget_ledger(ledger, tmp_path, 1.0) == ["budget_ledger_invalid"]


def test_duplicate_receipt_cost_is_rejected(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"cost_usd":23.647932,"cost_usd":1}', encoding="utf-8")
    assert gate._read_receipt_cost(receipt) is None


def test_duplicate_ledger_receipts_are_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"receipts":[{"invalid":true}],"receipts":[]}', encoding="utf-8")
    assert gate._validate_budget_ledger(ledger, tmp_path, 0) == ["budget_ledger_invalid"]


def test_runner_cost_precision_is_not_rounded_before_validation(tmp_path: Path) -> None:
    sha = "a" * 40
    output = _artifacts(tmp_path, "a_identity_runtime", sha)
    receipt = output / "runner-result.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["cost_usd"] = "raw-number-sentinel"
    receipt.write_text(
        json.dumps(payload).replace('"raw-number-sentinel"', "1.0000000000000001"),
        encoding="utf-8",
    )
    result = gate.inspect_artifacts(output, shard="a_identity_runtime", expected_sha=sha)
    assert "cost_invalid" in result["reason_codes"]


def test_budget_ledger_sums_decimal_costs_without_binary_float_drift(tmp_path: Path) -> None:
    entries = []
    for index, cost in enumerate(("0.100000", "0.200000")):
        receipt = tmp_path / f"receipt-{index}.json"
        receipt.write_text(json.dumps({"cost_usd": cost}), encoding="utf-8")
        entries.append(
            {
                "receipt_path": str(receipt),
                "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                "cost_usd": cost,
            }
        )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"receipts": entries}), encoding="utf-8")
    assert gate._validate_budget_ledger(ledger, tmp_path, 0.3) == []


def test_dirty_worktree_and_exact_sha_are_enforced(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    assert gate.require_clean_revision(repo) == sha
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="clean"):
        gate.require_clean_revision(repo)


def test_results_root_must_be_external_and_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(gate.GateError, match="outside"):
        gate.prepare_output(repo, repo / "results", "sha", "a_identity_runtime")
    outside = tmp_path / "results"
    output = gate.prepare_output(repo, outside, "sha", "a_identity_runtime")
    assert output == outside / "sha" / "a_identity_runtime"
    (output / "existing").write_text("x", encoding="utf-8")
    with pytest.raises(gate.GateError, match="non-empty"):
        gate.prepare_output(repo, outside, "sha", "a_identity_runtime")


def test_scanner_argv_keeps_strict_approval_as_one_value() -> None:
    argv = gate.build_scanner_argv(
        Path("C:/repo"), "a_identity_runtime", Path("C:/results"), dry_run=True
    )
    assert argv[:6] == [
        "npx",
        "--yes",
        "--registry=https://registry.npmjs.org/",
        "--ignore-scripts",
        "@openai/codex-security@0.1.18",
        "scan",
    ]
    assert "--codex" in argv
    assert argv[argv.index("--codex") + 1] == 'approval_policy="never"'
    assert argv[argv.index("--auth") + 1] == "chatgpt"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[argv.index("--max-cost") + 1] == "11.75"
    assert "--dry-run" in argv


def test_child_environment_is_allowlisted_and_external_state_is_per_run(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state" / "sha" / "shard"
    env = gate.build_child_environment(
        state,
        {
            "PATH": "safe",
            "OPENAI_API_KEY": "bad",
            "NPM_CONFIG_PREFIX": "C:/caller-prefix",
            "NPM_CONFIG_USERCONFIG": "C:/caller-config",
            "NPM_CONFIG_CACHE": "C:/caller-cache",
        },
    )
    assert env["PATH"] == "safe"
    assert env["CODEX_SECURITY_STATE_DIR"].endswith("sha\\shard") or env[
        "CODEX_SECURITY_STATE_DIR"
    ].endswith("sha/shard")
    assert "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "USERPROFILE" not in env
    assert env["NPM_CONFIG_USERCONFIG"] == str((state / "npm-userconfig").resolve())
    assert env["NPM_CONFIG_GLOBALCONFIG"] == str((state / "npm-globalconfig").resolve())
    assert env["NPM_CONFIG_CACHE"] == str((state / "npm-cache").resolve())
    assert Path(env["NPM_CONFIG_USERCONFIG"]).read_text(encoding="utf-8") == ""
    assert Path(env["NPM_CONFIG_GLOBALCONFIG"]).read_text(encoding="utf-8") == ""
    assert "NPM_CONFIG_PREFIX" not in env
    assert "caller-cache" not in env["NPM_CONFIG_CACHE"]


def test_select_allowlisted_environment_normalizes_windows_names() -> None:
    selected = gate.select_allowlisted_environment(
        {
            "SYSTEMROOT": "C:/Windows",
            "systemroot": "C:/Windows",
            "path": "safe",
            "OPENAI_API_KEY": "secret",
        },
        windows=True,
    )

    assert selected == {"SYSTEMROOT": "C:/Windows", "PATH": "safe"}


def test_select_allowlisted_environment_rejects_different_windows_alias_values() -> None:
    with pytest.raises(gate.GateError, match="conflicting environment aliases"):
        gate.select_allowlisted_environment(
            {"SystemRoot": "C:/Windows", "SYSTEMROOT": "D:/Windows"},
            windows=True,
        )


def test_select_allowlisted_environment_preserves_posix_case_sensitivity() -> None:
    selected = gate.select_allowlisted_environment(
        {"PATH": "safe", "path": "secret", "systemroot": "wrong"},
        windows=False,
    )

    assert selected == {"PATH": "safe"}


@pytest.mark.parametrize("failure", ["node", "npx", "missing", "timeout", "oserror"])
def test_preflight_fails_closed_without_scanner_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repo = _repo(tmp_path)
    calls: list[list[str]] = []
    original_run = gate.subprocess.run
    if failure == "missing":
        monkeypatch.setattr(gate.shutil, "which", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr(gate.shutil, "which", lambda name, **_kwargs: name)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = cast(list[str], args[0])
        if argv and argv[0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        calls.append(argv)
        if failure == "node" and argv[0] == "node.exe":
            return subprocess.CompletedProcess(argv, 134, "raw node", "secret node")
        if failure == "npx" and argv[0] == "npx.cmd":
            return subprocess.CompletedProcess(argv, 134, "raw npx", "secret npx")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output="raw", stderr="secret")
        if failure == "oserror":
            raise OSError("raw os error")
        return subprocess.CompletedProcess(argv, 0, "1.0.0", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    result = gate.run_preflight(repo, tmp_path / "results")

    assert result.exit_code in {2, 134}
    assert result.passed is False
    if failure != "missing":
        assert calls
    assert all(not any("codex-security" in token for token in call) for call in calls)
    saved = json.loads((result.output_dir / "preflight-result.json").read_text(encoding="utf-8"))
    encoded = json.dumps(saved)
    assert "raw" not in encoded
    assert "secret" not in encoded


def test_preflight_checks_node_then_npx_with_ten_second_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate.shutil, "which", lambda name, **_kwargs: name)
    calls: list[tuple[list[str], dict[str, object]]] = []
    original_run = gate.subprocess.run

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = cast(list[str], args[0])
        if argv and argv[0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "v", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    result = gate.run_preflight(repo, tmp_path / "results")

    assert result.passed is True
    assert [call[0][0] for call in calls] == ["node.exe", "npx.cmd"]
    assert [call[1]["timeout"] for call in calls] == [10, 10]
    expected_cwd = (
        tmp_path / "results" / ".state" / result.output_dir.parts[-2] / "runtime-preflight"
    ).resolve()
    assert all(Path(call[1]["cwd"]) == expected_cwd for call in calls)
    saved = json.loads((result.output_dir / "preflight-result.json").read_text(encoding="utf-8"))
    assert saved["preflight"] == {
        "node_crypto": {"status": "passed", "exit_code": 0},
        "npx_version": {"status": "passed", "exit_code": 0},
        "scan_output_write": {"status": "passed", "exit_code": 0},
    }
    assert saved["environment_policy_version"] == gate.ENVIRONMENT_POLICY_VERSION
    assert saved["present_environment_keys"] == sorted(saved["present_environment_keys"])
    assert "PATH" in saved["present_environment_keys"]
    assert "NPM_CONFIG_USERCONFIG" in saved["present_environment_keys"]


def test_preflight_probes_exact_scan_output_directory_before_paid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate.shutil, "which", lambda name, **_kwargs: name)
    original_run = gate.subprocess.run
    calls: list[list[str]] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = cast(list[str], args[0])
        if argv and argv[0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "v", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    output = tmp_path / "results" / gate.require_clean_revision(repo) / "u12_gate"
    checks, reasons, exit_code = gate._execute_preflight(
        tmp_path / "state",
        {"PATH": str(tmp_path), "CODEX_SECURITY_STATE_DIR": str(output.parent)},
        output,
    )

    assert reasons == []
    assert exit_code == 0
    assert checks["scan_output_write"] == {"status": "passed", "exit_code": 0}
    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert [call[0] for call in calls] == ["node.exe", "npx.cmd"]


def test_preflight_rejects_unwritable_scan_output_before_paid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate.shutil, "which", lambda name, **_kwargs: name)
    original_run = gate.subprocess.run

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = cast(list[str], args[0])
        if argv and argv[0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        return subprocess.CompletedProcess(argv, 0, "v", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    output = tmp_path / "results" / gate.require_clean_revision(repo) / "u12_gate"
    output.mkdir(parents=True)
    (output / "existing-result.json").write_text("preserve", encoding="utf-8")

    checks, reasons, exit_code = gate._execute_preflight(
        tmp_path / "state",
        {"PATH": str(tmp_path), "CODEX_SECURITY_STATE_DIR": str(output.parent)},
        output,
    )

    assert exit_code == 2
    assert reasons == ["scan_output_write preflight failed"]
    assert checks["scan_output_write"] == {"status": "failed", "exit_code": 2}
    assert (output / "existing-result.json").read_text(encoding="utf-8") == "preserve"


def test_preflight_keeps_legacy_pass_evidence_compatible_but_requires_new_probe() -> None:
    legacy_runner: dict[str, Any] = {
        "environment_policy_version": gate.LEGACY_ENVIRONMENT_POLICY_VERSION,
        "scanner_started": True,
        "preflight": {
            name: {"status": "passed", "exit_code": 0} for name in gate.PREFLIGHT_CHECKS
        },
    }
    current_runner: dict[str, Any] = {
        **legacy_runner,
        "environment_policy_version": gate.ENVIRONMENT_POLICY_VERSION,
    }

    assert gate._preflight_passes(legacy_runner) is True
    assert gate._preflight_passes(current_runner) is False
    current_runner["preflight"] = {
        name: {"status": "passed", "exit_code": 0}
        for name in gate.PREFLIGHT_EVIDENCE_CHECKS
    }
    assert gate._preflight_passes(current_runner) is True


@pytest.mark.parametrize("mode", ["standalone", "scan"])
def test_environment_preparation_failure_persists_fail_closed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    monkeypatch.setattr(
        gate,
        "build_child_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            gate.GateError("conflicting environment aliases for SYSTEMROOT")
        ),
    )
    root = tmp_path / "results"
    if mode == "standalone":
        result = gate.run_preflight(repo, root)
        evidence_name = "preflight-result.json"
    else:
        result = gate.run_scan(repo, root, gate.SHARD_IDS[0])
        evidence_name = "runner-result.json"

    assert result.exit_code == 2
    assert result.passed is False
    saved = json.loads((result.output_dir / evidence_name).read_text(encoding="utf-8"))
    assert saved["passed"] is False
    assert saved["exit_code"] == 2
    assert saved["scanner_started"] is False
    assert saved["scanner_exit_code"] is None
    assert saved["cost_usd"] is None
    assert saved["environment_policy_version"] == gate.ENVIRONMENT_POLICY_VERSION
    assert saved["present_environment_keys"] == []
    assert "SYSTEMROOT" not in json.dumps(saved)
    if mode == "scan":
        assert not (root / "baseline.json").exists()


def test_run_scan_state_preparation_failure_persists_runner_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "results"
    sha = gate.require_clean_revision(repo)
    (root / ".state").mkdir(parents=True)
    (root / ".state" / sha).write_text("not a directory", encoding="utf-8")
    result = gate.run_scan(repo, root, gate.SHARD_IDS[0])

    assert result.exit_code == 2
    assert result.passed is False
    saved = json.loads((result.output_dir / "runner-result.json").read_text(encoding="utf-8"))
    assert saved["scanner_started"] is False
    assert saved["scanner_exit_code"] is None
    assert saved["cost_usd"] is None
    assert saved["exit_code"] == 2
    assert saved["present_environment_keys"] == []
    assert not (root / "baseline.json").exists()


def test_run_scan_preflight_failure_never_invokes_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    monkeypatch.setattr(gate.shutil, "which", lambda name, **_kwargs: name)
    calls: list[list[str]] = []
    original_run = gate.subprocess.run

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = cast(list[str], args[0])
        if argv and argv[0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        calls.append(argv)
        if argv[0] == "node.exe":
            return subprocess.CompletedProcess(argv, 134, "raw", "secret")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    result = gate.run_scan(repo, tmp_path / "results", gate.SHARD_IDS[0])

    assert result.exit_code == 134
    assert result.passed is False
    assert all(not any("codex-security" in token for token in call) for call in calls)
    assert not (tmp_path / "results" / "baseline.json").exists()
    saved = json.loads((result.output_dir / "runner-result.json").read_text(encoding="utf-8"))
    assert saved["scanner_started"] is False
    assert saved["scanner_exit_code"] is None
    assert saved["cost_usd"] is None
    assert "secret" not in json.dumps(saved)


def test_scan_captures_cost_without_embedding_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    result_root = tmp_path / "results"
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="estimated cost: $3.25\nCRITICAL finding secret=abc\n",
        stderr="",
    )
    original_run = gate.subprocess.run

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        return completed

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, result_root, "a_identity_runtime", dry_run=False)
    saved = json.loads((result.output_dir / "runner-result.json").read_text(encoding="utf-8"))
    assert result.exit_code != 0
    assert result.passed is False
    assert saved["cost_usd"] == 3.25
    assert saved["environment_policy_version"] == gate.ENVIRONMENT_POLICY_VERSION
    assert saved["present_environment_keys"] == sorted(saved["present_environment_keys"])
    assert "PATH" in saved["present_environment_keys"]
    assert "CRITICAL" not in json.dumps(saved)
    assert "secret=abc" not in json.dumps(saved)


@pytest.mark.parametrize("exit_code", [1, 2, 130, 143])
def test_nonzero_scanner_exit_is_no_pass(
    tmp_path: Path, exit_code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    completed = subprocess.CompletedProcess(args=[], returncode=exit_code, stdout="", stderr="")
    original_run = gate.subprocess.run
    scanner_calls: list[list[str]] = []

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list):
            argv = cast(list[str], args[0])
            if argv and argv[0] == "git":
                return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
            if any("@openai/codex-security@" in token for token in argv):
                scanner_calls.append(argv)
                return completed
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess([], 0, "", "")

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, tmp_path / "results", "a_identity_runtime", dry_run=False)
    assert scanner_calls
    assert result.exit_code == exit_code
    assert result.passed is False


def test_finalize_requires_sealed_complete_artifacts_and_dispositions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    dispositions_root = tmp_path / "dispositions"
    (root).mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    for shard in gate.SHARD_IDS:
        _write_sidecar(
            dispositions_root,
            sha,
            shard,
            root / sha / shard / "findings.json",
            [],
        )
    assert gate.finalize(repo, root, dispositions_root).passed is True
    findings = root / sha / gate.SHARD_IDS[0] / "findings.json"
    _write_findings(
        findings,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("medium")],
        },
        update_digest=True,
    )
    assert gate.finalize(repo, root, dispositions_root).passed is False
    _write_findings(
        findings,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("low")],
        },
        update_digest=True,
    )
    sidecar_entry = {
        "occurrenceId": "occ_" + "2" * 24,
        "classification": "confirmed",
        "rationale": "test",
        "remediation_acceptance": "test",
        "backlog_reference": "SEC-1",
    }
    _write_sidecar(dispositions_root, sha, gate.SHARD_IDS[0], findings, [sidecar_entry])
    assert gate.finalize(repo, root, dispositions_root).passed is True
    _artifacts(root, gate.SHARD_IDS[0], sha, cost_usd=gate.MAX_COST_USD)
    assert gate.finalize(repo, root, dispositions_root).passed is False


def test_finalize_rejects_revision_path_coverage_and_missing_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    manifest = root / sha / gate.SHARD_IDS[0] / "scan-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["scan"]["target"]["revision"] = "wrong"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert _finalize(repo, root, tmp_path).passed is False
    (root / sha / gate.SHARD_IDS[1] / "scan-manifest.json").unlink()
    assert _finalize(repo, root, tmp_path).passed is False


def test_manifest_rejects_wrong_cost_and_duplicate_shard_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manifest = gate.load_manifest(gate.MANIFEST_PATH)
    manifest["shards"][0]["max_cost_usd"] = 1
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(gate.GateError, match="max_cost"):
        gate.validate_manifest(repo, path)
    manifest = gate.load_manifest(gate.MANIFEST_PATH)
    manifest["shards"].append(dict(manifest["shards"][0]))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(gate.GateError, match="duplicate shard"):
        gate.validate_manifest(repo, path)


def _scan_artifacts_for_run(output: Path, shard: str, sha: str) -> None:
    scan_id = f"scan-{shard}"
    include_paths = gate.SHARDS[shard]["paths"]
    coverage = {
        "documentType": "codex-security.coverage",
        "schemaVersion": "1.0",
        "scanId": scan_id,
        "mode": "scoped_path",
        "completeness": "complete",
        "inventoryStrategy": "scoped_path",
        "includePaths": include_paths,
        "excludePaths": [],
        "surfaces": [],
        "explicitExclusions": [],
        "deferred": [],
    }
    findings = {
        "documentType": "codex-security.findings",
        "schemaVersion": "1.0",
        "scanId": scan_id,
        "findings": [],
    }
    for name, value in (("coverage.json", coverage), ("findings.json", findings)):
        (output / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = {
        "documentType": "codex-security.scan-manifest",
        "schemaVersion": "1.0",
        "scan": {
            "id": scan_id,
            "producer": {"name": "codex-security-plugin", "version": "0.1.22"},
            "status": "completed",
            "startedAt": "2026-08-26T00:00:00Z",
            "completedAt": "2026-08-26T00:01:00Z",
            "sealedAt": "2026-08-26T00:01:00Z",
            "target": {
                "kind": "git_revision",
                "targetId": "test",
                "displayName": "test",
                "revision": sha,
            },
            "scope": {"includePaths": include_paths, "excludePaths": []},
            "coverageRef": "coverage.json",
            "findingsRef": "findings.json",
            "artifacts": [
                {
                    "path": name,
                    "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest(),
                    "mediaType": "application/json",
                }
                for name in ("coverage.json", "findings.json")
            ],
        },
    }
    (output / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (output / "report.md").write_text("sealed\n", encoding="utf-8")


def test_attached_head_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "attached")
    with pytest.raises(gate.GateError, match="detached"):
        gate.require_clean_revision(repo)


def test_real_scan_requires_numeric_cost_and_rejects_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    original_run = gate.subprocess.run
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="scan complete\n", stderr=""
    )

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        return completed

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, tmp_path / "missing-cost", "a_identity_runtime")
    assert result.exit_code != 0
    assert result.passed is False
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="estimated cost: $11.75\n", stderr=""
    )
    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, tmp_path / "cap", "a_identity_runtime")
    assert result.exit_code != 0
    assert result.total_cost_usd == gate.LEGACY_MAX_COST_USD


def test_real_scan_validates_sealed_artifacts_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    original_run = gate.subprocess.run

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        argv = cast(list[str], args[0])
        if "--output-dir" not in argv:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        output = Path(argv[argv.index("--output-dir") + 1])
        sha = _git(repo, "rev-parse", "HEAD")
        _scan_artifacts_for_run(output, "a_identity_runtime", sha)
        scan = json.loads((output / "scan-manifest.json").read_text(encoding="utf-8"))
        scan["scan"]["target"]["revision"] = "wrong"
        (output / "scan-manifest.json").write_text(json.dumps(scan), encoding="utf-8")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="estimated cost: $1.00\n", stderr=""
        )

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, tmp_path / "results", "a_identity_runtime")
    assert result.exit_code != 0
    assert "target_mismatch" in result.reasons
    assert all("wrong" not in reason for reason in result.reasons)


def test_shared_baseline_rejects_different_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": "0" * 40, "first_shard": "a_identity_runtime"}), encoding="utf-8"
    )
    with pytest.raises(gate.GateError, match="baseline"):
        gate.run_scan(repo, root, "a_identity_runtime")


def test_finalize_aggregates_cost_and_rejects_total_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha, cost_usd=11.75)
    result = _finalize(repo, root, tmp_path)
    assert result.passed is False
    assert result.total_cost_usd == pytest.approx(58.75)


def test_finalize_requires_provenance_and_finding_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    for shard in gate.SHARD_IDS:
        _write_sidecar(
            tmp_path / "dispositions",
            sha,
            shard,
            root / sha / shard / "findings.json",
            [],
        )
    finding = root / sha / gate.SHARD_IDS[0] / "findings.json"
    _write_findings(
        finding,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("medium")],
        },
        update_digest=True,
    )
    assert _finalize(repo, root, tmp_path).passed is False
    _write_findings(
        finding,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("low")],
        },
        update_digest=True,
    )
    _write_sidecar(
        tmp_path / "dispositions",
        sha,
        gate.SHARD_IDS[0],
        finding,
        [
            {
                "occurrenceId": "occ_" + "2" * 24,
                "classification": "confirmed",
                "rationale": "test",
                "remediation_acceptance": "test",
                "backlog_reference": "SEC-1",
            }
        ],
    )
    assert _finalize(repo, root, tmp_path).passed is True
    _write_findings(
        finding,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("medium")],
        },
        update_digest=True,
    )
    _write_sidecar(
        tmp_path / "dispositions",
        sha,
        gate.SHARD_IDS[0],
        finding,
        [
            {
                "occurrenceId": "occ_" + "2" * 24,
                "classification": "confirmed",
                "rationale": "test",
                "remediation_acceptance": "test",
                "owner": "sec",
                "disposition": "accept",
                "due_date": "2026-09-01",
                "risk_acceptance": "RA-1",
            }
        ],
    )
    assert _finalize(repo, root, tmp_path).passed is True
    runner = root / sha / gate.SHARD_IDS[0] / "runner-result.json"
    runner_data = json.loads(runner.read_text(encoding="utf-8"))
    runner_data["command"]["model"] = "wrong"
    runner.write_text(json.dumps(runner_data), encoding="utf-8")
    assert _finalize(repo, root, tmp_path).passed is False


def _small_manifest(tmp_path: Path, names: list[str]) -> tuple[Path, Path]:
    repo = _repo(tmp_path, {name: "x\n" for name in names})
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "expected_total_files": len(names),
        "aggregate_max_cost_usd": 58.75,
        "operational_ceiling_usd": 60.0,
        "common_knowledge_base": [
            "SECURITY.md",
            "docs/security/threat-model.md",
            "docs/security/scan-policy.md",
        ],
        "shards": [],
    }
    for shard, name in zip(gate.SHARD_IDS, names[:5], strict=True):
        data["shards"].append(
            {
                "id": shard,
                "expected_file_count": 1,
                "max_cost_usd": gate.LEGACY_MAX_COST_USD,
                "paths": [name],
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return repo, manifest


def test_manifest_missing_path_spec_fails_at_missing_branch(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["a", "b", "c", "d", "e"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["shards"][0]["paths"] = ["not-tracked"]
    with manifest.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(gate.GateError, match="missing path spec"):
        gate.validate_manifest(repo, manifest)


def test_manifest_duplicate_spec_fails_at_duplicate_spec_branch(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["a", "b", "c", "d", "e"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["shards"][0]["paths"].append("a")
    with manifest.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(gate.GateError, match="duplicate path spec"):
        gate.validate_manifest(repo, manifest)


def test_manifest_duplicate_file_assignment_fails_at_assignment_branch(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["dir/b", "b", "c", "d", "e"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["shards"][0]["paths"] = ["dir", "dir/b"]
    with manifest.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(gate.GateError, match="duplicate file assignment"):
        gate.validate_manifest(repo, manifest)


def test_manifest_unassigned_tracked_file_fails_independently(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["a", "b", "c", "d", "e", "extra"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["expected_total_files"] = 6
    with manifest.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(gate.GateError, match="unassigned tracked files"):
        gate.validate_manifest(repo, manifest)


def test_manifest_count_mismatch_fails_independently(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["a", "b", "c", "d", "e"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["shards"][0]["expected_file_count"] = 2
    with manifest.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(gate.GateError, match="expected 2 files"):
        gate.validate_manifest(repo, manifest)


def test_manifest_extra_tracked_file_fails_independently(tmp_path: Path) -> None:
    repo, manifest = _small_manifest(tmp_path, ["a", "b", "c", "d", "e"])
    (repo / "unexpected").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "unexpected")
    _git(repo, "commit", "-qm", "extra tracked fixture")
    with pytest.raises(gate.GateError, match="unassigned tracked files"):
        gate.validate_manifest(repo, manifest)


def test_status_show_untracked_files_no_cannot_hide_dirty_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "status.showUntrackedFiles", "no")
    (repo / "hidden-untracked").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="clean"):
        gate.require_clean_revision(repo)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", {}),
        ("owner", []),
        ("owner", 1),
        ("owner", True),
        ("owner", ""),
        ("owner", "   "),
        ("disposition", {}),
        ("disposition", []),
        ("disposition", 1),
        ("disposition", True),
        ("disposition", ""),
        ("disposition", "   "),
        ("due_date", {}),
        ("due_date", []),
        ("due_date", 1),
        ("due_date", True),
        ("due_date", ""),
        ("due_date", "   "),
        ("compensating_control", {}),
        ("compensating_control", []),
        ("compensating_control", 1),
        ("compensating_control", True),
        ("compensating_control", ""),
        ("compensating_control", "   "),
        ("risk_acceptance", {}),
        ("risk_acceptance", []),
        ("risk_acceptance", 1),
        ("risk_acceptance", True),
        ("risk_acceptance", ""),
        ("risk_acceptance", "   "),
    ],
)
def test_medium_metadata_must_be_nonempty_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    finding: dict[str, object] = {
        "severity": "Medium",
        "owner": "owner",
        "disposition": "accept",
        "due_date": "2026-09-01",
        "compensating_control": "control",
    }
    if field == "risk_acceptance":
        finding.pop("compensating_control")
        finding["risk_acceptance"] = "RA-1"
    finding[field] = value
    (root / sha / gate.SHARD_IDS[0] / "findings.json").write_text(
        json.dumps([finding]), encoding="utf-8"
    )
    assert _finalize(repo, root, tmp_path).passed is False


@pytest.mark.parametrize("value", [{}, [], 1, True, "", "   "])
def test_low_backlog_reference_must_be_nonempty_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    (root / sha / gate.SHARD_IDS[0] / "findings.json").write_text(
        json.dumps([{"severity": "Low", "backlog_reference": value}]), encoding="utf-8"
    )
    assert _finalize(repo, root, tmp_path).passed is False


def test_runner_artifact_paths_list_all_evidence_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="estimated cost: $1.00\n", stderr=""
    )
    original_run = gate.subprocess.run

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        return completed

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        result = gate.run_scan(repo, tmp_path / "results", "a_identity_runtime", dry_run=True)
    runner = json.loads((result.output_dir / "runner-result.json").read_text(encoding="utf-8"))
    assert runner["artifact_paths"] == {
        "scan_manifest": "scan-manifest.json",
        "coverage": "coverage.json",
        "findings": "findings.json",
        "report": "report.md",
        "runner_output": "runner-output.log",
    }


def _mock_scanner(
    gate_module: Any,
    *,
    returncode: int,
    stdout: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    original_run = gate_module.subprocess.run

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        argv = cast(list[str], args[0])
        if "--yes" not in argv:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="1.0.0", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    return run_scanner


def test_dry_run_failure_persists_sanitized_evidence_and_rerun_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "dry-runs"
    monkeypatch.setattr(gate, "DRY_RUN_ROOT", root)
    scanner = _mock_scanner(
        gate,
        returncode=1,
        stdout="scanner secret=canary-secret-83eb\nsource finding CRITICAL\n",
    )
    with patch.object(gate.subprocess, "run", side_effect=scanner):
        rc = gate.main(["dry-run", "--repo", str(repo), "--shard", gate.SHARD_IDS[0]])
    output = capsys.readouterr()
    assert rc == 1
    assert "canary-secret-83eb" not in output.out + output.err
    assert "CRITICAL" not in output.out + output.err
    sha = _git(repo, "rev-parse", "HEAD")
    evidence = root / sha / gate.SHARD_IDS[0]
    assert (evidence / "runner-result.json").is_file()
    assert (evidence / "runner-output.log").read_text(encoding="utf-8").startswith("scanner secret")
    assert str(evidence) in output.out
    with patch.object(gate.subprocess, "run", side_effect=scanner):
        rerun_rc = gate.main(["dry-run", "--repo", str(repo), "--shard", gate.SHARD_IDS[0]])
    rerun_output = capsys.readouterr()
    assert rerun_rc == 2
    assert "non-empty" in rerun_output.err
    assert "abc" not in rerun_output.out + rerun_output.err


def test_dry_run_success_reports_retained_evidence_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "dry-runs"
    monkeypatch.setattr(gate, "DRY_RUN_ROOT", root)
    scanner = _mock_scanner(gate, returncode=0, stdout="dry-run complete\n")
    with patch.object(gate.subprocess, "run", side_effect=scanner):
        rc = gate.main(["dry-run", "--repo", str(repo), "--shard", gate.SHARD_IDS[0]])
    captured = capsys.readouterr()
    sha = _git(repo, "rev-parse", "HEAD")
    evidence = root / sha / gate.SHARD_IDS[0]
    assert rc == 0
    assert str(evidence) in captured.out
    assert (evidence / "runner-result.json").is_file()
    assert (evidence / "runner-output.log").is_file()


def test_dry_run_default_root_is_repo_external_and_private() -> None:
    assert Path.home() / ".codex" / "security-results" / "d5-dry-runs" == gate.DRY_RUN_ROOT


def test_dry_run_root_inside_repo_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    monkeypatch.setattr(gate, "DRY_RUN_ROOT", repo / "dry-runs")
    rc = gate.main(["dry-run", "--repo", str(repo), "--shard", gate.SHARD_IDS[0]])
    captured = capsys.readouterr()
    assert rc == 2
    assert "outside" in captured.err


def test_real_scan_cli_records_effective_failure_and_sanitized_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "real-results"
    scanner = _mock_scanner(
        gate,
        returncode=0,
        stdout="estimated cost: $1.00\nscanner secret=abc\nsource finding CRITICAL\n",
    )
    with patch.object(gate.subprocess, "run", side_effect=scanner):
        rc = gate.main(
            [
                "scan",
                "--repo",
                str(repo),
                "--shard",
                gate.SHARD_IDS[0],
                "--results-root",
                str(root),
            ]
        )
    captured = capsys.readouterr()
    sha = _git(repo, "rev-parse", "HEAD")
    evidence = root / sha / gate.SHARD_IDS[0]
    runner = json.loads((evidence / "runner-result.json").read_text(encoding="utf-8"))
    assert rc == 2
    assert runner["scanner_exit_code"] == 0
    assert runner["exit_code"] == 2
    assert runner["passed"] is False
    assert runner["reasons"] == ["missing_required_artifact"]
    assert str(evidence) in captured.out
    assert "missing_required_artifact" in captured.err
    assert "secret=abc" not in captured.out + captured.err
    assert "CRITICAL" not in captured.out + captured.err


def test_real_scan_cli_records_success_and_exact_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    root = tmp_path / "real-results"
    original_run = gate.subprocess.run
    scanner_runtime: dict[str, Any] = {}

    def run_scanner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and isinstance(args[0], list) and args[0][0] == "git":
            return cast(subprocess.CompletedProcess[str], original_run(*args, **kwargs))
        argv = cast(list[str], args[0])
        if "--output-dir" not in argv:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        output = Path(argv[argv.index("--output-dir") + 1])
        scanner_runtime["environment"] = kwargs["env"]
        scanner_runtime["cwd"] = kwargs["cwd"]
        _scan_artifacts_for_run(output, gate.SHARD_IDS[0], _git(repo, "rev-parse", "HEAD"))
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="estimated cost: $1.00\n", stderr=""
        )

    with patch.object(gate.subprocess, "run", side_effect=run_scanner):
        rc = gate.main(
            [
                "scan",
                "--repo",
                str(repo),
                "--shard",
                gate.SHARD_IDS[0],
                "--results-root",
                str(root),
            ]
        )
    captured = capsys.readouterr()
    sha = _git(repo, "rev-parse", "HEAD")
    evidence = root / sha / gate.SHARD_IDS[0]
    runner = json.loads((evidence / "runner-result.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert runner["scanner_exit_code"] == 0
    assert runner["exit_code"] == 0
    assert runner["passed"] is True
    assert runner["reasons"] == []
    environment = cast(dict[str, str], scanner_runtime["environment"])
    state = root / ".state" / sha / gate.SHARD_IDS[0]
    assert Path(environment["CODEX_SECURITY_STATE_DIR"]) == evidence.parent.resolve()
    assert Path(environment["NPM_CONFIG_USERCONFIG"]) == state / "npm-userconfig"
    assert Path(environment["NPM_CONFIG_GLOBALCONFIG"]) == state / "npm-globalconfig"
    assert Path(scanner_runtime["cwd"]) == state.resolve()
    finished = gate.datetime.fromisoformat(runner["finished_at"])
    retention = gate.datetime.fromisoformat(runner["retention_until"])
    assert retention - finished == gate.timedelta(days=30)
    assert str(evidence) in captured.out


def test_finalize_rejects_too_short_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    runner_path = root / sha / gate.SHARD_IDS[0] / "runner-result.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    runner["retention_until"] = "2026-09-24T23:59:59+00:00"
    runner_path.write_text(json.dumps(runner), encoding="utf-8")
    assert _finalize(repo, root, tmp_path).passed is False


@pytest.mark.parametrize("field", ["node_crypto", "npx_version", "scan_output_write"])
def test_finalize_rejects_malformed_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    runner_path = root / sha / gate.SHARD_IDS[0] / "runner-result.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    runner["preflight"][field] = {"status": "failed", "exit_code": 134}
    runner_path.write_text(json.dumps(runner), encoding="utf-8")

    result = _finalize(repo, root, tmp_path)

    assert result.passed is False


def test_private_state_flow_stays_outside_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    root = tmp_path / "private-results"
    sha = _git(repo, "rev-parse", "HEAD")
    output = gate.prepare_output(repo, root, sha, gate.SHARD_IDS[0])
    state = root / ".state" / sha / gate.SHARD_IDS[0]
    env = gate.build_child_environment(
        state,
        {
            "PATH": "safe",
            "NPM_CONFIG_USERCONFIG": "C:/caller-token-config",
            "NPM_CONFIG_PREFIX": "C:/caller-prefix",
        },
        codex_state_dir=output.parent,
    )
    assert repo.resolve() not in output.resolve().parents
    assert Path(env["CODEX_SECURITY_STATE_DIR"]) == output.parent.resolve()
    assert Path(env["NPM_CONFIG_USERCONFIG"]).is_relative_to(state.resolve())
    assert Path(env["NPM_CONFIG_GLOBALCONFIG"]).is_relative_to(state.resolve())
    assert "caller-token-config" not in json.dumps(env)
    assert "caller-prefix" not in json.dumps(env)


@pytest.mark.parametrize("field", ["scanner_exit_code", "exit_code"])
def test_finalize_rejects_boolean_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        gate, "validate_manifest", lambda _repo: {key: [] for key in gate.SHARD_IDS}
    )
    sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "results"
    root.mkdir()
    (root / gate.BASELINE_FILENAME).write_text(
        json.dumps({"sha": sha, "first_shard": gate.SHARD_IDS[0]}), encoding="utf-8"
    )
    for shard in gate.SHARD_IDS:
        _artifacts(root, shard, sha)
    runner_path = root / sha / gate.SHARD_IDS[0] / "runner-result.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    runner[field] = False
    runner_path.write_text(json.dumps(runner), encoding="utf-8")
    assert _finalize(repo, root, tmp_path).passed is False


def test_evaluate_artifacts_returns_canonical_outcome_shape(tmp_path: Path) -> None:
    sha = "a" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result == {
        "contract_valid": True,
        "execution_pass": True,
        "governance_pass": True,
        "gate_pass": True,
        "scan_id": f"scan-{gate.SHARD_IDS[0]}",
        "scan_status": "completed",
        "cost_usd": 1.0,
        "severity_counts": {},
        "reason_codes": [],
    }


def test_evaluate_artifacts_defers_medium_without_sidecar(tmp_path: Path) -> None:
    sha = "b" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    findings_path = output / "findings.json"
    findings = {
        "documentType": "codex-security.findings",
        "schemaVersion": "1.0",
        "scanId": f"scan-{gate.SHARD_IDS[0]}",
        "findings": [
            {
                "findingId": "csf_" + "1" * 24,
                "occurrenceId": "occ_" + "2" * 24,
                "ruleId": "rule.test",
                "identity": {"anchor": "anchor.test"},
                "fingerprints": {
                    "algorithm": "codex-security/v1",
                    "primary": "codex-security/v1:sha256:" + "3" * 64,
                },
                "title": "test",
                "summary": "test",
                "remediation": "test",
                "severity": {"level": "medium"},
                "confidence": {"level": "high", "rationale": "test"},
                "taxonomy": {"category": "test", "cwe": []},
                "locations": [{"path": "README.md", "startLine": 1}],
                "provenance": {"source": "test"},
            }
        ],
    }
    findings_path.write_text(json.dumps(findings), encoding="utf-8")
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][1]["sha256"] = hashlib.sha256(
        findings_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["contract_valid"] is True
    assert result["execution_pass"] is True
    assert result["governance_pass"] is True
    assert result["gate_pass"] is True
    assert result["severity_counts"] == {"medium": 1}


def test_parser_requires_new_inspect_and_finalize_forms() -> None:
    parser = gate._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["inspect-artifacts", "--repo", "repo", "--shard", gate.SHARD_IDS[0]])
    args = parser.parse_args(
        [
            "inspect-artifacts",
            "--scan-dir",
            "scan",
            "--expected-sha",
            "a" * 40,
            "--shard",
            gate.SHARD_IDS[0],
        ]
    )
    assert args.command == "inspect-artifacts"
    final = parser.parse_args(
        ["finalize", "--repo", "repo", "--results-root", "results", "--dispositions-root", "disp"]
    )
    assert final.dispositions_root == Path("disp")


def test_round2_requires_scan_identity_rule_identity_and_open_questions_shape(
    tmp_path: Path,
) -> None:
    sha = "c" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["id"] = ""
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert "manifest_invalid" in result["reason_codes"]
    assert result["scan_id"] is None

    output = _artifacts(tmp_path / "rule", gate.SHARD_IDS[0], sha)
    findings_path = output / "findings.json"
    findings: dict[str, Any] = {
        "documentType": "codex-security.findings",
        "schemaVersion": "1.0",
        "scanId": f"scan-{gate.SHARD_IDS[0]}",
        "findings": [_canonical_finding("low")],
    }
    findings["findings"][0]["ruleId"] = "BAD RULE"
    findings_path.write_text(json.dumps(findings), encoding="utf-8")
    manifest = json.loads((output / "scan-manifest.json").read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][1]["sha256"] = hashlib.sha256(
        findings_path.read_bytes()
    ).hexdigest()
    (output / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert "finding_invalid" in result["reason_codes"]
    assert result["severity_counts"] == {}

    output = _artifacts(tmp_path / "questions", gate.SHARD_IDS[0], sha)
    coverage_path = output / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["openQuestions"] = {"not": "a list"}
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][0]["sha256"] = hashlib.sha256(
        coverage_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert "coverage_invalid" in result["reason_codes"]


def test_scanner_coverage_shape_accepts_exclusion_notes_and_empty_receipt_refs(
    tmp_path: Path,
) -> None:
    sha = "d" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    coverage_path = output / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["explicitExclusions"] = [
        {
            "pattern": "supporting callers",
            "reason": "outside the scoped inventory",
        }
    ]
    coverage["surfaces"] = [
        {
            "id": "surface-test",
            "label": "test",
            "disposition": "no_issue_found",
            "receiptRefs": "",
        }
    ]
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][0]["sha256"] = hashlib.sha256(
        coverage_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert "coverage_invalid" not in result["reason_codes"]


def test_round2_parser_requires_inspect_scan_dir_and_python_finalize_root() -> None:
    parser = gate._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "inspect-artifacts",
                "--expected-sha",
                "a" * 40,
                "--shard",
                gate.SHARD_IDS[0],
            ]
        )
    with pytest.raises(TypeError):
        gate.finalize(Path("repo"), Path("results"))


def test_external_linked_worktree_is_protected_even_outside_main_checkout(
    tmp_path: Path,
) -> None:
    (tmp_path / "main").mkdir()
    repo = _repo(tmp_path / "main")
    linked = tmp_path / "arbitrary-linked-worktree"
    _git(repo, "worktree", "add", "--detach", "-q", str(linked))

    with pytest.raises(gate.GateError, match="outside"):
        gate.prepare_output(repo, linked / "results", "a" * 40, gate.SHARD_IDS[0])


def test_coverage_open_questions_uses_canonical_shape(tmp_path: Path) -> None:
    sha = "d" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    coverage_path = output / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["openQuestions"] = [{"question": "follow up"}]
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][0]["sha256"] = hashlib.sha256(
        coverage_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["gate_pass"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("writeup", {"reportPath": "findings/test/other.md"}),
        ("codeEvidence", [{"id": "evidence"}]),
        ("code_evidence", [{"id": "evidence"}]),
        ("rootCause", {"evidenceRefs": [1]}),
        ("root_cause", {"evidence_refs": [1]}),
        ("validation", {"assertions": [1]}),
        ("attackPath", {"steps": [1]}),
        ("remediationTests", [1]),
        ("preventiveControls", [{}]),
        ("extensions", {"candidateId": 1}),
    ],
)
def test_known_optional_finding_structures_reject_type_confusion(
    tmp_path: Path, field: str, value: object
) -> None:
    sha = "e" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    findings_path = output / "findings.json"
    findings: dict[str, Any] = {
        "documentType": "codex-security.findings",
        "schemaVersion": "1.0",
        "scanId": f"scan-{gate.SHARD_IDS[0]}",
        "findings": [_canonical_finding("low")],
    }
    findings["findings"][0][field] = value
    findings_path.write_text(json.dumps(findings), encoding="utf-8")
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["artifacts"][1]["sha256"] = hashlib.sha256(
        findings_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["contract_valid"] is False
    assert "finding_invalid" in result["reason_codes"]


def test_explicit_missing_sidecar_blocks_zero_finding_governance(tmp_path: Path) -> None:
    sha = "f" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=tmp_path / "missing-sidecar.json",
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["contract_valid"] is True
    assert result["execution_pass"] is True
    assert result["governance_pass"] is False
    assert result["gate_pass"] is False
    assert result["reason_codes"] == ["dispositions_missing"]


def test_invalid_arguments_are_preserved_with_missing_artifacts(tmp_path: Path) -> None:
    result = gate.evaluate_artifacts(
        tmp_path / "missing",
        shard=gate.SHARD_IDS[0],
        expected_sha="not-a-sha",
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["reason_codes"] == ["invalid_arguments", "missing_required_artifact"]


def test_legacy_decision_authorities_are_removed() -> None:
    assert not hasattr(gate, "_legacy_inspect_artifacts")
    assert not hasattr(gate, "_legacy_finalize")


def test_git_revision_failed_partial_evidence_is_contract_valid_but_not_executable(
    tmp_path: Path,
) -> None:
    sha = "1" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha, complete=False)
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = manifest["scan"]["target"]
    target["kind"] = "git_revision"
    target.pop("snapshotDigest", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["contract_valid"] is True
    assert result["execution_pass"] is False
    assert result["gate_pass"] is False
    assert "coverage_incomplete" in result["reason_codes"]


def test_main_inspect_no_pass_returns_two(tmp_path: Path) -> None:
    sha = "2" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    runner_path = output / "runner-result.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    runner["exit_code"] = 1
    runner["passed"] = False
    runner["reasons"] = ["runner_outcome_failed"]
    runner_path.write_text(json.dumps(runner), encoding="utf-8")

    result = gate.main(
        [
            "inspect-artifacts",
            "--scan-dir",
            str(output),
            "--expected-sha",
            sha,
            "--shard",
            gate.SHARD_IDS[0],
        ]
    )

    assert result == 2


def test_evaluator_containment_uses_filesystem_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "3" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    monkeypatch.setattr(gate, "_git", lambda *_args: (_ for _ in ()).throw(AssertionError("git")))

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=tmp_path / "missing-sidecar.json",
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["reason_codes"] == ["dispositions_missing"]


def test_arbitrary_linked_worktree_sidecar_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "4" * 40
    output = _artifacts(tmp_path / "evidence", gate.SHARD_IDS[0], sha)
    linked = tmp_path / "linked-worktree"
    linked.mkdir()
    monkeypatch.setattr(gate, "_protected_worktree_roots", lambda: [linked])
    sidecar = linked / "sidecar.json"
    sidecar.write_text(json.dumps({"schema_version": "1.0", "findings": []}), encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["reason_codes"] == ["disposition_path_invalid"]


def test_malformed_zero_finding_sidecar_blocks_governance(tmp_path: Path) -> None:
    sha = "5" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    sidecar = tmp_path / "malformed-sidecar.json"
    sidecar.write_text("{", encoding="utf-8")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["governance_pass"] is False
    assert result["reason_codes"] == ["disposition_invalid"]


def _filesystem_git_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    main = tmp_path / "main"
    common = main / ".git"
    admin = common / "worktrees" / "arbitrary"
    linked = tmp_path / "arbitrary-location" / "linked"
    admin.mkdir(parents=True)
    linked.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(str(linked / ".git") + "\n", encoding="utf-8")
    (linked / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    monkeypatch.setattr(gate, "__file__", str(main / "scripts" / "d5_security_gate.py"))
    return main, linked, admin


def test_filesystem_metadata_discovers_main_and_arbitrary_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked, _admin = _filesystem_git_layout(tmp_path, monkeypatch)
    roots = gate._protected_worktree_roots()
    assert main.resolve() in roots
    assert linked.resolve() in roots


def test_real_metadata_rejects_arbitrary_linked_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, _admin = _filesystem_git_layout(tmp_path, monkeypatch)
    sha = "6" * 40
    output = _artifacts(tmp_path / "evidence", gate.SHARD_IDS[0], sha)
    sidecar = linked / "sidecar.json"
    sidecar.write_text(json.dumps({"schema_version": "1.0", "findings": []}), encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert result["reason_codes"] == ["disposition_path_invalid"]


def test_filesystem_containment_never_spawns_git_or_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _filesystem_git_layout(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "_git", lambda *_args: (_ for _ in ()).throw(AssertionError("git")))
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("run")),
    )
    monkeypatch.setattr(
        gate.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Popen")),
    )
    assert len(gate._protected_worktree_roots()) == 2


def test_linked_marker_without_commondir_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, admin = _filesystem_git_layout(tmp_path, monkeypatch)
    (admin / "commondir").unlink()
    monkeypatch.setattr(gate, "__file__", str(linked / "scripts" / "d5_security_gate.py"))
    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


def test_non_directory_worktree_admin_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    (main / ".git" / "worktrees").mkdir(parents=True)
    (main / ".git" / "worktrees" / "malformed").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(gate, "__file__", str(main / "scripts" / "d5_security_gate.py"))
    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


def test_unreadable_git_metadata_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, admin = _filesystem_git_layout(tmp_path, monkeypatch)
    commondir = (admin / "commondir").resolve()
    original = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.resolve() == commondir:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(gate, "__file__", str(linked / "scripts" / "d5_security_gate.py"))
    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


def test_unreadable_zero_finding_sidecar_blocks_governance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "7" * 40
    output = _artifacts(tmp_path / "evidence", gate.SHARD_IDS[0], sha)
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({"schema_version": "1.0", "findings": []}), encoding="utf-8")
    original = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.resolve() == sidecar.resolve():
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert result["governance_pass"] is False
    assert result["reason_codes"] == ["disposition_invalid"]


def test_reason_codes_use_global_precedence_for_multiple_domains(tmp_path: Path) -> None:
    sha = "8" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha, complete=False, cost_usd=11.75)
    sidecar = tmp_path / "malformed.json"
    sidecar.write_text("{", encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=True,
        validate_runner_outcome=True,
    )
    assert len(result["reason_codes"]) >= 3
    assert result["reason_codes"] == [
        code for code in gate._REASON_ORDER if code in result["reason_codes"]
    ]


def test_git_worktree_target_kind_is_not_part_of_canonical_contract(tmp_path: Path) -> None:
    sha = "9" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    manifest_path = output / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scan"]["target"]["kind"] = "git_worktree"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )
    assert "target_mismatch" in result["reason_codes"]


def test_linked_commondir_must_resolve_to_owning_common_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, admin = _filesystem_git_layout(tmp_path, monkeypatch)
    decoy = tmp_path / "decoy-common"
    decoy.mkdir()
    (admin / "commondir").write_text(str(decoy), encoding="utf-8")
    monkeypatch.setattr(gate, "__file__", str(linked / "scripts" / "d5_security_gate.py"))

    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


def test_linked_gitdir_file_must_point_back_to_its_admin_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, _admin = _filesystem_git_layout(tmp_path, monkeypatch)
    wrong_admin = tmp_path / "wrong-admin"
    wrong_admin.mkdir()
    (linked / ".git").write_text(f"gitdir: {wrong_admin}\n", encoding="utf-8")

    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


def test_linked_gitdir_target_must_be_a_backlink_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked, _admin = _filesystem_git_layout(tmp_path, monkeypatch)
    (linked / ".git").unlink()
    (linked / ".git").mkdir()

    with pytest.raises(gate.GateError):
        gate._protected_worktree_roots()


@pytest.mark.parametrize(
    ("expected_sha", "shard"),
    [("not-a-sha", gate.SHARD_IDS[0]), ("a" * 40, "not-a-shard")],
)
def test_invalid_arguments_block_otherwise_valid_artifacts(
    tmp_path: Path, expected_sha: str, shard: str
) -> None:
    valid_sha = "a" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], valid_sha)

    result = gate.evaluate_artifacts(
        output,
        shard=shard,
        expected_sha=expected_sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert result["reason_codes"][0] == "invalid_arguments"
    assert result["contract_valid"] is False
    assert result["execution_pass"] is False
    assert result["governance_pass"] is False
    assert result["gate_pass"] is False


@pytest.mark.parametrize("value", [[], {}, True, 1])
@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("scan_status", "manifest_invalid"),
        ("coverage_completeness", "coverage_invalid"),
        ("surface_disposition", "coverage_invalid"),
        ("finding_confidence", "finding_invalid"),
    ],
)
def test_malformed_enum_values_return_sanitized_no_pass(
    tmp_path: Path, location: str, reason: str, value: object
) -> None:
    sha = "b" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha)
    if location == "scan_status":
        path = output / "scan-manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["scan"]["status"] = value
        path.write_text(json.dumps(document), encoding="utf-8")
    elif location in {"coverage_completeness", "surface_disposition"}:
        path = output / "coverage.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        if location == "coverage_completeness":
            document["completeness"] = value
        else:
            document["surfaces"] = [
                {
                    "id": "surface-test",
                    "label": "test",
                    "disposition": value,
                    "receiptRefs": [],
                }
            ]
        path.write_text(json.dumps(document), encoding="utf-8")
        _update_artifact_digest(output, "coverage.json")
    else:
        path = output / "findings.json"
        finding = _canonical_finding("low")
        finding["confidence"]["level"] = value
        _write_findings(
            path,
            {
                "documentType": "codex-security.findings",
                "schemaVersion": "1.0",
                "scanId": f"scan-{gate.SHARD_IDS[0]}",
                "findings": [finding],
            },
            update_digest=True,
        )

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert reason in result["reason_codes"]
    assert result["gate_pass"] is False


@pytest.mark.parametrize(
    "name",
    [
        "runner-result.json",
        "scan-manifest.json",
        "coverage.json",
        "findings.json",
        "report.md",
    ],
)
def test_fixed_artifact_link_escape_is_rejected(tmp_path: Path, name: str) -> None:
    sha = "c" * 40
    output = _artifacts(tmp_path / "evidence", gate.SHARD_IDS[0], sha)
    path = output / name
    outside = tmp_path / f"outside-{name}"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=False,
    )

    assert "artifact_path_invalid" in result["reason_codes"]
    assert result["gate_pass"] is False


def test_missing_artifact_preserves_independent_runner_failure_reasons(tmp_path: Path) -> None:
    sha = "d" * 40
    output = _artifacts(tmp_path, gate.SHARD_IDS[0], sha, cost_usd=11.75)
    runner_path = output / "runner-result.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    runner.update(
        {
            "scanner_exit_code": 2,
            "exit_code": 2,
            "passed": False,
            "reasons": ["runner_outcome_failed"],
        }
    )
    runner_path.write_text(json.dumps(runner), encoding="utf-8")
    (output / "report.md").unlink()

    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=None,
        require_dispositions=False,
        validate_runner_outcome=True,
    )

    assert result["reason_codes"] == [
        "missing_required_artifact",
        "scanner_nonzero_exit",
        "runner_outcome_failed",
        "cost_cap_hit",
    ]


def test_malformed_findings_never_reads_governance_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "e" * 40
    output = _artifacts(tmp_path / "evidence", gate.SHARD_IDS[0], sha)
    findings_path = output / "findings.json"
    _write_findings(
        findings_path,
        {
            "documentType": "codex-security.findings",
            "schemaVersion": "1.0",
            "scanId": f"scan-{gate.SHARD_IDS[0]}",
            "findings": [_canonical_finding("medium"), {"malformed": True}],
        },
        update_digest=True,
    )
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text("{}", encoding="utf-8")
    original = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.resolve() == sidecar.resolve():
            raise AssertionError("sidecar must not be read for malformed findings")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    result = gate.evaluate_artifacts(
        output,
        shard=gate.SHARD_IDS[0],
        expected_sha=sha,
        dispositions=sidecar,
        require_dispositions=True,
        validate_runner_outcome=False,
    )

    assert "finding_invalid" in result["reason_codes"]
    assert not any(code.startswith("disposition_") for code in result["reason_codes"])
    assert result["governance_pass"] is False
