from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "mini_health_attestation.py"
SPEC = importlib.util.spec_from_file_location("mini_health_attestation", SOURCE)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def healthy_snapshot() -> dict:
    commit = "a" * 40
    return {
        "generated_at": "2026-07-27T00:00:00+00:00",
        "runtime": {
            "symlink": True,
            "target": "/home/test/.hermes/releases/v1.0.0-aaaaaaaaaaaa",
            "target_under_releases": True,
            "actual_sha": commit,
            "expected_sha": commit,
            "dirty_paths": [],
            "source_binding": {
                "release_dir": "v1.0.0-aaaaaaaaaaaa",
                "release_name_binds_sha": True,
                "sources": {
                    "attestor": {"path": "/rt/machine-setup/mini-scripts/x.py", "under_runtime": True},
                    "hermes_cli.config": {"path": "/rt/hermes_cli/config.py", "under_runtime": True},
                    "cron.jobs": {"path": "/rt/cron/jobs.py", "under_runtime": True},
                    "cron.executions": {"path": "/rt/cron/executions.py", "under_runtime": True},
                },
                "bound": True,
            },
        },
        "services": {
            "gateway": {
                "label": "ai.hermes.gateway",
                "registered": True,
                "pid": 100,
                "alive": True,
                "command_ok": True,
                "runtime_code_file_count": 3,
                "executable_in_release": True,
                "http_required": False,
            },
            "dashboard": {
                "label": "com.colingreig.hermes-dashboard",
                "registered": True,
                "pid": 101,
                "alive": True,
                "command_ok": True,
                "runtime_code_file_count": 4,
                "executable_in_release": True,
                "http_required": True,
                "http_url": "http://127.0.0.1:9119/health",
                "http_status": 200,
            },
        },
        "config": {
            "current": 33,
            "latest": 33,
            "receipt_valid": True,
            "receipt_detail": {"receipt_hash": "b" * 64},
        },
        "release": {
            "latest_hash_valid": True,
            "latest_hash": "c" * 64,
            "latest_event": "cut",
            "latest_from_commit": "d" * 40,
            "latest_to_commit": commit,
            "last_good": {
                "valid": True,
                "event": "cut",
                "to_commit": commit,
                "receipt_hash": "c" * 64,
            },
        },
        "governed": {
            "launchd-environment": {"ok": True, "detail": "verified"},
            "marketplace-skills": {"ok": True, "detail": "verified"},
            "pr-pipeline": {"ok": True, "detail": "verified"},
        },
        "execution": {
            "summary": {
                "inflight": 1,
                "stale": 0,
                "legacy_unfenced": 0,
                "insufficient_evidence": 0,
            },
            "entries": [
                {
                    "execution_id": "exec-1",
                    "job_id": "job-1",
                    "owner": {"token_present": True},
                    "lease": {"heartbeat_at": "now", "state": "current"},
                    "owner_liveness": "live",
                    "disposition": "live_or_unexpired",
                }
            ],
        },
        "skills": [{"name": "ignite-validate", "available": True}],
        "credentials": [
            {
                "job": "hermes-pr-validate",
                "name": "HERMES_VALIDATOR_FINALIZE_TOKEN",
                "available": True,
                "classification": "available",
            }
        ],
        "cron": [
            {
                "id": "job-1",
                "name": "hermes-pr-validate",
                "enabled": True,
                "last_status": "ok",
                "latest_execution_status": "completed",
            },
            {
                "id": "research-stage-monitor",
                "name": "research-stage-monitor",
                "enabled": True,
                "last_status": "ok",
                "latest_execution_status": "completed",
                "monitor_status": "healthy",
                "contract": {"served_rate": 1.0},
            },
        ],
    }


def result(snapshot: dict) -> dict:
    return module.evaluate_snapshot(snapshot)


def assert_fails(snapshot: dict, check_id: str) -> None:
    report = result(snapshot)
    assert report["healthy"] is False
    assert report["exit_code"] == 2
    assert any(
        item["id"] == check_id and item["state"] == "fail"
        for item in report["checks"]
    )


def test_healthy_snapshot_passes() -> None:
    report = result(healthy_snapshot())
    assert report["healthy"] is True
    assert report["exit_code"] == 0


def test_dirty_or_stale_release_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["runtime"]["dirty_paths"] = ["package-lock.json"]
    assert_fails(snapshot, "runtime.clean")

    snapshot = healthy_snapshot()
    snapshot["runtime"]["actual_sha"] = "e" * 40
    assert_fails(snapshot, "runtime.commit")


def test_config_mismatch_or_missing_receipt_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["config"]["current"] = 32
    assert_fails(snapshot, "config.schema")

    snapshot = healthy_snapshot()
    snapshot["config"]["receipt_valid"] = False
    assert_fails(snapshot, "config.migration-receipt")


def test_tampered_or_missing_receipt_sibling_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["release"]["latest_hash_valid"] = False
    assert_fails(snapshot, "release.latest-content-address")


def test_config_receipt_requires_immutable_sibling_and_backup_hash(tmp_path: Path) -> None:
    hermes = tmp_path / ".hermes"
    state = hermes / "state" / "config-migrations"
    state.mkdir(parents=True)
    backup = hermes / "backups" / "config-migrations" / "stamp" / "config.yaml"
    backup.parent.mkdir(parents=True)
    backup.write_text("old-config")
    backup.chmod(0o600)
    receipt = {
        "schema": module.CONFIG_RECEIPT_SCHEMA,
        "from_version": 32,
        "to_version": 33,
        "backup_path": str(backup),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    (state / "latest.json").write_bytes(payload)
    (state / f"receipt-{digest}.json").write_bytes(payload)

    valid, _ = module._config_receipt_status(hermes, 33, 33)
    assert valid is True
    backup.write_text("tampered")
    valid, detail = module._config_receipt_status(hermes, 33, 33)
    assert valid is False
    assert detail["backup_hash_valid"] is False


def test_config_receipt_rejects_nonprivate_or_out_of_scope_backup(
    tmp_path: Path,
) -> None:
    hermes = tmp_path / ".hermes"
    state = hermes / "state" / "config-migrations"
    state.mkdir(parents=True)
    backup = hermes / "backups" / "config-migrations" / "stamp" / "config.yaml"
    backup.parent.mkdir(parents=True)
    backup.write_text("old-config")

    def write_receipt(path: Path) -> None:
        receipt = {
            "schema": module.CONFIG_RECEIPT_SCHEMA,
            "from_version": 32,
            "to_version": 33,
            "backup_path": str(path),
            "backup_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        payload = (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        (state / "latest.json").write_bytes(payload)
        (state / f"receipt-{digest}.json").write_bytes(payload)

    backup.chmod(0o644)
    write_receipt(backup)
    valid, detail = module._config_receipt_status(hermes, 33, 33)
    assert valid is False
    assert detail["backup_private"] is False

    external = tmp_path / "external-config.yaml"
    external.write_text("old-config")
    external.chmod(0o600)
    write_receipt(external)
    valid, detail = module._config_receipt_status(hermes, 33, 33)
    assert valid is False
    assert detail["backup_scoped"] is False


def test_latest_rejection_preserves_last_good_activation() -> None:
    snapshot = healthy_snapshot()
    snapshot["release"].update(
        {
            "latest_event": "rejected",
            "latest_from_commit": snapshot["runtime"]["actual_sha"],
            "latest_to_commit": "f" * 40,
        }
    )
    report = result(snapshot)
    assert report["healthy"] is True
    assert any(
        item["id"] == "release.latest-attempt" and item["state"] == "warn"
        for item in report["checks"]
    )

    snapshot["release"]["last_good"]["valid"] = False
    assert_fails(snapshot, "release.last-good")


def test_stale_or_pid_reused_process_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["services"]["gateway"]["command_ok"] = False
    assert_fails(snapshot, "service.gateway.command")

    snapshot = healthy_snapshot()
    snapshot["services"]["gateway"]["runtime_code_file_count"] = 0
    snapshot["services"]["gateway"]["executable_in_release"] = False
    assert_fails(snapshot, "service.gateway.runtime")


def test_broken_skill_or_credential_preflight_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["skills"][0]["available"] = False
    assert_fails(snapshot, "skill.ignite-validate")

    snapshot = healthy_snapshot()
    snapshot["credentials"][0].update(
        {"available": False, "classification": "missing"}
    )
    assert_fails(
        snapshot,
        "credential.hermes-pr-validate.HERMES_VALIDATOR_FINALIZE_TOKEN",
    )


def test_validator_false_green_is_detected() -> None:
    snapshot = healthy_snapshot()
    snapshot["cron"][0]["latest_execution_status"] = "failed"
    assert_fails(snapshot, "cron.hermes-pr-validate")
    check = next(
        item for item in result(snapshot)["checks"]
        if item["id"] == "cron.hermes-pr-validate"
    )
    assert check["detail"]["false_green"] is True


def test_missing_or_inflight_latest_execution_is_not_accepted() -> None:
    for latest_status in (None, "claimed", "running"):
        snapshot = healthy_snapshot()
        snapshot["cron"][0]["latest_execution_status"] = latest_status
        assert_fails(snapshot, "cron.hermes-pr-validate")
        check = next(
            item
            for item in result(snapshot)["checks"]
            if item["id"] == "cron.hermes-pr-validate"
        )
        assert check["detail"]["false_green"] is True


def test_orphan_execution_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["execution"]["summary"]["stale"] = 1
    snapshot["execution"]["entries"][0].update(
        {"owner_liveness": "dead", "disposition": "stale"}
    )
    snapshot["execution"]["entries"][0]["lease"]["state"] = "expired"
    assert_fails(snapshot, "execution.inflight-classification")


def test_unhealthy_enabled_cron_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["cron"][0]["last_status"] = "error"
    assert_fails(snapshot, "cron.hermes-pr-validate")


def test_truthful_research_alert_remains_red() -> None:
    snapshot = healthy_snapshot()
    research = snapshot["cron"][1]
    research.update(
        {
            "last_status": "error",
            "latest_execution_status": "failed",
            "monitor_status": "degraded",
            "contract": {"served_rate": 0.57, "degraded_rate": 0.43},
        }
    )
    assert_fails(snapshot, "cron.research-stage-monitor")
    check = next(
        item for item in result(snapshot)["checks"]
        if item["id"] == "cron.research-stage-monitor"
    )
    assert check["detail"]["monitor_status"] == "degraded"
    assert check["detail"]["contract"]["served_rate"] == 0.57


def test_snapshot_cli_exit_code_matches_report(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(healthy_snapshot()))
    monkeypatch.setattr("sys.argv", ["mini-health", "--snapshot", str(path)])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True


def test_unbound_source_tree_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["runtime"]["source_binding"]["sources"]["cron.jobs"]["under_runtime"] = False
    snapshot["runtime"]["source_binding"]["bound"] = False
    assert_fails(snapshot, "runtime.source-binding")
    check = next(
        item for item in result(snapshot)["checks"]
        if item["id"] == "runtime.source-binding"
    )
    assert "cron.jobs" in check["detail"]["unbound_sources"]

    snapshot = healthy_snapshot()
    snapshot["runtime"]["source_binding"]["release_name_binds_sha"] = False
    snapshot["runtime"]["source_binding"]["bound"] = False
    assert_fails(snapshot, "runtime.source-binding")


def test_missing_source_binding_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["runtime"].pop("source_binding")
    assert_fails(snapshot, "runtime.source-binding")


def test_empty_inventories_fail_closed() -> None:
    for section, check_id in (
        ("services", "inventory.services"),
        ("governed", "inventory.governed"),
        ("skills", "inventory.skills"),
        ("credentials", "inventory.credentials"),
        ("cron", "inventory.cron"),
    ):
        snapshot = healthy_snapshot()
        snapshot[section] = type(snapshot[section])()
        assert_fails(snapshot, check_id)


def test_missing_expected_service_or_governed_asset_fails_closed() -> None:
    snapshot = healthy_snapshot()
    snapshot["services"].pop("dashboard")
    assert_fails(snapshot, "inventory.services")
    check = next(
        item for item in result(snapshot)["checks"]
        if item["id"] == "inventory.services"
    )
    assert "dashboard" in check["detail"]["missing"]

    snapshot = healthy_snapshot()
    snapshot["governed"].pop("pr-pipeline")
    assert_fails(snapshot, "inventory.governed")


def test_all_disabled_cron_inventory_fails_closed() -> None:
    snapshot = healthy_snapshot()
    for job in snapshot["cron"]:
        job["enabled"] = False
    assert_fails(snapshot, "inventory.cron")


def test_invalid_monitor_output_is_not_accepted() -> None:
    snapshot = healthy_snapshot()
    snapshot["cron"][1]["monitor_status"] = "invalid-output"
    assert_fails(snapshot, "cron.research-stage-monitor")


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_source_binding_release_name_gates_commit(tmp_path: Path) -> None:
    commit = "abcdef1234567890abcdef1234567890abcdef12"
    runtime = tmp_path / "releases" / f"v1.2.3-{commit[:7]}"
    runtime.mkdir(parents=True)
    binding = module._source_binding(runtime, commit)
    assert binding["release_name_binds_sha"] is True
    # The real attestor/module files are not under the throwaway runtime, so the
    # overall binding must fail closed even when the directory name matches.
    assert binding["bound"] is False
    assert "attestor" in binding["sources"]

    stale = tmp_path / "releases" / "hand-checkout"
    stale.mkdir()
    assert module._source_binding(stale, commit)["release_name_binds_sha"] is False


def test_collect_service_requires_loaded_release_code(tmp_path: Path, monkeypatch) -> None:
    commit = "abcdef1234567890abcdef1234567890abcdef12"
    runtime = tmp_path / "releases" / f"v1.2.3-{commit[:7]}"
    runtime.mkdir(parents=True)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    command = (
        f"{hermes_home / 'runtime-current'}/venv/bin/python "
        "-m hermes_cli.main gateway run"
    )
    lsof = "\n".join(
        [
            "p100",
            "fcwd",
            f"n{runtime}",  # working directory — must NOT bind
            "ftxt",
            f"n{runtime}/venv/bin/python3",  # executable — binds
            "fmem",
            f"n{runtime}/venv/lib/libhermes.dylib",  # loaded library — binds
            "f3",
            f"n{runtime}/logs/app.log",  # ordinary open file — must NOT bind
            "ftxt",
            "n/usr/lib/dyld",  # system text outside release — must NOT bind
        ]
    )

    def fake_run(argv, cwd=None, **_kwargs):
        program = argv[0]
        if program == "launchctl":
            return _Result(0, "\tpid = 100\n")
        if program == "ps":
            return _Result(0, command + "\n")
        if program == "lsof":
            return _Result(0, lsof + "\n")
        return _Result(0, "")

    monkeypatch.setattr(module, "_run", fake_run)
    service = module._collect_service(
        "gateway", module.SERVICE_CONTRACTS["gateway"], runtime, hermes_home
    )
    assert service["alive"] is True
    assert service["command_ok"] is True
    assert service["runtime_code_file_count"] == 2
    assert service["executable_in_release"] is True

    only_cwd = "\n".join(["p100", "fcwd", f"n{runtime}", "f3", f"n{runtime}/logs/app.log"])

    def cwd_only_run(argv, cwd=None, **_kwargs):
        if argv[0] == "lsof":
            return _Result(0, only_cwd + "\n")
        return fake_run(argv, cwd=cwd)

    monkeypatch.setattr(module, "_run", cwd_only_run)
    unbound = module._collect_service(
        "gateway", module.SERVICE_CONTRACTS["gateway"], runtime, hermes_home
    )
    assert unbound["runtime_code_file_count"] == 0
    assert unbound["executable_in_release"] is False


def test_collect_release_falls_back_on_missing_or_corrupt_receipt(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    runtime = releases / "v1.2.3-abcdef1"
    runtime.mkdir()
    empty = module._collect_release(releases, runtime, "a" * 40)
    assert empty["latest_hash_valid"] is False
    assert empty["last_good"] == {"valid": False}

    (releases / ".mini-release-last-receipt.json").write_text("{not-json")
    corrupt = module._collect_release(releases, runtime, "a" * 40)
    assert corrupt["latest_hash_valid"] is False
    assert corrupt["last_good"] == {"valid": False}


def test_safe_probe_captures_success_and_failure() -> None:
    assert module._safe_probe(lambda: "verified") == {"ok": True, "detail": "verified"}

    def boom() -> None:
        raise RuntimeError("mismatch")

    probe = module._safe_probe(boom)
    assert probe["ok"] is False
    assert probe["detail"] == "RuntimeError: mismatch"
