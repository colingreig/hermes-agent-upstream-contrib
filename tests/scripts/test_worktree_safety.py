import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "worktree_safety.py"


@pytest.fixture()
def safety():
    spec = importlib.util.spec_from_file_location("worktree_safety", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _proc(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def test_content_landed_fetches_fork_when_configured(tmp_path, safety, monkeypatch):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append((args, timeout))
        if args == ["remote"]:
            return _proc(stdout="origin\nfork\n")
        if args[:2] == ["fetch", "origin"]:
            return _proc()
        if args[:2] == ["fetch", "fork"]:
            return _proc(returncode=1)
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    assert safety.content_landed(tmp_path) is False
    assert (["fetch", "origin", "--quiet"], 60) in calls
    assert (["fetch", "fork", "--quiet"], 60) in calls


def test_content_landed_does_not_fetch_fork_when_absent(tmp_path, safety, monkeypatch):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append(args)
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    assert safety.content_landed(tmp_path) is False
    assert ["fetch", "origin", "--quiet"] in calls
    assert ["fetch", "fork", "--quiet"] not in calls


def test_content_landed_origin_fetch_failure_blocks_stale_ancestor_landed(
    tmp_path, safety, monkeypatch
):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append(args)
        if args == ["fetch", "origin", "--quiet"]:
            return _proc(returncode=128)
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    assert safety.content_landed(tmp_path) is False
    assert ["merge-base", "--is-ancestor", "HEAD", "origin/main"] not in calls


def test_content_landed_origin_fetch_none_blocks_stale_tree_landed(
    tmp_path, safety, monkeypatch
):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append(args)
        if args == ["fetch", "origin", "--quiet"]:
            return None
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc(returncode=1)
        if args == ["merge-tree", "--write-tree", "origin/main", "HEAD"]:
            return _proc(stdout="tree123\n")
        if args == ["rev-parse", "origin/main^{tree}"]:
            return _proc(stdout="tree123\n")
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)

    assert safety.content_landed(tmp_path) is False
    assert ["merge-tree", "--write-tree", "origin/main", "HEAD"] not in calls


def test_content_landed_fork_fetch_failure_blocks_stale_ancestor_landed(
    tmp_path, safety, monkeypatch
):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append(args)
        if args == ["fetch", "origin", "--quiet"]:
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\nfork\n")
        if args == ["fetch", "fork", "--quiet"]:
            return _proc(returncode=128)
        if args == ["rev-parse", "--abbrev-ref", "fork/HEAD"]:
            return _proc(stdout="fork/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "fork/main"]:
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    assert safety.content_landed(tmp_path) is False
    assert ["fetch", "fork", "--quiet"] in calls
    assert ["merge-base", "--is-ancestor", "HEAD", "fork/main"] not in calls


def test_content_landed_fork_fetch_none_blocks_stale_tree_landed(
    tmp_path, safety, monkeypatch
):
    calls = []

    def fake_git(args, cwd, timeout=30):
        calls.append(args)
        if args == ["fetch", "origin", "--quiet"]:
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\nfork\n")
        if args == ["fetch", "fork", "--quiet"]:
            return None
        if args == ["rev-parse", "--abbrev-ref", "fork/HEAD"]:
            return _proc(stdout="fork/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "fork/main"]:
            return _proc(returncode=1)
        if args == ["merge-tree", "--write-tree", "fork/main", "HEAD"]:
            return _proc(stdout="tree123\n")
        if args == ["rev-parse", "fork/main^{tree}"]:
            return _proc(stdout="tree123\n")
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)

    assert safety.content_landed(tmp_path) is False
    assert ["fetch", "fork", "--quiet"] in calls
    assert ["merge-tree", "--write-tree", "fork/main", "HEAD"] not in calls


def test_content_landed_falls_through_when_ancestor_check_cannot_prove_landed(
    tmp_path, safety, monkeypatch
):
    def fake_git(args, cwd, timeout=30):
        if args == ["fetch", "origin", "--quiet"]:
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc(returncode=1)
        if args == ["merge-tree", "--write-tree", "origin/main", "HEAD"]:
            return _proc(stdout="tree123\n")
        if args == ["rev-parse", "origin/main^{tree}"]:
            return _proc(stdout="tree123\n")
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)

    assert safety.content_landed(tmp_path) is True
