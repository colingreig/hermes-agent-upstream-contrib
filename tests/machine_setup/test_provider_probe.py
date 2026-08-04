"""Contract tests for machine-setup/mini-scripts/provider_probe.py — the
generalized multi-provider probe (ClickUp 86e2mb8p0, PR 2/4 of the
provider-failure taxonomy epic, building on 86e2mb8nv PR 1's classifier +
credential-pool taxonomy).

Lives under the main ``tests/`` CI lane (not
``machine-setup/mini-scripts/tests/``, which is NOT part of any CI workflow
and has known cross-file ``sys.modules`` pollution when scripts loaded there
do plain sibling imports) — see ``tests/machine_setup/test_fleet_outcome_probe.py``
for the same per-test unique-module-name loading pattern this file reuses.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
MODULE_PATH = SCRIPTS / "provider_probe.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"provider_probe_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def monitor():
    return _load_module()


def _write_auth_store(tmp_path, provider: str, entries: list[dict]) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"credential_pool": {provider: entries}}, indent=2)
    )


def _read_auth_store(tmp_path) -> dict:
    return json.loads((tmp_path / "hermes" / "auth.json").read_text())


def _entry(entry_id: str, *, reset_at, status: str = "exhausted", extra: dict | None = None) -> dict:
    base = {
        "id": entry_id,
        "label": entry_id,
        "auth_type": "api_key",
        "priority": 0,
        "source": f"manual:{entry_id}",
        "access_token": f"access-{entry_id}",
        "last_status": status,
        "last_status_at": time.time(),
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": reset_at,
    }
    if extra:
        base.update(extra)
    return base


# ── Adapter table ─────────────────────────────────────────────────────────

def test_resolve_adapter_maps_registered_providers(monitor):
    assert monitor.resolve_adapter("openai-codex") is monitor.probe_codex_usage_entry
    assert monitor.resolve_adapter("anthropic") is monitor.probe_anthropic_entry
    assert monitor.resolve_adapter("openrouter") is monitor.probe_openrouter_entry


def test_resolve_adapter_falls_back_to_generic_for_unregistered_provider(monitor):
    assert monitor.resolve_adapter("some-unregistered-provider") is monitor.probe_generic_entry
    # Case/whitespace-insensitive, matching the credential pool's own provider key handling.
    assert monitor.resolve_adapter(" OpenAI-Codex ".strip().lower()) is monitor.probe_codex_usage_entry


# ── Codex /usage adapter — session/weekly headroom ─────────────────────────

def test_codex_usage_adapter_classifies_low_usage_as_usable_with_headroom(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    body = json.dumps({
        "rate_limit": {
            "primary_window": {"used_percent": 12.5},
            "secondary_window": {"used_percent": 40.0},
        }
    }).encode()
    result = monitor.probe_codex_usage_entry(entry, http_get=lambda *a: (200, body))
    assert result.usable is True
    assert result.usage_percent == 40.0


def test_codex_usage_adapter_classifies_exhausted_window_as_unusable(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    body = json.dumps({
        "rate_limit": {
            "primary_window": {"used_percent": 100.0},
            "secondary_window": {"used_percent": 55.0},
        }
    }).encode()
    result = monitor.probe_codex_usage_entry(entry, http_get=lambda *a: (200, body))
    assert result.usable is False
    assert result.detail == "usage_cap:session"
    assert result.usage_percent == 100.0


def test_codex_usage_adapter_treats_empty_body_as_usable(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    result = monitor.probe_codex_usage_entry(entry, http_get=lambda *a: (200, b"{}"))
    assert result.usable is True
    assert result.usage_percent is None


def test_codex_usage_adapter_classifies_401_and_429(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    assert monitor.probe_codex_usage_entry(entry, http_get=lambda *a: (401, b"")).detail == "unauthorized"
    body = json.dumps({"error": {"type": "usage_limit_reached"}}).encode()
    result = monitor.probe_codex_usage_entry(entry, http_get=lambda *a: (429, body))
    assert result.usable is False and result.detail == "usage_limit_reached"


def test_codex_usage_adapter_never_raises_on_transport_failure(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)

    def _boom(*a):
        raise TimeoutError("connect timed out")

    result = monitor.probe_codex_usage_entry(entry, http_get=_boom)
    assert result.usable is False
    assert result.detail == "probe_error:TimeoutError"


# ── Anthropic count_tokens adapter ──────────────────────────────────────────

def test_anthropic_adapter_classifies_200_401_403_429(monitor):
    entry = mock.Mock(access_token="sk-ant-api-tok", base_url=None)
    assert monitor.probe_anthropic_entry(entry, http_get=lambda *a: (200, b"{}")).usable is True
    assert monitor.probe_anthropic_entry(entry, http_get=lambda *a: (401, b"")).detail == "unauthorized"
    assert monitor.probe_anthropic_entry(entry, http_get=lambda *a: (403, b"")).detail == "forbidden"
    result = monitor.probe_anthropic_entry(entry, http_get=lambda *a: (429, b"not json"))
    assert result.usable is False and result.detail == "429:unknown"


def test_anthropic_adapter_missing_credential_never_calls_network(monitor):
    entry = mock.Mock(access_token="", base_url=None)
    calls = []
    result = monitor.probe_anthropic_entry(entry, http_get=lambda *a: calls.append(a) or (200, b"{}"))
    assert result.detail == "missing_credential"
    assert calls == []


# ── OpenRouter key-check adapter ────────────────────────────────────────────

def test_openrouter_adapter_computes_usage_percent_from_key_payload(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    body = json.dumps({"data": {"usage": 25.0, "limit": 100.0}}).encode()
    result = monitor.probe_openrouter_entry(entry, http_get=lambda *a: (200, body))
    assert result.usable is True
    assert result.usage_percent == 25.0


def test_openrouter_adapter_classifies_401_and_429(monitor):
    entry = mock.Mock(access_token="tok", base_url=None)
    assert monitor.probe_openrouter_entry(entry, http_get=lambda *a: (401, b"")).detail == "unauthorized"
    assert monitor.probe_openrouter_entry(entry, http_get=lambda *a: (429, b"")).detail == "rate_limited"


# ── Generic /models fallback adapter ────────────────────────────────────────

def test_generic_adapter_classifies_200_and_401(monitor):
    entry = mock.Mock(access_token="tok", base_url="https://example.test/v1")
    assert monitor.probe_generic_entry(entry, http_get=lambda *a: (200, b"{}")).usable is True
    assert monitor.probe_generic_entry(entry, http_get=lambda *a: (401, b"")).detail == "unauthorized"


# ── Cost guards: 10 min/entry, 12/provider/hr ───────────────────────────────

def test_cost_guard_allows_first_probe_and_blocks_within_interval(monitor):
    state: dict = {}
    now = time.time()
    assert monitor._cost_guard_allows(state, "anthropic", "e1", now) is True
    monitor._cost_guard_record(state, "anthropic", "e1", now)
    assert monitor._cost_guard_allows(state, "anthropic", "e1", now + 60) is False
    assert monitor._cost_guard_allows(state, "anthropic", "e1", now + monitor.MIN_PROBE_INTERVAL_SECONDS + 1) is True


def test_cost_guard_caps_probes_per_provider_per_hour(monitor):
    state: dict = {}
    now = time.time()
    for i in range(monitor.MAX_PROBES_PER_PROVIDER_HOUR):
        entry_id = f"e{i}"
        assert monitor._cost_guard_allows(state, "openrouter", entry_id, now) is True
        monitor._cost_guard_record(state, "openrouter", entry_id, now)
    # 13th distinct entry in the same rolling hour is throttled by the
    # provider-wide cap even though ITS OWN 10-minute window is untouched.
    assert monitor._cost_guard_allows(state, "openrouter", "e-overflow", now) is False
    # An hour later the window has rolled fully off.
    assert monitor._cost_guard_allows(state, "openrouter", "e-overflow", now + 3601) is True


def test_run_probe_skips_entries_throttled_by_cost_guard(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "anthropic", [_entry("primary", reset_at=now + 5 * 24 * 3600)])
    state = {"entries": {"anthropic:primary": now - 30}}  # probed 30s ago, well under 10 min

    calls = []
    result = monitor.run_probe(
        "anthropic", now=now,
        http_get=lambda *a: calls.append(a) or (200, b"{}"),
        cost_guard_state=state,
    )
    assert calls == []
    assert [g["id"] for g in result["skipped_cost_guard"]] == ["primary"]
    assert result["cleared"] == []


# ── Never probe DEAD entries ─────────────────────────────────────────────

def test_run_probe_never_probes_a_dead_entry(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path, "anthropic",
        [
            _entry("alive", reset_at=now + 5 * 24 * 3600, status="exhausted"),
            _entry("gone", reset_at=now + 5 * 24 * 3600, status="dead"),
        ],
    )
    calls = []
    result = monitor.run_probe(
        "anthropic", now=now,
        http_get=lambda *a: calls.append(a[0]) or (200, b"{}"),
    )
    assert result["frozen_count"] == 1
    assert [c["id"] for c in result["cleared"]] == ["alive"]
    assert calls == ["access-alive"]


# ── Zero configured entries → not_configured, no HTTP call ────────────────

def test_run_probe_zero_entries_is_not_configured_without_any_http_call(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, "xai-oauth", [])
    calls = []
    result = monitor.run_probe("xai-oauth", http_get=lambda *a: calls.append(a) or (200, b"{}"))
    assert result["configured"] is False
    assert result["frozen_count"] == 0
    assert calls == []


# ── record_probe_verdict() integration ──────────────────────────────────

def test_run_probe_records_usable_verdict_on_a_successful_probe(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "openrouter", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    monitor.run_probe("openrouter", now=now, http_get=lambda *a: (200, b"{}"))

    store = _read_auth_store(tmp_path)
    entry = store["credential_pool"]["openrouter"][0]
    assert entry["last_probe_verdict"] == monitor.VERDICT_USABLE


def test_run_probe_records_still_unusable_verdict_on_a_genuine_rejection(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "openrouter", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    monitor.run_probe("openrouter", now=now, http_get=lambda *a: (401, b""))

    store = _read_auth_store(tmp_path)
    entry = store["credential_pool"]["openrouter"][0]
    assert entry["last_probe_verdict"] == monitor.VERDICT_STILL_UNUSABLE
    assert entry["last_status"] == "exhausted"  # never promoted/demoted by PR2


def test_dry_run_never_calls_record_probe_verdict(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "openrouter", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    monitor.run_probe("openrouter", now=now, http_get=lambda *a: (200, b"{}"), dry_run=True)

    store = _read_auth_store(tmp_path)
    entry = store["credential_pool"]["openrouter"][0]
    assert entry.get("last_probe_verdict") is None
    assert entry["last_status"] == "exhausted"


# ── Control-host network arbitration ────────────────────────────────────

def test_transport_failure_with_control_host_down_is_inconclusive_not_provider_side(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "anthropic", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    def _boom(*a):
        raise ConnectionError("no route to host")

    result = monitor.run_probe(
        "anthropic", now=now, http_get=_boom,
        control_host_check=lambda: False,  # local network is ALSO down
    )
    assert result["cleared"] == []
    assert result["still_exhausted"] == []
    assert [n["id"] for n in result["inconclusive_network"]] == ["primary"]

    store = _read_auth_store(tmp_path)
    entry = store["credential_pool"]["anthropic"][0]
    assert entry["last_probe_verdict"] == monitor.VERDICT_INCONCLUSIVE_LOCAL_NETWORK
    assert entry["last_status"] == "exhausted"  # untouched — never blamed on the credential


def test_transport_failure_with_control_host_up_is_confirmed_provider_side(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(tmp_path, "anthropic", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    def _boom(*a):
        raise ConnectionError("connection refused")

    result = monitor.run_probe(
        "anthropic", now=now, http_get=_boom,
        control_host_check=lambda: True,  # everything else reaches the internet fine
    )
    assert result["inconclusive_network"] == []
    assert [s["id"] for s in result["still_exhausted"]] == ["primary"]

    store = _read_auth_store(tmp_path)
    entry = store["credential_pool"]["anthropic"][0]
    assert entry["last_probe_verdict"] == monitor.VERDICT_CONFIRMED_PROVIDER_SIDE


# ── Old pools tolerated without crash ───────────────────────────────────

def test_old_pool_entry_missing_failure_taxonomy_fields_probes_without_crashing(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    # A pool entry exactly as written by a pre-86e2mb8nv runtime: none of the
    # PR1 failure-taxonomy fields (last_failure_kind, last_probe_verdict, ...)
    # exist on disk at all.
    legacy_entry = {
        "id": "legacy",
        "label": "legacy",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual:legacy",
        "access_token": "access-legacy",
        "last_status": "exhausted",
        "last_status_at": now,
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": now + 5 * 24 * 3600,
    }
    _write_auth_store(tmp_path, "openrouter", [legacy_entry])

    result = monitor.run_probe("openrouter", now=now, http_get=lambda *a: (200, b"{}"))

    assert [c["id"] for c in result["cleared"]] == ["legacy"]
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openrouter"][0]["last_status"] is None


# ── main() CLI ────────────────────────────────────────────────────────────

def test_main_json_reports_not_configured_for_an_empty_provider(monitor, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(monitor, "COST_GUARD_STATE_PATH", str(tmp_path / "cost-guard.json"))
    _write_auth_store(tmp_path, "openrouter", [])

    with pytest.raises(SystemExit) as raised:
        monitor.main(["--provider", "openrouter", "--json"])

    assert raised.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] is False


def test_main_exits_nonzero_and_reports_error_class_on_pool_load_failure(monitor, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(monitor, "COST_GUARD_STATE_PATH", str(tmp_path / "cost-guard.json"))

    with mock.patch("agent.credential_pool.load_pool", side_effect=RuntimeError("boom")):
        with pytest.raises(SystemExit) as raised:
            monitor.main(["--provider", "anthropic", "--json"])

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "RuntimeError"


def test_main_persists_cost_guard_state_across_invocations(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    cost_guard_path = str(tmp_path / "cost-guard.json")
    monkeypatch.setattr(monitor, "COST_GUARD_STATE_PATH", cost_guard_path)
    now = time.time()
    _write_auth_store(tmp_path, "openrouter", [_entry("primary", reset_at=now + 5 * 24 * 3600)])

    with mock.patch.object(monitor, "_default_control_host_check", return_value=True):
        with mock.patch.object(monitor, "probe_openrouter_entry", return_value=monitor.ProbeResult(False, 401, "unauthorized")):
            with pytest.raises(SystemExit):
                monitor.main(["--provider", "openrouter", "--json", "--now", str(now)])

    saved = json.loads(Path(cost_guard_path).read_text())
    assert saved["entries"]["openrouter:primary"] == now


# ── codex_quota_probe.py thin-wrapper sibling import ────────────────────

def test_codex_quota_probe_imports_provider_probe_as_a_sibling():
    """Guards the ``import provider_probe`` sibling-import contract the
    thin wrapper relies on — both files must be deployed together (see
    fleet_outcome_manifest.json) or the deployed codex-quota-probe launchd
    job breaks with ModuleNotFoundError."""
    source = (SCRIPTS / "codex_quota_probe.py").read_text(encoding="utf-8")
    assert "import provider_probe" in source
    assert (SCRIPTS / "provider_probe.py").is_file()
