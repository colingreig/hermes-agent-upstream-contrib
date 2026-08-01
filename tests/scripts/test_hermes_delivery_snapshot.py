from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "hermes_delivery_snapshot" / "live-sources.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


producer = _load("hermes_delivery_snapshot_test", SCRIPTS / "hermes_delivery_snapshot.py")
delivery = _load("task_delivery_snapshot_test", SCRIPTS / "task_delivery.py")
NOW = datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc)


def _config() -> dict:
    return {
        "clickup_list_id": "901714465284",
        "mini_host": "mini",
        "lookback_hours": 72,
        "max_tasks": 40,
    }


def test_fixture_builds_repo_stacked_no_pr_and_mini_delivery_chains():
    snapshot = producer.build_snapshot(
        producer.FixtureBackend(FIXTURE), _config(), now=NOW
    )

    assert set(snapshot) == {"schema", "generated_at", "collection", "tasks", "watch"}
    assert all(state["status"] == "OK" for state in snapshot["collection"].values())
    tasks = {task["task"]["id"]: task for task in snapshot["tasks"]}
    assert set(tasks) == {"REPO-1", "STACK-1", "NOPR-1", "MINI-1"}
    assert tasks["STACK-1"]["stacked"] is True
    assert [pr["number"] for pr in tasks["STACK-1"]["pull_requests"]] == [10, 11]
    assert tasks["NOPR-1"]["allow_no_pr"] is True
    assert tasks["NOPR-1"]["ci"]["runs"][0]["run_id"] == "303"
    assert tasks["MINI-1"]["task"]["lane"] == "mini"
    assert tasks["MINI-1"]["release"]["head_sha"] == "d" * 40
    assert tasks["REPO-1"]["executor"]["run_id"] == "repo-run"
    assert tasks["MINI-1"]["executor"]["run_id"] == "mini-run-1"
    assert [
        owner["task_id"] for owner in snapshot["watch"]["owners"]
    ] == ["ACTIVE-1"]
    assert snapshot["watch"]["review_gate"]["status"] == "clean"
    assert snapshot["watch"]["review_gate"]["consecutive_clean_runs"] == 3
    assert snapshot["watch"]["lifecycle_events"] == [
        {
            "classification": "transition",
            "false_alert": False,
            "id": "life-1",
            "timestamp": "2026-07-29T15:50:36Z",
            "valid": True,
        }
    ]
    assert snapshot["watch"]["promotions"][0]["certified"] is True

    correlations = {
        task_id: delivery.correlate(task)
        for task_id, task in tasks.items()
    }
    assert {result["delivery_status"] for result in correlations.values()} == {
        "DELIVERED"
    }


def test_missing_live_source_is_explicit_unknown_and_never_synthesizes_success(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del fixture["github_prs"]["owner/repo#1"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )

    source = snapshot["collection"]["github:owner/repo#1"]
    assert source["status"] == "UNKNOWN"
    task = next(task for task in snapshot["tasks"] if task["task"]["id"] == "REPO-1")
    assert task["sources"]["github:owner/repo#1"]["status"] == "UNKNOWN"
    assert delivery.correlate(task)["delivery_status"] == "UNKNOWN"


def test_pr_job_check_cannot_override_failed_governing_workflow(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # The PR rollup fixture still contains a successful job check; the
    # independently read workflow run is authoritative.
    fixture["github_runs"]["owner/repo#201"]["conclusion"] = "failure"
    broken = tmp_path / "failed-workflow.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )
    task = next(task for task in snapshot["tasks"] if task["task"]["id"] == "REPO-1")

    assert task["ci"]["runs"][0]["conclusion"] == "failure"
    result = delivery.correlate(task)
    assert result["delivery_status"] == "INCOMPLETE"
    assert result["identity_mismatches"] == [
        "ci_exact_terminal_success:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]


def test_offline_cli_writes_only_the_requested_atomic_snapshot(tmp_path):
    config = tmp_path / "config.json"
    output = tmp_path / "state" / "snapshot.json"
    config.write_text(
        json.dumps(
            {
                "delivery_snapshot": _config(),
                "delivery_watch": {
                    "collectors": [
                        {"name": "fixture", "kind": "file", "path": str(output)}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "hermes_delivery_snapshot.py"),
            "--once",
            "--fixture",
            str(FIXTURE),
            "--observed-at",
            NOW.isoformat(),
            "--config",
            str(config),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "OK"
    assert summary["task_count"] == 4
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == producer.SCHEMA
    assert list(output.parent.iterdir()) == [output]


def test_fixed_observation_time_is_fixture_only(tmp_path, capsys):
    config = tmp_path / "config.json"
    output = tmp_path / "snapshot.json"
    config.write_text(json.dumps({"delivery_snapshot": _config()}), encoding="utf-8")

    result = producer.main(
        [
            "--once",
            "--observed-at",
            NOW.isoformat(),
            "--config",
            str(config),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert "only allowed with --fixture" in capsys.readouterr().out
    assert not output.exists()


def test_live_commands_are_shell_free_and_remote_queries_are_fixed(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    class Result:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    backend = producer.LiveBackend(mini_host="mini")
    backend.ssh = "/usr/bin/ssh"
    backend.mini_sqlite("ledger")

    argv, kwargs = calls[0]
    assert argv[:7] == [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "mini",
        "/usr/bin/sqlite3",
    ]
    assert "-readonly" in argv
    assert kwargs.get("shell") is None
    assert kwargs["stdin"] is None
    assert kwargs["input"].startswith(b"SELECT id,job_id")


def test_admission_reads_active_singleton_and_bounded_recent_history(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    class Result:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    backend = producer.LiveBackend(mini_host="mini")
    backend.ssh = "/usr/bin/ssh"
    backend.mini_sqlite("admission")
    backend.mini_sqlite("admission_history")

    active_query = calls[0][1]["input"].decode()
    history_query = calls[1][1]["input"].decode()
    assert "WHERE state='active' LIMIT 1" in active_query
    assert "executor_lease_history" in history_query
    assert "'-72 hours'" in history_query
    assert "LIMIT 100" in history_query


def test_mini_runtime_uses_fixed_macos_readlink_argv(monkeypatch):
    calls: list[list[str]] = []
    backend = producer.LiveBackend(mini_host="mini")

    def fake_ssh(remote_argv, **_kwargs):
        calls.append(remote_argv)
        if remote_argv[0] == "/usr/bin/readlink":
            return b"/Users/colingreig/.hermes/releases/v0.18.2-aaaaaaaaaaaa\n"
        return (b"a" * 40) + b"\n"

    monkeypatch.setattr(backend, "_ssh", fake_ssh)

    assert backend.mini_runtime() == {
        "target": "/Users/colingreig/.hermes/releases/v0.18.2-aaaaaaaaaaaa",
        "head_sha": "a" * 40,
    }
    assert calls == [
        ["/usr/bin/readlink", "/Users/colingreig/.hermes/runtime-current"],
        [
            "/usr/bin/git",
            "-C",
            "/Users/colingreig/.hermes/runtime-current",
            "rev-parse",
            "HEAD^{commit}",
        ],
    ]


def test_clickup_reads_disable_the_cli_cache(monkeypatch, tmp_path):
    calls: list[tuple[list[str], dict]] = []

    class Result:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    cli = tmp_path / "clickup.mjs"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    backend = producer.LiveBackend(mini_host="mini")
    backend.node = "/usr/bin/node"
    backend.clickup_cli = cli
    backend.clickup_list("901714465284")

    assert calls[0][1]["env"]["CLICKUP_NO_CACHE"] == "1"


def test_ledger_evidence_never_falls_back_to_clickup_text(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["mini_sqlite"]["ledger"] = [
        row
        for row in fixture["mini_sqlite"]["ledger"]
        if row.get("id") != "repo-execution"
    ]
    broken = tmp_path / "missing-ledger.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )
    task = next(task for task in snapshot["tasks"] if task["task"]["id"] == "REPO-1")

    assert task["ledger"] == {}
    assert task["sources"]["mini:ledger-join:REPO-1"]["status"] == "UNKNOWN"
    assert delivery.correlate(task)["delivery_status"] == "UNKNOWN"


def test_missing_admission_history_cannot_correlate_completed_delivery(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["mini_sqlite"]["admission_history"] = []
    broken = tmp_path / "missing-history.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )
    task = next(
        task for task in snapshot["tasks"] if task["task"]["id"] == "REPO-1"
    )

    assert task["executor"]["run_id"] is None
    assert task["sources"]["mini:ledger-join:REPO-1"]["status"] == "UNKNOWN"
    assert delivery.correlate(task)["delivery_status"] == "UNKNOWN"


def test_detached_and_unadmitted_live_owners_are_visible_to_overlap_alerts():
    mini = {
        "admission": [
            {
                "task_id": "TASK-A",
                "job_id": "executor",
                "owner_run_id": "admitted",
                "fencing_token": 7,
                "acquired_at": "2026-07-29T16:00:00Z",
                "heartbeat_at": "2026-07-29T16:29:00Z",
                "expires_at": "2026-07-29T17:00:00Z",
                "ledger_execution_id": "exec-a",
                "state": "active",
            }
        ],
        "ledger": [
            {
                "id": "exec-a",
                "job_id": "executor",
                "pid": 100,
                "owner_token": "cron-a",
                "status": "running",
                "claimed_at": "2026-07-29T16:00:00Z",
                "started_at": "2026-07-29T16:00:01Z",
                "heartbeat_at": "2026-07-29T16:29:00Z",
                "lease_expires_at": "2026-07-29T17:00:00Z",
            },
            {
                "id": "exec-b",
                "job_id": "executor",
                "pid": 200,
                "owner_token": "cron-b",
                "status": "running",
                "claimed_at": "2026-07-29T16:10:00Z",
                "started_at": "2026-07-29T16:10:01Z",
                "heartbeat_at": "2026-07-29T16:29:00Z",
                "lease_expires_at": "2026-07-29T17:00:00Z",
            },
        ],
        "claims": [{"_path": "/x/legacy.claim", "task_id": "legacy", "pid": 300}],
        "processes": [
            {"pid": 100, "command": "gateway"},
            {"pid": 200, "command": "clickup-queue-poller"},
            {"pid": 300, "command": "ignite-execute"},
            {"pid": 400, "command": "opencode_exec detached"},
        ],
    }

    owners = producer._owners(mini)
    assert {owner["owner_kind"] for owner in owners if owner.get("owner_kind")} == {
        "cron-ledger",
        "legacy-claim",
        "detached-process",
    }
    watch = _load("watch_for_owner_test", SCRIPTS / "hermes_delivery_watch.py")
    alerts = watch.evaluate_alerts(
        {"watch": {"owners": owners}}, [], now=NOW
    )
    kinds = {alert["kind"] for alert in alerts}
    assert "duplicate_ownership" in kinds
    assert "unfenced_owner" in kinds


def test_unconfigured_workflow_path_is_unknown(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["github_runs"]["owner/repo#201"]["path"] = ".github/workflows/release.yml"
    broken = tmp_path / "wrong-workflow.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )
    task = next(task for task in snapshot["tasks"] if task["task"]["id"] == "REPO-1")
    assert task["sources"]["github-run:owner/repo#201"]["status"] == "UNKNOWN"
    assert delivery.correlate(task)["delivery_status"] == "UNKNOWN"


def test_legacy_or_wrong_target_release_cannot_authorize_mini(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["mini_json"]["release"]["schema_version"] = 1
    broken = tmp_path / "legacy-release.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")

    snapshot = producer.build_snapshot(
        producer.FixtureBackend(broken), _config(), now=NOW
    )
    task = next(task for task in snapshot["tasks"] if task["task"]["id"] == "MINI-1")
    assert task["deployment"]["target"] is None
    assert task["release"]["authority"] is None
    assert task["sources"]["mini:delivery-join:MINI-1"]["status"] == "UNKNOWN"


def test_expired_whole_poll_deadline_stops_before_subprocess(monkeypatch):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(producer.subprocess, "run", forbidden)
    with __import__("pytest").raises(producer.SnapshotError, match="deadline exhausted"):
        producer.LiveBackend._run(
            ["/usr/bin/false"],
            timeout=30,
            source="deadline-fixture",
            deadline=time.monotonic() - 1,
        )
    assert called is False


def test_config_clamps_work_below_launchd_cadence(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "delivery_snapshot": {
                    "clickup_list_id": "901714465284",
                    "max_tasks": 999,
                    "poll_timeout_seconds": 999,
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = producer._load_config(config)

    assert loaded["max_tasks"] == 25
    assert loaded["poll_timeout_seconds"] == 240
    assert loaded["poll_timeout_seconds"] < 300
