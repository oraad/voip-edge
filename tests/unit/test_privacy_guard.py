"""The privacy guard must fail the commit, not just print a warning."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "check-privacy.sh"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "tools" / "check-privacy.sh")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_staged_env_file_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".env").write_text("CONSOLE_TOKEN=secret\n")
    subprocess.run(["git", "add", "-f", ".env"], cwd=repo, check=True)

    result = subprocess.run(
        ["sh", "tools/check-privacy.sh"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "refusing env file" in result.stderr
    assert "commit blocked" in result.stderr


def test_staged_env_variant_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".env.production").write_text("CONSOLE_TOKEN=secret\n")
    subprocess.run(["git", "add", "-f", ".env.production"], cwd=repo, check=True)

    result = subprocess.run(
        ["sh", "tools/check-privacy.sh"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "refusing env file" in result.stderr


def test_staged_plain_file_passes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)

    result = subprocess.run(
        ["sh", "tools/check-privacy.sh"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_named_env_file_in_a_subdirectory_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "deploy").mkdir()
    (repo / "deploy" / "acme.env").write_text("Technitium_Token=secret\n")
    subprocess.run(
        ["git", "add", "-f", "deploy/acme.env"], cwd=repo, check=True
    )

    result = subprocess.run(
        ["sh", "tools/check-privacy.sh"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "refusing env file" in result.stderr
