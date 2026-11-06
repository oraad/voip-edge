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


def test_backup_companions_are_refused(tmp_path):
    repo = _repo(tmp_path)
    for name in (
        "edge-20260101.sqlite.console.sqlite.bak",
        "edge-20260101.sqlite.parked.json",
    ):
        (repo / name).write_text("tenant snapshot")
        subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)
        result = subprocess.run(
            ["sh", "tools/check-privacy.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, name
        assert "backup/snapshot companion" in result.stderr


def test_partial_backup_tmps_are_refused(tmp_path):
    repo = _repo(tmp_path)
    for name in (
        "edge-20260101.sqlite.tmp",
        "edge-20260101.sqlite.console.sqlite.bak.tmp",
        "edge-20260101.sqlite.parked.json.tmp",
    ):
        (repo / name).write_text("partial tenant snapshot")
        subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)
        result = subprocess.run(
            ["sh", "tools/check-privacy.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, name
        assert "partial backup" in result.stderr


def test_top_level_secret_paths_are_refused(tmp_path):
    repo = _repo(tmp_path)
    for name in ("tls/notes.txt", "tsig-key.conf", "acme/account.json"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret material")
        subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)
        result = subprocess.run(
            ["sh", "tools/check-privacy.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, name
        assert "secret/state path" in result.stderr


def test_database_journals_and_alternate_suffixes_are_refused(tmp_path):
    repo = _repo(tmp_path)
    for name in (
        "edge.sqlite-journal",
        "edge.db-wal",
        "edge.db-shm",
        "edge.db-journal",
        "edge.sqlite3",
    ):
        (repo / name).write_bytes(b"tenant database pages")
        subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)
        result = subprocess.run(
            ["sh", "tools/check-privacy.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, name
        assert "refusing database file" in result.stderr
