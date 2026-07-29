"""Tests for the sync-prod-live-patches GitHub Actions workflow.

The mini's deploy branch (prod-live-patches, tracked by release-poll)
historically required a manual forward-merge from main, and when nobody
did it, merged fixes silently never deployed (task 86e2hw6fp — on
2026-07-28 prod-live-patches was 6 commits behind main).

This test module covers two things:

1. The workflow YAML has the triggers/safety properties the fix requires
   (push to main, daily schedule, manual dispatch, full-history checkout,
   a concurrency group, and a bot git identity).
2. The core merge decision — clean merge vs. conflicting merge — behaves
   as the workflow's shell steps assume, exercised against real local git
   repos in a tmp dir (no network, no GitHub API).
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "sync-prod-live-patches.yml"
)


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "test")
    run_git(repo, "config", "user.email", "test@example.com")


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content)
    assert run_git(repo, "add", name).returncode == 0
    assert (
        run_git(repo, "commit", "-q", "-m", message).returncode == 0
    ), run_git(repo, "commit", "-q", "-m", message).stderr


def attempt_merge(repo: Path, ref_to_merge: str) -> dict:
    """Mirror the workflow's merge step: attempt a merge, and on conflict
    record the conflicting files and leave the working tree clean by
    aborting. Returns a dict matching the shape of the workflow's step
    outputs (conflict / changed), plus the conflicting file list.
    """
    is_ancestor = run_git(
        repo, "merge-base", "--is-ancestor", ref_to_merge, "HEAD"
    )
    if is_ancestor.returncode == 0:
        return {"conflict": False, "changed": False, "conflicts": []}

    merge = run_git(repo, "merge", "--no-edit", ref_to_merge)
    if merge.returncode == 0:
        return {"conflict": False, "changed": True, "conflicts": []}

    conflicts = run_git(
        repo, "diff", "--name-only", "--diff-filter=U"
    ).stdout.splitlines()
    abort = run_git(repo, "merge", "--abort")
    assert abort.returncode == 0, abort.stderr
    return {"conflict": True, "changed": False, "conflicts": conflicts}


# ── workflow YAML structure ───────────────────────────────────────────


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW_PATH.is_file(), f"missing workflow file: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as f:
        return yaml.safe_load(f)


def test_workflow_parses_as_yaml(workflow):
    assert isinstance(workflow, dict)


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True in YAML 1.1.
    return workflow.get("on") or workflow.get(True)


def test_triggers_on_push_to_main(workflow):
    triggers = _triggers(workflow)
    assert "push" in triggers
    assert "main" in triggers["push"]["branches"]


def test_triggers_on_daily_schedule(workflow):
    triggers = _triggers(workflow)
    assert "schedule" in triggers
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert "0 6 * * *" in crons


def test_triggers_on_workflow_dispatch(workflow):
    triggers = _triggers(workflow)
    assert "workflow_dispatch" in triggers


def test_has_concurrency_group(workflow):
    assert "concurrency" in workflow
    assert workflow["concurrency"]["group"]


def test_checkout_uses_full_history(workflow):
    raw = WORKFLOW_PATH.read_text()
    assert "fetch-depth: 0" in raw


def test_configures_bot_git_identity(workflow):
    raw = WORKFLOW_PATH.read_text()
    assert "github-actions[bot]" in raw


def test_uses_github_token_for_auth(workflow):
    raw = WORKFLOW_PATH.read_text()
    assert "secrets.GITHUB_TOKEN" in raw


def test_no_hardcoded_secrets_outside_github_token(workflow):
    raw = WORKFLOW_PATH.read_text()
    # Guard against accidentally wiring in a PAT/custom secret for a
    # workflow that's meant to run with default permissions only.
    assert "secrets." in raw  # sanity: something references secrets
    for line in raw.splitlines():
        if "secrets." in line:
            assert "secrets.GITHUB_TOKEN" in line


# ── merge logic: clean vs. conflicting ────────────────────────────────


def test_clean_merge_fast_forwards_without_conflict(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "base\n", "base commit")

    run_git(repo, "branch", "prod-live-patches")

    # Advance main only.
    commit_file(repo, "b.txt", "new file on main\n", "add b.txt on main")

    run_git(repo, "checkout", "-q", "prod-live-patches")
    result = attempt_merge(repo, "main")

    assert result == {"conflict": False, "changed": True, "conflicts": []}
    assert (repo / "b.txt").exists()
    # Working tree is clean, no leftover merge state.
    status = run_git(repo, "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_already_up_to_date_is_a_noop(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "base\n", "base commit")
    run_git(repo, "branch", "prod-live-patches")

    run_git(repo, "checkout", "-q", "prod-live-patches")
    result = attempt_merge(repo, "main")

    assert result == {"conflict": False, "changed": False, "conflicts": []}


def test_conflicting_merge_is_detected_and_aborted(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "base\n", "base commit")
    run_git(repo, "branch", "prod-live-patches")

    # Diverge: main and prod-live-patches both edit a.txt differently.
    commit_file(repo, "a.txt", "changed on main\n", "edit a.txt on main")

    run_git(repo, "checkout", "-q", "prod-live-patches")
    commit_file(
        repo, "a.txt", "changed on prod-live-patches\n", "edit a.txt on prod"
    )

    result = attempt_merge(repo, "main")

    assert result["conflict"] is True
    assert result["changed"] is False
    assert result["conflicts"] == ["a.txt"]

    # merge --abort must have left the tree clean and mid-merge state gone.
    status = run_git(repo, "status", "--porcelain")
    assert status.stdout.strip() == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_conflicting_merge_leaves_original_branch_content_intact(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "base\n", "base commit")
    run_git(repo, "branch", "prod-live-patches")

    commit_file(repo, "a.txt", "changed on main\n", "edit a.txt on main")

    run_git(repo, "checkout", "-q", "prod-live-patches")
    commit_file(
        repo, "a.txt", "changed on prod-live-patches\n", "edit a.txt on prod"
    )

    attempt_merge(repo, "main")

    # After an aborted conflicting merge, prod-live-patches must still show
    # its own content — we never want to push a half-resolved merge.
    assert (repo / "a.txt").read_text() == "changed on prod-live-patches\n"


def test_multiple_conflicting_files_all_reported(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "base a\n", "base a")
    commit_file(repo, "b.txt", "base b\n", "base b")
    run_git(repo, "branch", "prod-live-patches")

    (repo / "a.txt").write_text("main a\n")
    (repo / "b.txt").write_text("main b\n")
    run_git(repo, "add", "a.txt", "b.txt")
    run_git(repo, "commit", "-q", "-m", "edit both on main")

    run_git(repo, "checkout", "-q", "prod-live-patches")
    (repo / "a.txt").write_text("prod a\n")
    (repo / "b.txt").write_text("prod b\n")
    run_git(repo, "add", "a.txt", "b.txt")
    run_git(repo, "commit", "-q", "-m", "edit both on prod")

    result = attempt_merge(repo, "main")

    assert result["conflict"] is True
    assert sorted(result["conflicts"]) == ["a.txt", "b.txt"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
