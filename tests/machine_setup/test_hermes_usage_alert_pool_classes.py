"""Pool/probe-state-primary alert classes for hermes_usage_alert.py

(ClickUp 86e2mb8p5, PR 3/4 of the adversarial provider-failure taxonomy
epic). Covers: usage_cap and missing_credential paging via credential-pool
state (not log-grep), sibling suppression, cooldown-until-reset_at, and
that unconfigured/idle-refreshable classes never page through this alarm
either — the same misclassification boundary
tests/machine_setup/test_degraded_secrets_monitor_taxonomy.py exercises,
proven again end-to-end through this script's own main().

Lives under the main ``tests/`` CI lane, mirroring
tests/machine_setup/test_provider_probe.py's per-test unique-module-name
loading pattern (and machine-setup/mini-scripts/tests/test_hermes_usage_alert.py's
pr_pipeline sys.path shim for slack_msg_builder, which this file also needs
since it exercises the real _build_alert path).

IMPORTANT: ``module.time`` is the real stdlib ``time`` module (a process-wide
singleton) — freezing "now" MUST go through pytest's ``monkeypatch`` fixture
(auto-restored after each test), never a bare attribute assignment, or it
leaks into every other test in the session.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
MODULE_PATH = SCRIPTS / "hermes_usage_alert.py"
_COUNTER = 0

# 2026-08-03T22:25:00Z — matches the 86e2mdfhx live-evidence timestamp.
NOW = datetime(2026, 8, 3, 22, 25, tzinfo=timezone.utc).timestamp()


def _load_module():
    global _COUNTER
    _COUNTER += 1
    dependency_root = SCRIPTS / "pr_pipeline"
    sys.path.insert(0, str(dependency_root))
    try:
        spec = importlib.util.spec_from_file_location(f"hermes_usage_alert_pool_ut_{_COUNTER}", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(dependency_root))
    return module


@pytest.fixture()
def module(monkeypatch):
    mod = _load_module()
    # Freeze "now" for the whole test via monkeypatch so it's guaranteed to
    # be restored on teardown — module.time IS the real stdlib time module.
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    return mod


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["hermes"], returncode, stdout, stderr)


def _prepare(mod, tmp_path, *, pool):
    mod.STATE_PATH = str(tmp_path / "state.json")
    mod.RECEIPT_PATH = str(tmp_path / "receipt.json")
    mod.JOBS_PATH = str(tmp_path / "jobs.json")
    mod.LOGS = []
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"credential_pool": pool}), encoding="utf-8")
    mod.AUTH_PATH = str(auth_path)
    Path(mod.JOBS_PATH).write_text(json.dumps({"jobs": []}), encoding="utf-8")
    return auth_path


def test_usage_cap_pages_via_pool_state_with_no_log_signal(module, tmp_path):
    """A pool-only usage_cap signal (no matching log line at all) still

    pages — pool state is primary, not merely a fallback for log-grep.
    """
    _prepare(module, tmp_path, pool={
        "codex": [{
            "id": "c",
            "access_token": "tok",
            "last_status": "exhausted",
            "last_error_reset_at": "2099-01-01T00:00:00Z",
        }]
    })
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    assert sender.call_count == 1
    sent = sender.call_args.args[0]
    assert "usage cap" in sent.lower() or "usage_cap" in sent.lower()
    assert "codex" in sent


def test_missing_credential_pages_via_pool_state(module, tmp_path):
    _prepare(module, tmp_path, pool={
        "xai": [{"id": "x", "access_token": "tok", "last_status": "invalid"}]
    })
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    assert sender.call_count == 1
    sent = sender.call_args.args[0]
    assert "missing" in sent.lower() or "broken" in sent.lower()
    assert "xai" in sent


def test_usage_cap_suppressed_by_healthy_sibling(module, tmp_path):
    """Sibling suppression (built into degraded_secrets_monitor's

    classify_credential_pool) carries through: a capped entry with a
    healthy sibling never reaches this alarm's hits at all.
    """
    _prepare(module, tmp_path, pool={
        "codex": [
            {
                "id": "primary",
                "access_token": "tok",
                "last_status": "exhausted",
                "last_error_reset_at": "2099-01-01T00:00:00Z",
            },
            {"id": "backup", "access_token": "backup-tok", "last_status": "ok"},
        ]
    })
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    sender.assert_not_called()


def test_unconfigured_provider_never_pages(module, tmp_path):
    """The measured live copilot/gemini false page (86e2mdfhx) must stay

    silent through this alarm too.
    """
    _prepare(module, tmp_path, pool={"copilot": [], "gemini": []})
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    sender.assert_not_called()


def test_nous_idle_refreshable_never_pages(module, tmp_path):
    """The measured live nous idle-but-refreshable false page (86e2mdfhx)

    must stay silent through this alarm too — the exhausted_session class
    it maps to is never in _POOL_STATUS_TO_KIND.
    """
    _prepare(module, tmp_path, pool={
        "nous": [{
            "id": "primary",
            "last_status": "ok",
            "request_count": 0,
            "refresh_token": "a-real-refresh-token",
            "expires_at": "2026-08-03T21:40:47Z",  # expired 45 min before NOW
        }]
    })
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    sender.assert_not_called()


def test_unconfigured_entry_never_pages(module, tmp_path):
    _prepare(module, tmp_path, pool={"openrouter": [{"id": "scaffold"}]})
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        rc = module.main()

    assert rc == 0
    sender.assert_not_called()


def test_probe_mode_surfaces_pool_events_without_mutating_state(module, tmp_path, monkeypatch):
    _prepare(module, tmp_path, pool={
        "xai": [{"id": "x", "access_token": "tok", "last_status": "invalid"}]
    })
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--probe"])
    rc = module.main()

    assert rc == 1
    assert not Path(module.STATE_PATH).exists()


# ── _scan_pool_events / _pool_cooldown_elapsed unit coverage ───────────────

def test_scan_pool_events_maps_taxonomy_classes_to_alert_kinds(module, tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "credential_pool": {
            "codex": [{
                "id": "c",
                "access_token": "tok",
                "last_status": "exhausted",
                "last_error_reset_at": "2099-01-01T00:00:00Z",
            }],
            "xai": [{"id": "x", "access_token": "tok", "last_status": "invalid"}],
            "copilot": [],
        }
    }), encoding="utf-8")

    events = module._scan_pool_events(str(auth_path), NOW)

    kinds = {(e["kind"], e["key"]) for e in events}
    assert kinds == {
        (module.USAGE_CAP_KIND, "codex:c"),
        (module.MISSING_CREDENTIAL_KIND, "xai:x"),
    }
    usage_cap_event = next(e for e in events if e["kind"] == module.USAGE_CAP_KIND)
    assert usage_cap_event["retry_at"] == "2099-01-01T00:00:00+00:00"


def test_pool_cooldown_usage_cap_blocks_before_earliest_reset_at(module):
    events = [{"retry_at": "2026-08-03T23:00:00+00:00"}]  # 35 min after NOW
    last_alert = NOW - (module.USAGE_CAP_FLOOR_COOLDOWN_S + 1)  # floor already elapsed, already alerted once

    elapsed = module._pool_cooldown_elapsed(module.USAGE_CAP_KIND, events, last_alert, NOW)

    assert elapsed is False


def test_pool_cooldown_usage_cap_fires_immediately_on_first_ever_alert(module):
    """A brand-new cap always fires on the FIRST alert regardless of how

    far out its reset_at is — the reset_at gate only suppresses REPEATS.
    """
    events = [{"retry_at": "2099-01-01T00:00:00+00:00"}]
    never_alerted = 0.0

    elapsed = module._pool_cooldown_elapsed(module.USAGE_CAP_KIND, events, never_alerted, NOW)

    assert elapsed is True


def test_pool_cooldown_usage_cap_opens_once_reset_at_passes(module):
    reset_epoch = NOW + 60
    events = [{"retry_at": module.datetime_from_timestamp(reset_epoch)}]
    last_alert = NOW - (module.USAGE_CAP_FLOOR_COOLDOWN_S + 1)

    still_blocked = module._pool_cooldown_elapsed(module.USAGE_CAP_KIND, events, last_alert, NOW)
    now_after_reset = reset_epoch + 1
    opened = module._pool_cooldown_elapsed(module.USAGE_CAP_KIND, events, last_alert, now_after_reset)

    assert still_blocked is False
    assert opened is True


def test_pool_cooldown_usage_cap_respects_floor_even_past_reset_at(module):
    """Even once reset_at has passed, a usage_cap re-alert never fires

    faster than the floor cooldown — protects against a flapping/garbage
    reset_at value.
    """
    events = [{"retry_at": module.datetime_from_timestamp(NOW - 3600)}]  # already reset
    last_alert = NOW - 5  # far under the floor

    elapsed = module._pool_cooldown_elapsed(module.USAGE_CAP_KIND, events, last_alert, NOW)

    assert elapsed is False


def test_pool_cooldown_missing_credential_uses_fixed_floor(module):
    events = [{"retry_at": None}]
    just_under = NOW - (module.MISSING_CREDENTIAL_COOLDOWN_S - 1)
    just_over = NOW - (module.MISSING_CREDENTIAL_COOLDOWN_S + 1)

    assert module._pool_cooldown_elapsed(module.MISSING_CREDENTIAL_KIND, events, just_under, NOW) is False
    assert module._pool_cooldown_elapsed(module.MISSING_CREDENTIAL_KIND, events, just_over, NOW) is True
