"""Focused behavior tests for the acting orphan PR sweep."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "orphan_pr_sweep.py"
PR_WORKFLOW_SKILL = ROOT / "skills" / "github" / "github-pr-workflow" / "SKILL.md"
TASK = "86e2gmgc3"


def _load_module():
    spec = importlib.util.spec_from_file_location("orphan_pr_sweep", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_task_identity_is_tri_state_and_structured_marker_has_precedence():
    sweep = _load_module()

    unique = sweep._task_identity("hermes/retry", f"<!-- clickup-task-id: {TASK} -->", "86e29q8pg")
    assert unique.status == "unique"
    assert unique.task_id == TASK
    assert sweep._task_identity("no task here").status == "absent"
    assert sweep._task_identity(TASK, "86e29q8pg").status == "ambiguous"
    assert sweep._task_identity(
        f"<!-- clickup-task-id: {TASK} -->",
        "<!-- clickup-task-id: 86e29q8pg -->",
    ).status == "ambiguous"


def test_pr_workflow_skill_documents_exhaustive_tri_state_marker_inventory():
    guidance = PR_WORKFLOW_SKILL.read_text(encoding="utf-8")

    assert "gh api --paginate --slurp" in guidance
    assert "gh pr list --state open --limit 200" not in guidance
    assert "clickup-task-id:" in guidance
    assert "Canonical markers have precedence" in guidance
    assert "found | absent | unknown" in guidance
    assert "ACTIVE_BOT_PREFIXES" in guidance


def test_existing_pr_lookup_fails_closed_on_api_error_and_malformed_json(monkeypatch):
    sweep = _load_module()
    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(1, stderr="boom"))
    assert sweep._existing_pr("hermes/retry").status == "unknown"

    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(stdout="not json"))
    assert sweep._existing_pr("hermes/retry").status == "unknown"

    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(stdout="[]"))
    assert sweep._existing_pr("hermes/retry").status == "absent"


def test_open_pr_inventory_uses_exhaustive_pagination_and_indexes_last_page(monkeypatch):
    sweep = _load_module()
    commands = []
    pages = [
        [{"number": n, "head": {"ref": f"feature/{n}"}, "title": "other", "body": "", "user": {"login": "human"}} for n in range(1, 201)],
        [{"number": 201, "head": {"ref": "hermes/retry-a"}, "title": "fix", "body": f"<!-- clickup-task-id: {TASK} -->", "user": {"login": "hermes-dev-assistant[bot]"}}],
    ]

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return _proc(stdout=json.dumps(pages))

    monkeypatch.setattr(sweep, "_run", fake_run)
    indexed = sweep._open_prs_by_task("acme/repo")

    assert indexed[TASK][0]["number"] == 201
    assert commands == [[
        "gh", "api", "--paginate", "--slurp",
        "repos/acme/repo/pulls?state=open&per_page=100",
    ]]


def test_open_pr_inventory_refuses_malformed_or_incomplete_shape(monkeypatch):
    sweep = _load_module()
    for payload in ("not-json", "{}", "[{}]", '[[{"number": 1, "head": null}]]'):
        monkeypatch.setattr(sweep, "_run", lambda cmd, payload=payload, **kwargs: _proc(stdout=payload))
        assert sweep._open_prs_by_task("acme/repo") is None


def test_inventory_fails_closed_for_unidentified_bot_branch_but_ignores_human_false_match(monkeypatch):
    sweep = _load_module()
    bot_without_task = [[{
        "number": 4,
        "head": {"ref": "hermes/retry"},
        "title": "fix",
        "body": "no task marker",
        "user": {"login": "hermes-dev-assistant[bot]"},
    }]]
    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(stdout=json.dumps(bot_without_task)))
    assert sweep._open_prs_by_task("acme/repo") is None

    human_ambiguous = [[{
        "number": 5,
        "head": {"ref": "hermes/human-experiment"},
        "title": f"compare {TASK} with 86e29q8pg",
        "body": "discussion only",
        "user": {"login": "human"},
    }]]
    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(stdout=json.dumps(human_ambiguous)))
    assert sweep._open_prs_by_task("acme/repo") == {}


def test_inventory_uses_active_custom_prefix_for_fail_closed_bot_detection(monkeypatch):
    sweep = _load_module()
    bot_without_task = [[{
        "number": 6,
        "head": {"ref": "custom/retry"},
        "title": "fix",
        "body": "no task marker",
        "user": {"login": "hermes-dev-assistant[bot]"},
    }]]
    monkeypatch.setattr(
        sweep,
        "_run",
        lambda cmd, **kwargs: _proc(stdout=json.dumps(bot_without_task)),
    )

    assert sweep._open_prs_by_task("acme/repo", prefixes=("custom/",)) is None
    assert sweep._open_prs_by_task("acme/repo", prefixes=("agent/",)) == {}


def test_sweep_threads_active_custom_prefixes_into_inventory(monkeypatch):
    sweep = _load_module()
    seen = []
    monkeypatch.setattr(sweep, "_remote_branches", lambda prefixes, remote: ["custom/retry"])

    def inventory(gh_repo, prefixes=sweep.DEFAULT_PREFIXES):
        seen.append(prefixes)
        return None

    monkeypatch.setattr(sweep, "_open_prs_by_task", inventory)

    assert sweep.sweep(prefixes=("custom/",), gh_repo="acme/repo") == 1
    assert seen == [("custom/",)]


def test_bot_branch_with_absent_or_ambiguous_identity_fails_closed(monkeypatch):
    sweep = _load_module()
    creates = []
    monkeypatch.setattr(sweep, "_open_prs_by_task", lambda gh_repo, prefixes=sweep.DEFAULT_PREFIXES: {})
    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: creates.append(cmd) or _proc())

    monkeypatch.setattr(sweep, "_last_commit_subject_body", lambda ref, gitdir=None: ("fix", "no task"))
    assert not sweep._create_pr("hermes/retry", "main", False, open_prs_by_task={})

    monkeypatch.setattr(sweep, "_last_commit_subject_body", lambda ref, gitdir=None: (TASK, "86e29q8pg"))
    assert not sweep._create_pr("agent/retry", "main", False, open_prs_by_task={})
    assert creates == []


def test_create_body_roundtrips_through_inventory_without_unrelated_task_id(monkeypatch, tmp_path):
    sweep = _load_module()
    inventory = sweep._open_prs_by_task
    created_body = []
    monkeypatch.setenv("HERMES_ORPHAN_PR_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setattr(sweep, "_last_commit_subject_body", lambda ref, gitdir=None: ("fix: recover", f"ClickUp task {TASK}"))
    monkeypatch.setattr(sweep, "_open_prs_by_task", lambda gh_repo, prefixes=sweep.DEFAULT_PREFIXES: {})

    def create_run(cmd, **kwargs):
        created_body.append(cmd[cmd.index("--body") + 1])
        return _proc(stdout="https://github.test/acme/repo/pull/7")

    monkeypatch.setattr(sweep, "_run", create_run)
    assert sweep._create_pr("hermes/retry", "main", False, gh_repo="acme/repo", open_prs_by_task={})
    assert "86e29q8pg" not in created_body[0]
    assert f"<!-- clickup-task-id: {TASK} -->" in created_body[0]

    row = [[{"number": 7, "head": {"ref": "hermes/retry"}, "title": "fix: recover", "body": created_body[0], "user": {"login": "hermes-dev-assistant[bot]"}}]]
    monkeypatch.setattr(sweep, "_run", lambda cmd, **kwargs: _proc(stdout=json.dumps(row)))
    assert inventory("acme/repo")[TASK][0]["number"] == 7


def _concurrent_create_worker(start, lock_root, registry, result_queue, branch):
    sweep = _load_module()
    os.environ["HERMES_ORPHAN_PR_LOCK_ROOT"] = lock_root
    sweep._last_commit_subject_body = lambda ref, gitdir=None: ("fix: recover", f"ClickUp task {TASK}")

    def inventory(gh_repo, prefixes=sweep.DEFAULT_PREFIXES):
        # Deliberately model GitHub read-after-write lag: both processes see an
        # empty API inventory, so only the shared lease reservation can dedupe.
        return {}

    def run(cmd, **kwargs):
        with open(registry, "a", encoding="utf-8") as handle:
            handle.write("created\n")
            handle.flush()
            os.fsync(handle.fileno())
        return _proc(stdout="https://github.test/acme/repo/pull/9")

    sweep._open_prs_by_task = inventory
    sweep._run = run
    start.wait(5)
    ok = sweep._create_pr(branch, "main", False, gh_repo="acme/repo", open_prs_by_task={})
    result_queue.put(ok)


def test_two_distinct_branches_for_same_task_create_at_most_one_pr(tmp_path):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    results = ctx.Queue()
    registry = str(tmp_path / "created.txt")
    lock_root = str(tmp_path / "locks")
    processes = [
        ctx.Process(
            target=_concurrent_create_worker,
            args=(start, lock_root, registry, results, branch),
        )
        for branch in ("hermes/retry-a", "hermes/retry-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    assert [results.get(timeout=1) for _ in processes] == [True, True]
    assert Path(registry).read_text(encoding="utf-8").splitlines() == ["created"]


def test_process_death_after_remote_create_leaves_durable_pending_reservation(tmp_path, monkeypatch):
    sweep = _load_module()
    monkeypatch.setenv("HERMES_ORPHAN_PR_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setattr(
        sweep,
        "_last_commit_subject_body",
        lambda ref, gitdir=None: ("fix: recover", f"ClickUp task {TASK}"),
    )
    monkeypatch.setattr(sweep, "_open_prs_by_task", lambda gh_repo, prefixes=sweep.DEFAULT_PREFIXES: {})

    class ProcessDeath(BaseException):
        pass

    calls = []

    def remote_create_then_die(cmd, **kwargs):
        calls.append(cmd)
        raise ProcessDeath

    monkeypatch.setattr(sweep, "_run", remote_create_then_die)
    try:
        sweep._create_pr("hermes/retry-a", "main", False, open_prs_by_task={})
    except ProcessDeath:
        pass
    else:
        raise AssertionError("simulated process death did not escape")

    reservation_path = tmp_path / "locks" / f"{TASK}.lock"
    reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    assert reservation["state"] == "pending"
    assert reservation["branch"] == "hermes/retry-a"

    def duplicate_create(cmd, **kwargs):
        raise AssertionError(f"duplicate create attempted: {cmd}")

    monkeypatch.setattr(sweep, "_run", duplicate_create)
    assert sweep._create_pr("hermes/retry-b", "main", False, open_prs_by_task={})
    assert len(calls) == 1


def test_reservations_bound_future_clock_skew_and_clean_stale_or_malformed(tmp_path, monkeypatch):
    sweep = _load_module()
    now = 1_000_000.0
    monkeypatch.setattr(sweep.time, "time", lambda: now)
    path = tmp_path / "lease"

    with path.open("w+", encoding="utf-8") as handle:
        json.dump({
            "state": "pending",
            "branch": "hermes/retry",
            "created_at": now + sweep.MAX_CLOCK_SKEW_SECONDS,
        }, handle)
        handle.flush()
        assert sweep._fresh_reservation(handle)["branch"] == "hermes/retry"

    invalid_reservations = (
        {"state": "pending", "branch": "hermes/retry", "created_at": now + sweep.MAX_CLOCK_SKEW_SECONDS + 1},
        {"state": "pending", "branch": "hermes/retry", "created_at": now - sweep.RESERVATION_TTL_SECONDS - 1},
        {"state": "pending", "branch": "", "created_at": now},
        "not-json",
    )
    for reservation in invalid_reservations:
        with path.open("w+", encoding="utf-8") as handle:
            if isinstance(reservation, str):
                handle.write(reservation)
            else:
                json.dump(reservation, handle)
            handle.flush()
            assert sweep._fresh_reservation(handle) is None
            handle.seek(0)
            assert handle.read() == ""


def test_sweep_fails_closed_when_branch_pr_lookup_is_unknown(monkeypatch):
    sweep = _load_module()
    monkeypatch.setattr(sweep, "_remote_branches", lambda prefixes, remote: [f"hermes/{TASK}"])
    monkeypatch.setattr(sweep, "_ahead_of_base", lambda branch, base, remote: True)
    monkeypatch.setattr(sweep, "_open_prs_by_task", lambda gh_repo, prefixes=sweep.DEFAULT_PREFIXES: {})
    monkeypatch.setattr(sweep, "_existing_pr", lambda branch, gh_repo: sweep.PrLookup("unknown"))
    monkeypatch.setattr(sweep, "_clickup_needs_validation_tasks", lambda: [])

    assert sweep.sweep() == 1
