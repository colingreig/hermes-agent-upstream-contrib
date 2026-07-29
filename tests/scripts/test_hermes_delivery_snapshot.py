from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
