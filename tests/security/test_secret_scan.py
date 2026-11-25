"""Tests for deterministic secret scanner (scripts/secret_scan.py, ADR-018)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
secret_scan = importlib.import_module("secret_scan")


def test_scan_content_detects_all_secret_classes_and_ignores_benign_templates() -> None:
    fake_whsec = "whsec_" + ("a1b2c3d4" * 6)
    fake_aws = "AKIA" + ("1234ABCD" * 2)
    fake_gh = "ghp_" + ("AbCdEfGh1234" * 3)
    fake_gcp = "AIza" + ("SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"[:35])
    fake_slack = "xoxb-" + "1234567890-abcdef"
    fake_fernet = Fernet.generate_key().decode("ascii")
    pem_header = "-----BEGIN " + "PRIVATE KEY-----"

    sample = "\n".join(
        [
            f'secret = "{fake_whsec}"',
            pem_header,
            f'aws_id = "{fake_aws}"',
            f'gh = "{fake_gh}"',
            f'gcp = "{fake_gcp}"',
            f'slack = "{fake_slack}"',
            "BOOTSTRAP_ADMIN_PASSWORD=super_real_literal_password",
            f'MINI_ERP_SECRET_ENCRYPTION_KEYS="{fake_fernet}"',
            f'encryption_key = "{fake_fernet}"',
            # Benign lines that must pass
            "BOOTSTRAP_ADMIN_PASSWORD=",
            'BOOTSTRAP_ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD}"',
            "MINI_ERP_SECRET_ENCRYPTION_KEYS=",
        ]
    )

    findings = secret_scan.scan_content(sample, path="app/leaky.py")
    rules = [f.rule_id for f in findings]
    assert rules == [
        "WHSEC_SECRET",
        "PRIVATE_KEY",
        "AWS_ACCESS_KEY",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "SLACK_TOKEN",
        "HARDCODED_SECRET_ENV",
        "HARDCODED_SECRET_ENV",
        "INLINE_FERNET_KEY",
    ]
    # Redacted previews must never expose full secret strings
    for item in findings:
        assert fake_whsec not in item.preview
        assert fake_gh not in item.preview
        assert fake_fernet not in item.preview
        assert "super_real_literal_password" not in item.preview


def test_allow_marker_honoured_only_in_tests_directory() -> None:
    sample_line = 'secret = "whsec_' + ("f0e1d2c3" * 6) + '"  # secret-scan: allow-test-fixture'
    assert secret_scan.scan_content(sample_line, path="tests/test_sample.py") == []
    app_findings = secret_scan.scan_content(sample_line, path="app/prod_file.py")
    assert len(app_findings) == 1
    assert app_findings[0].rule_id == "WHSEC_SECRET"


def test_scan_repository_and_git_history_catch_deleted_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Tester"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tester@example.com"],
        check=True,
        capture_output=True,
    )

    leaked_token = "ghp_" + ("ZxYvWuTs9876" * 3)
    secret_file = repo / "config.py"
    secret_file.write_text(f'TOKEN = "{leaked_token}"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "config.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "accidental leak"],
        check=True,
        capture_output=True,
    )

    # Working tree scan catches it
    assert secret_scan.main(["--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err
    assert leaked_token not in err

    # Remove the secret in a later commit -> tracked scan passes, but --git-history catches it!
    secret_file.write_text('TOKEN = ""\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "config.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "remove token"],
        check=True,
        capture_output=True,
    )

    assert secret_scan.main(["--repo", str(repo)]) == 0
    assert secret_scan.main(["--repo", str(repo), "--git-history"]) == 1
    hist_err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in hist_err
    assert leaked_token not in hist_err


def test_current_repository_passes_secret_scan_with_git_history() -> None:
    assert secret_scan.main(["--repo", str(PROJECT_ROOT), "--git-history"]) == 0


def test_scan_line_checks_every_match_when_historical_allowlist_present_on_same_line() -> None:
    historical_sample = "whsec_" + ("1234567890" * 4) + "12345678"
    real_leak = "whsec_" + ("deadbeef" * 6)
    combined_line = f'secrets = ["{historical_sample}", "{real_leak}"]'

    findings = secret_scan.scan_line(
        combined_line,
        path="app/pair.py",
        line_no=1,
        source="git-history:abc123",
        allow_historical_hashes=True,
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "WHSEC_SECRET"
    assert real_leak not in findings[0].preview
