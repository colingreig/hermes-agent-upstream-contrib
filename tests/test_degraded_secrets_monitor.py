from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "machine-setup" / "mini-scripts" / "degraded_secrets_monitor.py"


def _load_monitor():
    spec = importlib.util.spec_from_file_location("degraded_secrets_monitor", MONITOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_auth(path: Path, credential_pool):
    path.write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": credential_pool}))


def test_check_credential_pool_healthy_statuses_do_not_trigger(tmp_path):
    monitor = _load_monitor()
    auth_file = tmp_path / "auth.json"
    _write_auth(auth_file, {
        "nous": [
            {"id": "a", "last_status": "ok"},
            {"id": "b", "last_status": None},
            {"id": "c", "last_status": "cooldown"},
        ]
    })

    result = monitor.check_credential_pool(str(auth_file))

    assert result["triggered"] is False
    assert result["status"] == "ok"
    assert result["hits"] == []


def test_check_credential_pool_degraded_statuses_trigger(tmp_path):
    monitor = _load_monitor()
    auth_file = tmp_path / "auth.json"
    _write_auth(auth_file, {
        "xai": [{"id": "x", "last_status": "invalid"}],
        "codex": [{"id": "c", "last_status": "exhausted"}],
        "nous": [{"id": "n", "last_status": "error"}],
    })

    result = monitor.check_credential_pool(str(auth_file))

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "codex", "id": "c", "status": "exhausted"},
        {"provider": "nous", "id": "n", "status": "error"},
        {"provider": "xai", "id": "x", "status": "invalid"},
    ]


def test_check_credential_pool_missing_malformed_and_absent_are_not_degraded(tmp_path):
    monitor = _load_monitor()
    missing = tmp_path / "missing-auth.json"
    malformed = tmp_path / "malformed-auth.json"
    absent = tmp_path / "absent-auth.json"
    malformed.write_text("{not-json")
    absent.write_text(json.dumps({"version": 1, "providers": {}}))

    assert monitor.check_credential_pool(str(missing))["status"] == "missing"
    malformed_result = monitor.check_credential_pool(str(malformed))
    assert malformed_result["status"] == "malformed"
    assert malformed_result["triggered"] is False
    absent_result = monitor.check_credential_pool(str(absent))
    assert absent_result["status"] == "absent"
    assert absent_result["triggered"] is False


def test_signature_includes_sorted_json_roundtrippable_credential_pool_hits():
    monitor = _load_monitor()
    fatal = {"triggered": False}
    parked_auth = {"triggered": False}
    placeholder = {"hits": []}
    credential_pool = {"hits": [
        {"provider": "xai", "id": "z", "status": "invalid"},
        {"provider": "codex", "id": "a", "status": "exhausted"},
    ]}

    sig = monitor._signature(fatal, parked_auth, placeholder, credential_pool)

    assert sig["credential_pool"] == [
        ["codex", "a", "exhausted"],
        ["xai", "z", "invalid"],
    ]
    assert json.loads(json.dumps(sig)) == sig


def test_old_signature_without_credential_pool_normalizes_for_dedup():
    monitor = _load_monitor()
    old_sig = {"fatal": True, "parked_auth": False, "placeholder_keys": []}

    assert monitor._normalize_signature(old_sig) == {
        "fatal": True,
        "parked_auth": False,
        "placeholder_keys": [],
        "credential_pool": [],
    }


def test_json_result_folds_credential_pool_into_degraded(tmp_path):
    auth_file = tmp_path / "auth.json"
    log_file = tmp_path / "gateway.error.log"
    log_file.write_text("")
    _write_auth(auth_file, {"nous": [{"id": "pool-1", "last_status": "error"}]})

    result = subprocess.run(
        [
            sys.executable,
            str(MONITOR_PATH),
            "--json",
            "--log-file",
            str(log_file),
            "--auth-file",
            str(auth_file),
            "--now",
            "2026-07-27T00:00:00+00:00",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["degraded"] is True
    assert payload["credential_pool"]["hits"] == [
        {"provider": "nous", "id": "pool-1", "status": "error"}
    ]


def test_dry_run_alert_formats_slack_and_clickup_without_secret_resolution(tmp_path):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    auth_file = tmp_path / "auth.json"
    log_file = tmp_path / "gateway.error.log"
    log_file.write_text("")
    _write_auth(auth_file, {"codex": [{"id": "pool-1", "last_status": "exhausted"}]})
    env = os.environ.copy()
    env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})
    env.pop("CLICKUP_API_TOKEN", None)

    result = subprocess.run(
        [
            sys.executable,
            str(MONITOR_PATH),
            "--alert",
            "--log-file",
            str(log_file),
            "--auth-file",
            str(auth_file),
            "--now",
            "2026-07-27T00:00:00+00:00",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 1
    assert "[degraded-secrets-monitor] DRY_RUN slack:" in result.stdout
    assert "<@UN4CQ1EGG>" in result.stdout
    assert "[degraded-secrets-monitor] DRY_RUN clickup comment on 86e2610g8:" in result.stdout
    assert "Credential pool degraded: provider 'codex' entry 'pool-1' last_status=exhausted" in result.stdout
    assert "[degraded-secrets-monitor] alerted (slack=True clickup=True)" in result.stdout
    assert result.stderr == ""


def test_alert_recovery_clears_credential_pool_dedup_state(tmp_path):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    state_dir = hermes_home / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "degraded-secrets-monitor.json"
    state_file.write_text(json.dumps({
        "last_alert_signature": {
            "fatal": False,
            "parked_auth": False,
            "placeholder_keys": [],
            "credential_pool": [["codex", "pool-1", "exhausted"]],
        }
    }))
    auth_file = tmp_path / "auth.json"
    log_file = tmp_path / "gateway.error.log"
    log_file.write_text("")
    _write_auth(auth_file, {"codex": [{"id": "pool-1", "last_status": "ok"}]})
    env = os.environ.copy()
    env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

    result = subprocess.run(
        [
            sys.executable,
            str(MONITOR_PATH),
            "--alert",
            "--log-file",
            str(log_file),
            "--auth-file",
            str(auth_file),
            "--now",
            "2026-07-27T00:00:00+00:00",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0
    assert "[degraded-secrets-monitor] healthy" in result.stdout
    assert "[degraded-secrets-monitor] recovered" in result.stdout
    assert json.loads(state_file.read_text())["last_alert_signature"] is None
