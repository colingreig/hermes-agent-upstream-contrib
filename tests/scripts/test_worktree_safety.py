import importlib.util
import json
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


# --- content_landed_check(): SKIP_FETCH_FAILED classification -------------------------


def test_content_landed_check_reports_fetch_failed_when_origin_fetch_fails(
    tmp_path, safety, monkeypatch
):
    def fake_git(args, cwd, timeout=30):
        if args == ["fetch", "origin", "--quiet"]:
            return _proc(returncode=1)
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    result = safety.content_landed_check(tmp_path)
    assert result == safety.LandedCheck(landed=False, fetch_failed=True, via="")
    # The bool-only wrapper must still resolve to the same fail-closed False.
    assert safety.content_landed(tmp_path) is False


def test_content_landed_check_reports_fetch_failed_when_fork_fetch_fails(
    tmp_path, safety, monkeypatch
):
    def fake_git(args, cwd, timeout=30):
        if args == ["fetch", "origin", "--quiet"]:
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\nfork\n")
        if args == ["fetch", "fork", "--quiet"]:
            return _proc(returncode=1)
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    result = safety.content_landed_check(tmp_path)
    assert result.landed is False
    assert result.fetch_failed is True


def test_content_landed_check_ancestor_reports_via_and_no_fetch_failure(
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
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    result = safety.content_landed_check(tmp_path)
    assert result == safety.LandedCheck(landed=True, fetch_failed=False, via="ancestor")


# --- fetch memoization (fetch_cache) ---------------------------------------------------


def test_fetch_remote_memoizes_by_resolved_url_across_calls(tmp_path, safety, monkeypatch):
    fetch_calls = []

    def fake_git(args, cwd, timeout=30):
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["fetch", "origin", "--quiet"]:
            fetch_calls.append(args)
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    cache = {}
    first = safety.content_landed_check(tmp_path, fetch_cache=cache)
    second = safety.content_landed_check(tmp_path, fetch_cache=cache)

    assert first.landed is True and second.landed is True
    # The actual network fetch only happened once — the second call reused the cached
    # result for the same resolved remote URL.
    assert len(fetch_calls) == 1


def test_fetch_remote_without_cache_always_fetches_fresh(tmp_path, safety, monkeypatch):
    fetch_calls = []

    def fake_git(args, cwd, timeout=30):
        if args == ["fetch", "origin", "--quiet"]:
            fetch_calls.append(args)
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    safety.content_landed_check(tmp_path)
    safety.content_landed_check(tmp_path)

    # fetch_cache=None (default) preserves the original always-fetch-fresh behavior.
    assert len(fetch_calls) == 2


def test_fetch_remote_cache_pre_seeded_by_prefetch_skips_fetch(tmp_path, safety, monkeypatch):
    """Mirrors how worktree_backstop_sweep._prefetch_bare_mirrors() pre-seeds the cache: if
    the resolved remote URL is already a key in fetch_cache, content_landed_check() must
    never issue its own `git fetch` for that URL."""
    fetch_calls = []

    def fake_git(args, cwd, timeout=30):
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["fetch", "origin", "--quiet"]:
            fetch_calls.append(args)
            return _proc()
        if args == ["remote"]:
            return _proc(stdout="origin\n")
        if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return _proc(stdout="origin/main\n")
        if args == ["merge-base", "--is-ancestor", "HEAD", "origin/main"]:
            return _proc()
        return _proc(returncode=1)

    monkeypatch.setattr(safety, "_git", fake_git)

    cache = {"https://github.com/acme/repo.git": True}
    result = safety.content_landed_check(tmp_path, fetch_cache=cache)

    assert result.landed is True
    assert fetch_calls == []


# --- second landing proof: merged PR (86e2k..., merge-tree decay) ---------------------


def test_content_landed_check_falls_back_to_merged_pr_when_tree_mismatch(
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
            return _proc(stdout="treeAAA\n")
        if args == ["rev-parse", "origin/main^{tree}"]:
            return _proc(stdout="treeBBB\n")  # mismatch -> tree-equality inconclusive
        if args == ["remote", "get-url", "origin"]:
            return _proc(stdout="https://github.com/acme/repo.git\n")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _proc(stdout="ignite-abc123\n")
        return _proc(returncode=1)

    gh_calls = []

    def fake_gh(args, timeout=30):
        gh_calls.append((args, timeout))
        assert args[:2] == ["pr", "list"]
        assert "acme/repo" in args
        assert "ignite-abc123" in args
        payload = json.dumps(
            [{"number": 5, "mergedAt": "2026-07-01T00:00:00Z", "headRefName": "ignite-abc123"}]
        )
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)
    monkeypatch.setattr(safety, "_run_gh", fake_gh)

    result = safety.content_landed_check(tmp_path)
    assert result == safety.LandedCheck(landed=True, fetch_failed=False, via="pr_merged")
    assert len(gh_calls) == 1
    assert gh_calls[0][1] == 30  # 30s timeout on the gh call


def _mismatched_tree_git(extra=None):
    extra = extra or {}

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
            return _proc(stdout="treeAAA\n")
        if args == ["rev-parse", "origin/main^{tree}"]:
            return _proc(stdout="treeBBB\n")
        key = tuple(args)
        if key in extra:
            return extra[key]
        return _proc(returncode=1)

    return fake_git


def test_merged_pr_landed_rejects_when_gh_finds_no_merged_pr(tmp_path, safety, monkeypatch):
    fake_git = _mismatched_tree_git(
        {
            ("remote", "get-url", "origin"): _proc(stdout="https://github.com/acme/repo.git\n"),
            ("rev-parse", "--abbrev-ref", "HEAD"): _proc(stdout="ignite-abc123\n"),
        }
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)
    monkeypatch.setattr(
        safety, "_run_gh", lambda args, timeout=30: SimpleNamespace(returncode=0, stdout="[]", stderr="")
    )

    result = safety.content_landed_check(tmp_path)
    assert result == safety.LandedCheck(landed=False, fetch_failed=False, via="")


def test_merged_pr_landed_fails_closed_on_gh_nonzero_exit(tmp_path, safety, monkeypatch):
    fake_git = _mismatched_tree_git(
        {
            ("remote", "get-url", "origin"): _proc(stdout="https://github.com/acme/repo.git\n"),
            ("rev-parse", "--abbrev-ref", "HEAD"): _proc(stdout="ignite-abc123\n"),
        }
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)
    monkeypatch.setattr(
        safety,
        "_run_gh",
        lambda args, timeout=30: SimpleNamespace(returncode=1, stdout="", stderr="rate limited"),
    )

    assert safety.content_landed(tmp_path) is False


def test_merged_pr_landed_fails_closed_on_gh_timeout(tmp_path, safety, monkeypatch):
    fake_git = _mismatched_tree_git(
        {
            ("remote", "get-url", "origin"): _proc(stdout="https://github.com/acme/repo.git\n"),
            ("rev-parse", "--abbrev-ref", "HEAD"): _proc(stdout="ignite-abc123\n"),
        }
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)
    # _run_gh's real implementation returns None on any exception (incl. TimeoutExpired) —
    # exercise the same contract here.
    monkeypatch.setattr(safety, "_run_gh", lambda args, timeout=30: None)

    assert safety.content_landed(tmp_path) is False


def test_merged_pr_landed_fails_closed_on_headref_mismatch(tmp_path, safety, monkeypatch):
    """gh --head already filters server-side, but the client-side check must still reject a
    row whose headRefName doesn't match exactly (defense in depth)."""
    fake_git = _mismatched_tree_git(
        {
            ("remote", "get-url", "origin"): _proc(stdout="https://github.com/acme/repo.git\n"),
            ("rev-parse", "--abbrev-ref", "HEAD"): _proc(stdout="ignite-abc123\n"),
        }
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)
    payload = json.dumps(
        [{"number": 9, "mergedAt": "2026-07-01T00:00:00Z", "headRefName": "some-other-branch"}]
    )
    monkeypatch.setattr(
        safety, "_run_gh", lambda args, timeout=30: SimpleNamespace(returncode=0, stdout=payload, stderr="")
    )

    assert safety.content_landed(tmp_path) is False


def test_merged_pr_landed_skips_gh_when_origin_url_unparseable(tmp_path, safety, monkeypatch):
    fake_git = _mismatched_tree_git(
        {("remote", "get-url", "origin"): _proc(stdout="not-a-url\n")}
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)

    gh_calls = []
    monkeypatch.setattr(
        safety,
        "_run_gh",
        lambda args, timeout=30: gh_calls.append(args) or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    assert safety.content_landed(tmp_path) is False
    assert gh_calls == []  # unresolvable repo slug — must never even call gh


def test_merged_pr_landed_skips_gh_on_detached_head(tmp_path, safety, monkeypatch):
    fake_git = _mismatched_tree_git(
        {
            ("remote", "get-url", "origin"): _proc(stdout="https://github.com/acme/repo.git\n"),
            ("rev-parse", "--abbrev-ref", "HEAD"): _proc(stdout="HEAD\n"),
        }
    )
    monkeypatch.setattr(safety, "_git", fake_git)
    monkeypatch.setattr(safety, "HAS_WRITE_TREE", True)

    gh_calls = []
    monkeypatch.setattr(
        safety,
        "_run_gh",
        lambda args, timeout=30: gh_calls.append(args) or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    assert safety.content_landed(tmp_path) is False
    assert gh_calls == []
