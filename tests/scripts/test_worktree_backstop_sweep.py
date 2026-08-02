import importlib.util
import json
import os
import subprocess
import time as time_mod
from pathlib import Path
from types import SimpleNamespace

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


def test_effective_age_disabled_by_default(tmp_path, sweep):
    root = tmp_path / "worktrees"
    root.mkdir()
    assert sweep._effective_age_days(root, default_days=7, min_free_gb=0, pressure_days=2) == 7


def test_effective_age_tightens_under_disk_pressure(tmp_path, sweep, monkeypatch):
    root = tmp_path / "worktrees"
    root.mkdir()

    class _Usage:
        free = 3 * 1024 ** 3  # 3GB free
        total = 500 * 1024 ** 3

    monkeypatch.setattr(sweep.shutil, "disk_usage", lambda _p: _Usage())
    assert sweep._effective_age_days(root, default_days=7, min_free_gb=5, pressure_days=2) == 2


def test_effective_age_normal_when_disk_healthy(tmp_path, sweep, monkeypatch):
    root = tmp_path / "worktrees"
    root.mkdir()

    class _Usage:
        free = 50 * 1024 ** 3  # 50GB free
        total = 500 * 1024 ** 3

    monkeypatch.setattr(sweep.shutil, "disk_usage", lambda _p: _Usage())
    assert sweep._effective_age_days(root, default_days=7, min_free_gb=5, pressure_days=2) == 7


def test_effective_age_fails_closed_toward_normal_on_stat_error(tmp_path, sweep, monkeypatch):
    root = tmp_path / "worktrees"
    root.mkdir()

    def _raise(_p):
        raise OSError("boom")

    monkeypatch.setattr(sweep.shutil, "disk_usage", _raise)
    # A failed disk-usage read must fall back to the NORMAL (less aggressive)
    # threshold, never the tighter pressure one — a visibility failure must
    # never cause a surprise deletion.
    assert sweep._effective_age_days(root, default_days=7, min_free_gb=5, pressure_days=2) == 7


def test_fmt_bytes_human_readable(sweep):
    assert sweep._fmt_bytes(0) == "0.0B"
    assert sweep._fmt_bytes(2048) == "2.0KB"
    assert sweep._fmt_bytes(5 * 1024 ** 3) == "5.0GB"


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


# --- main() integration: SKIP_FETCH_FAILED / LANDED_PR_MERGED / receipt ---------------


def _proc(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _fake_candidate(root: Path, name: str = "ignite-mocked01") -> Path:
    """A directory shaped like a candidate worktree (matches TASK_DIR_RE, has a `.git`
    entry) WITHOUT being a real git repo — main()'s git-facing calls are fully mocked in
    these tests via _safety._git / sweep._git, so nothing here ever shells out for real."""
    wdir = root / name
    wdir.mkdir(parents=True)
    (wdir / ".git").mkdir()
    return wdir


def _run_main(
    sweep, monkeypatch, root, fake_git, *,
    receipt_path, extra_args=(), fake_gh=None, dry_run=True,
):
    monkeypatch.setattr(sweep, "_git", fake_git)
    monkeypatch.setattr(sweep._safety, "_git", fake_git)
    if fake_gh is not None:
        monkeypatch.setattr(sweep._safety, "_run_gh", fake_gh)
    argv = [
        "--root", str(root),
        "--bare-root", str(root / "no-such-bare-root"),
        "--retire-manifest", str(root / "no-such-retire-manifest.json"),
        "--receipt-path", str(receipt_path),
        *(["--dry-run"] if dry_run else []),
        *extra_args,
    ]
    rc = sweep.main(argv)
    assert rc == 0


def test_skip_fetch_failed_is_classified_separately_from_skip_ahead(
    tmp_path, sweep, monkeypatch, capsys
):
    root = tmp_path / "worktrees"
    root.mkdir()
    wdir = _fake_candidate(root)

    fetch_calls = []

    def fake_git(args, cwd=None, timeout=30):
        if args == ["status", "--porcelain"]:
            return _proc(stdout="")
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["rev-list", "--count", "origin/HEAD..HEAD"]:
            return _proc(stdout="3\n")
        if args == ["fetch", "origin", "--quiet"]:
            fetch_calls.append(args)
            return _proc(returncode=1)  # the actual fetch fails (e.g. osxkeychain -25308)
        return _proc(returncode=1)

    receipt_path = tmp_path / "receipt.json"
    _run_main(sweep, monkeypatch, root, fake_git, receipt_path=receipt_path)

    out = capsys.readouterr().out
    assert "SKIP_FETCH_FAILED" in out
    assert "SKIP_AHEAD_COMMITS" not in out
    assert wdir.exists()  # fail-closed: never removed on an unverified refresh
    assert len(fetch_calls) >= 1

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["skip_counts"]["skipped_fetch_failed"] == 1
    assert receipt["skip_counts"]["skipped_ahead"] == 0


def test_landed_pr_merged_is_classified_separately_from_landed_squash(
    tmp_path, sweep, monkeypatch, capsys
):
    root = tmp_path / "worktrees"
    root.mkdir()
    _fake_candidate(root, "ignite-mocked02")

    def fake_git(args, cwd=None, timeout=30):
        if args == ["status", "--porcelain"]:
            return _proc(stdout="")
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["rev-list", "--count", "origin/HEAD..HEAD"]:
            return _proc(stdout="2\n")
        if args == ["fetch", "origin", "--quiet"]:
            return _proc()
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc(returncode=1)
        if args == ["merge-tree", "--write-tree", "origin/main", "HEAD"]:
            return _proc(stdout="treeAAA\n")
        if args == ["rev-parse", "origin/main^{tree}"]:
            return _proc(stdout="treeBBB\n")  # mismatch -> tree-equality inconclusive
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _proc(stdout="ignite-mocked02\n")
        return _proc(returncode=1)

    def fake_gh(args, timeout=30):
        payload = json.dumps(
            [{"number": 7, "mergedAt": "2026-07-15T00:00:00Z", "headRefName": "ignite-mocked02"}]
        )
        return _proc(returncode=0, stdout=payload)

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(sweep, "HAS_WRITE_TREE", True)
    monkeypatch.setattr(sweep._safety, "HAS_WRITE_TREE", True)
    _run_main(sweep, monkeypatch, root, fake_git, receipt_path=receipt_path, fake_gh=fake_gh)

    out = capsys.readouterr().out
    assert "LANDED_PR_MERGED" in out
    assert "LANDED_SQUASH" not in out

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["skip_counts"]["landed_pr_merged"] == 1
    assert receipt["skip_counts"]["landed_squash"] == 0


def test_fetch_memoized_once_per_remote_across_early_and_late_checks(
    tmp_path, sweep, monkeypatch, capsys
):
    """The early eligibility gate and the late belt-and-suspenders re-check both call
    content_landed_check() for the same candidate; with the shared run_fetch_cache, the
    actual `git fetch origin` must happen only once for the whole run."""
    root = tmp_path / "worktrees"
    root.mkdir()
    wdir = _fake_candidate(root, "ignite-mocked03")
    # Backdate mtime well past the default 7-day age gate so the sweep reaches the late
    # re-check (which only runs for candidates that clear the age gate).
    old = time_mod.time() - 30 * 86400
    os.utime(wdir, (old, old))

    fetch_calls = []

    def fake_git(args, cwd=None, timeout=30):
        if args == ["status", "--porcelain"]:
            return _proc(stdout="")
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["rev-list", "--count", "origin/HEAD..HEAD"]:
            return _proc(stdout="1\n")
        if args == ["fetch", "origin", "--quiet"]:
            fetch_calls.append(args)
            return _proc()
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc()  # landed via ancestor -> both early + late checks say landed
        return _proc(returncode=1)

    receipt_path = tmp_path / "receipt.json"
    # Not dry-run this time, so the late belt-and-suspenders re-check actually runs (it's
    # skipped entirely under --dry-run) and issues its own content_landed_check() call —
    # the assertion below is that this doesn't re-fetch.
    _run_main(
        sweep, monkeypatch, root, fake_git, receipt_path=receipt_path,
        extra_args=["--days", "1"], dry_run=False,
    )

    assert len(fetch_calls) == 1


def test_receipt_written_atomically_with_expected_fields(tmp_path, sweep, monkeypatch):
    root = tmp_path / "worktrees"
    root.mkdir()

    def fake_git(args, cwd=None, timeout=30):
        return _proc(returncode=1)

    receipt_path = tmp_path / "nested" / "receipt.json"
    _run_main(sweep, monkeypatch, root, fake_git, receipt_path=receipt_path)

    assert receipt_path.exists()
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    for key in (
        "timestamp", "removed", "removed_bytes", "skip_counts", "free_gb",
        "min_free_gb", "pressure", "dry_run",
    ):
        assert key in data
    assert data["pressure"] is False  # --min-free-gb disabled by default
    assert data["removed"] == 0
    assert isinstance(data["skip_counts"], dict)
    assert "skipped_fetch_failed" in data["skip_counts"]
    assert "landed_pr_merged" in data["skip_counts"]
    # _write_json_atomic must never leave a stray .tmp-<pid> file behind.
    assert not list((tmp_path / "nested").glob(".*.tmp-*"))


def test_receipt_extends_existing_receipt_shape_not_a_second_file(tmp_path, sweep, monkeypatch):
    """The spec calls for extending a single receipt file, never adding a second one — pin
    the exact path convention so a future change can't silently start writing elsewhere."""
    assert sweep.RECEIPT_PATH_DEFAULT == "~/.hermes/state/worktree-backstop-sweep-receipt.json"
