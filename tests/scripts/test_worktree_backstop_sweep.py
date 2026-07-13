import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "worktree_backstop_sweep.py"


@pytest.fixture()
def sweep(monkeypatch):
    spec = importlib.util.spec_from_file_location("worktree_backstop_sweep", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_is_claimed", None)
    return module


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(root: Path, name: str = "ignite-abc123") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("landed\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _manifest(snapshot: dict, **overrides) -> dict:
    entry = {
        **snapshot,
        "decision": "retire",
        "classification": "LANDED",
        "reason": "merged PR verified during dry-run triage",
        "approved_at": "2026-07-13T15:00:00Z",
    }
    entry.update(overrides)
    return {"version": 1, "entries": [entry]}


def test_snapshot_fingerprint_changes_when_worktree_changes(tmp_path, sweep):
    root = tmp_path / "worktrees"
    repo = _repo(root)

    before = sweep._candidate_snapshot(root, repo.name)
    (repo / "untracked.txt").write_text("new work\n", encoding="utf-8")
    after = sweep._candidate_snapshot(root, repo.name)

    assert before["head"] == after["head"]
    assert before["status_sha256"] != after["status_sha256"]
    assert before["fingerprint"] != after["fingerprint"]


def test_approved_clone_is_retired_only_on_exact_fingerprint(tmp_path, sweep):
    root = tmp_path / "worktrees"
    repo = _repo(root)
    snapshot = sweep._candidate_snapshot(root, repo.name)
    manifest_path = tmp_path / "retire.json"
    manifest_path.write_text(
        json.dumps(_manifest(snapshot)), encoding="utf-8"
    )

    removed, blocked, reserved = sweep._process_retire_manifest(
        root, manifest_path, dry_run=False
    )

    assert (removed, blocked) == (1, 0)
    assert reserved == {repo.name}
    assert not repo.exists()
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))["entries"][0]
    assert completed["decision"] == "completed"
    assert completed["result"] == "removed"
    assert completed["completed_at"].endswith("Z")


def test_drifted_candidate_fails_closed(tmp_path, sweep):
    root = tmp_path / "worktrees"
    repo = _repo(root)
    snapshot = sweep._candidate_snapshot(root, repo.name)
    manifest_path = tmp_path / "retire.json"
    manifest_path.write_text(
        json.dumps(_manifest(snapshot)), encoding="utf-8"
    )
    (repo / "new.txt").write_text("changed after approval\n", encoding="utf-8")

    removed, blocked, reserved = sweep._process_retire_manifest(
        root, manifest_path, dry_run=False
    )

    assert (removed, blocked) == (0, 1)
    assert reserved == {repo.name}
    assert repo.exists()
    pending = json.loads(manifest_path.read_text(encoding="utf-8"))["entries"][0]
    assert pending["decision"] == "retire"


def test_dry_run_never_mutates_candidate_or_manifest(tmp_path, sweep):
    root = tmp_path / "worktrees"
    repo = _repo(root)
    snapshot = sweep._candidate_snapshot(root, repo.name)
    manifest_path = tmp_path / "retire.json"
    original = json.dumps(_manifest(snapshot))
    manifest_path.write_text(original, encoding="utf-8")

    removed, blocked, _ = sweep._process_retire_manifest(
        root, manifest_path, dry_run=True
    )

    assert (removed, blocked) == (0, 0)
    assert repo.exists()
    assert manifest_path.read_text(encoding="utf-8") == original


def test_broken_symlink_requires_explicit_classification(tmp_path, sweep):
    root = tmp_path / "worktrees"
    root.mkdir()
    link = root / "ignite-dead123"
    link.symlink_to(root / "missing-target")
    snapshot = sweep._candidate_snapshot(root, link.name)
    manifest_path = tmp_path / "retire.json"
    manifest_path.write_text(
        json.dumps(_manifest(snapshot, classification="BROKEN_SYMLINK")),
        encoding="utf-8",
    )

    removed, blocked, _ = sweep._process_retire_manifest(
        root, manifest_path, dry_run=False
    )

    assert (removed, blocked) == (1, 0)
    assert not link.is_symlink()
