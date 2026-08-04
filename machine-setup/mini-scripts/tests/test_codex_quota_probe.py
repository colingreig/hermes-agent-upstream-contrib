#!/usr/bin/env python3
"""Contract tests for codex_quota_probe.py (ClickUp 86e2kxk50).

Generalized for 86e2mb8p0 (PR 2/4): the script now delegates its entry loop
to provider_probe.py's generalized engine, loaded as a sibling module via
``importlib`` (not a plain ``import provider_probe`` — that would need this
directory on ``sys.path``, which mutated at module scope leaks into every
other file a test runner collects in the same process)."""
from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "codex_quota_probe.py"
SCRIPT_DIR = SOURCE.parent
LAUNCHD_PLIST = SCRIPT_DIR / "launchd" / "com.colingreig.hermes.codex-quota-probe.plist"

SPEC = importlib.util.spec_from_file_location("codex_quota_probe_under_test", SOURCE)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


def _write_auth_store(tmp_path, entries) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"credential_pool": {"openai-codex": entries}}, indent=2)
    )


def _read_auth_store(tmp_path) -> dict:
    return json.loads((tmp_path / "hermes" / "auth.json").read_text())


def _codex_entry(entry_id: str, *, reset_at) -> dict:
    return {
        "id": entry_id,
        "label": entry_id,
        "auth_type": "oauth",
        "priority": 0,
        "source": f"device_code:{entry_id}",
        "access_token": f"access-{entry_id}",
        "refresh_token": f"refresh-{entry_id}",
        "last_status": "exhausted",
        "last_status_at": time.time(),
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": reset_at,
    }


def test_probe_entry_classifies_200_as_usable():
    entry = mock.Mock(access_token="tok", base_url=None)
    result = monitor.probe_entry(entry, http_get=lambda *a: (200, b"{}"))
    assert result == monitor.ProbeResult(True, 200, "ok")


def test_probe_entry_classifies_usage_limit_reached_429_as_unusable():
    entry = mock.Mock(access_token="tok", base_url=None)
    body = json.dumps({"error": {"type": "usage_limit_reached"}}).encode()
    result = monitor.probe_entry(entry, http_get=lambda *a: (429, body))
    assert result.usable is False
    assert result.status_code == 429
    assert result.detail == "usage_limit_reached"


def test_probe_entry_classifies_other_429_shapes_as_unusable_but_distinct():
    entry = mock.Mock(access_token="tok", base_url=None)
    result = monitor.probe_entry(entry, http_get=lambda *a: (429, b"not json"))
    assert result.usable is False
    assert result.detail == "429:unknown"


def test_probe_entry_classifies_401_as_unusable():
    entry = mock.Mock(access_token="tok", base_url=None)
    result = monitor.probe_entry(entry, http_get=lambda *a: (401, b""))
    assert result == monitor.ProbeResult(False, 401, "unauthorized")


def test_probe_entry_never_raises_on_transport_failure():
    entry = mock.Mock(access_token="tok", base_url=None)

    def _boom(*a):
        raise ConnectionError("no route to host")

    result = monitor.probe_entry(entry, http_get=_boom)
    assert result.usable is False
    assert result.status_code is None
    assert "ConnectionError" in result.detail


def test_probe_entry_treats_missing_credential_as_unusable_without_network_call():
    entry = mock.Mock(access_token="", base_url=None)
    calls = []
    result = monitor.probe_entry(entry, http_get=lambda *a: calls.append(a) or (200, b"{}"))
    assert result.usable is False
    assert result.detail == "missing_credential"
    assert calls == []


def test_probe_entry_never_leaks_the_access_token_in_its_result():
    secret = "super-secret-codex-token"
    entry = mock.Mock(access_token=secret, base_url=None)
    result = monitor.probe_entry(entry, http_get=lambda *a: (200, b"{}"))
    assert secret not in repr(result)


def test_run_probe_clears_stale_exhaustion_on_a_successful_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    result = monitor.run_probe(
        "openai-codex", now=now, http_get=lambda *a: (200, b"{}"),
    )

    assert result["frozen_count"] == 1
    assert [c["id"] for c in result["cleared"]] == ["primary"]
    assert result["still_exhausted"] == []
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openai-codex"][0]["last_status"] is None


def test_run_probe_leaves_genuinely_exhausted_entry_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    def _still_limited(*a):
        return 429, json.dumps({"error": {"type": "usage_limit_reached"}}).encode()

    result = monitor.run_probe("openai-codex", now=now, http_get=_still_limited)

    assert result["cleared"] == []
    assert [s["id"] for s in result["still_exhausted"]] == ["primary"]
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openai-codex"][0]["last_status"] == "exhausted"


def test_run_probe_isolates_each_account_independently(tmp_path, monkeypatch):
    """Dual-account fleet (primary + backup): one recovers, the other doesn't
    — clearing the recovered account must never touch its sibling."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path,
        [
            _codex_entry("primary-9dc40b", reset_at=now + 5 * 24 * 3600),
            _codex_entry("backup-75ac96", reset_at=now + 5 * 24 * 3600),
        ],
    )

    def _fake_get(access_token, base_url, timeout):
        if "primary" in access_token:
            return 200, b"{}"
        return 429, json.dumps({"error": {"type": "usage_limit_reached"}}).encode()

    result = monitor.run_probe("openai-codex", now=now, http_get=_fake_get)

    assert [c["id"] for c in result["cleared"]] == ["primary-9dc40b"]
    assert [s["id"] for s in result["still_exhausted"]] == ["backup-75ac96"]
    store = _read_auth_store(tmp_path)
    entries = {e["id"]: e for e in store["credential_pool"]["openai-codex"]}
    assert entries["primary-9dc40b"]["last_status"] is None
    assert entries["backup-75ac96"]["last_status"] == "exhausted"


def test_run_probe_skips_already_expired_entries_without_a_network_call(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now - 60)])
    calls = []

    result = monitor.run_probe(
        "openai-codex", now=now, http_get=lambda *a: calls.append(a) or (200, b"{}"),
    )

    assert result["frozen_count"] == 0
    assert calls == []


def test_dry_run_never_persists_a_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    result = monitor.run_probe(
        "openai-codex", now=now, http_get=lambda *a: (200, b"{}"), dry_run=True,
    )

    assert result["cleared"][0]["dry_run"] is True
    assert result["cleared"][0]["persisted"] is False
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openai-codex"][0]["last_status"] == "exhausted"


def test_cli_within_one_hour_drill_clears_a_stale_exhausted_mark(tmp_path, monkeypatch, capsys):
    """End-to-end drill matching the acceptance criterion: an account marked
    exhausted with a days-out reset_at, but actually available again right
    now, must have its stale mark cleared by a single monitor invocation —
    comfortably inside a one-hour window. Exercises the real ``main()`` CLI
    entry point; only the network boundary (``_default_http_get``) is faked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    argv = [str(SOURCE), "--json", "--now", str(now)]
    with mock.patch.object(monitor, "_default_http_get", return_value=(200, b"{}")):
        with mock.patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as raised:
                monitor.main()

    assert raised.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in payload["cleared"]] == ["primary"]
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openai-codex"][0]["last_status"] is None


def test_alert_fires_once_per_clear_and_stays_silent_when_nothing_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(monitor, "STATE_PATH", str(tmp_path / "state.json"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    slack = mock.Mock(return_value=True)
    monkeypatch.setattr(monitor, "_send_slack", slack)
    argv = [str(SOURCE), "--alert", "--now", str(now)]
    with mock.patch.object(monitor, "_default_http_get", return_value=(200, b"{}")):
        with mock.patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as raised:
                monitor.main()
        assert raised.value.code == 0
        assert slack.call_count == 1
        assert "primary" in slack.call_args[0][0]

        # Second run: nothing left to clear (already cleared) — silent, no re-alert.
        with mock.patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                monitor.main()
    assert slack.call_count == 1


def test_dry_run_alert_never_posts_for_a_simulated_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(monitor, "STATE_PATH", str(tmp_path / "state.json"))
    now = time.time()
    _write_auth_store(tmp_path, [_codex_entry("primary", reset_at=now + 5 * 24 * 3600)])

    slack = mock.Mock(return_value=True)
    monkeypatch.setattr(monitor, "_send_slack", slack)
    argv = [str(SOURCE), "--alert", "--dry-run", "--now", str(now)]
    with mock.patch.object(sys, "argv", argv):
        with mock.patch.object(monitor, "probe_entry", return_value=monitor.ProbeResult(True, 200, "ok")):
            with pytest.raises(SystemExit):
                monitor.main()

    assert slack.call_count == 0


def test_malformed_auth_store_is_handled_without_crashing(tmp_path, monkeypatch, capsys):
    """A malformed auth.json is a fail-safe empty store upstream (matches the
    degraded-secrets monitor's precedent) — nothing to probe, not a crash."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True)
    (tmp_path / "hermes" / "auth.json").write_text("{not-json")

    argv = [str(SOURCE), "--json"]
    with mock.patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as raised:
            monitor.main()
    assert raised.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["frozen_count"] == 0


def test_pool_probe_exception_exits_nonzero_and_reports_error_class(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    argv = [str(SOURCE), "--json"]
    with mock.patch("agent.credential_pool.load_pool", side_effect=RuntimeError("boom")):
        with mock.patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as raised:
                monitor.main()
    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "RuntimeError"


def test_launchagent_contract_targets_the_runtime_venv_and_a_sub_hour_interval():
    payload = plistlib.loads(LAUNCHD_PLIST.read_bytes())

    assert payload["Label"] == "com.colingreig.hermes.codex-quota-probe"
    assert payload["ProgramArguments"] == [
        "/Users/colingreig/.hermes/runtime-current/venv/bin/python",
        "/Users/colingreig/.hermes/scripts/codex_quota_probe.py",
        "--alert",
    ]
    # Sub-hour interval so a real recovery clears comfortably inside the
    # drill's one-hour acceptance bound.
    assert 0 < payload["StartInterval"] <= 3600
    assert payload["RunAtLoad"] is True
    env = payload["EnvironmentVariables"]
    assert env["HOME"] == "/Users/colingreig"
    assert env["HERMES_HOME"] == "/Users/colingreig/.hermes"
    assert env["CODEX_QUOTA_PROBE_ALERT_SLACK"] == "slack:D0BA2PM9CFM"
    serialized = LAUNCHD_PLIST.read_text(encoding="utf-8")
    assert "CLICKUP_API_TOKEN" not in serialized
    assert "op://" not in serialized


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
