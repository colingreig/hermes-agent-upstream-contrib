"""Contract tests for machine-setup/mini-scripts/fleet_outcome_probe.py's
``credential_taxonomy`` operational-check kind — ClickUp 86e2mb8pb, PR 4/4 of
the provider-failure taxonomy epic (86e2mb8k3), building on PR 1's classifier
(86e2mb8nv), PR 2's provider probe (86e2mb8p0), and PR 3's per-class alerting
(86e2mb8p5).

Lives under the main ``tests/`` CI lane (not
``machine-setup/mini-scripts/tests/``, which is NOT part of any CI workflow)
— see ``tests/machine_setup/test_provider_probe.py`` for the same
per-test unique-module-name loading pattern this file reuses.

Only covers the NEW ``credential_taxonomy`` check added for 86e2mb8pb; the
rest of ``fleet_outcome_probe.py``'s contract surface is exercised by the
pre-existing (CI-uncovered) ``machine-setup/mini-scripts/tests/test_fleet_outcome_probe.py``.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
MODULE_PATH = SCRIPTS / "fleet_outcome_probe.py"
CONTRACTS_PATH = SCRIPTS / "fleet_outcome_contracts.json"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"fleet_outcome_probe_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _contract(*, path, **overrides):
    base = {
        "id": "credential-taxonomy-health",
        "kind": "credential_taxonomy",
        "path": str(path),
        # Empty by default so tests that aren't exercising the
        # expected_providers rule don't incidentally trip it; the two
        # zero-entry tests below opt in explicitly.
        "expected_providers": [],
        "dead_stale_hours": 6,
        "probe_stale_hours": 24,
    }
    base.update(overrides)
    return base


def _write_auth(tmp_path, pool: dict) -> Path:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"credential_pool": pool}, indent=2), encoding="utf-8")
    return auth_path


def test_canonical_credential_taxonomy_contract_is_wired():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    entry = next(
        item for item in contracts["operational_checks"] if item["id"] == "credential-taxonomy-health"
    )
    assert entry["kind"] == "credential_taxonomy"
    assert entry["expected_providers"]
    assert entry["dead_stale_hours"] > 0
    assert entry["probe_stale_hours"] > 0
    # The intentional-suppression rationale is decided to live in the
    # contract JSON, not a code comment — assert it's actually there.
    assert "usage_cap" in entry["note"]
    assert "exhausted_cap" in entry["note"]


def test_credential_taxonomy_missing_auth_file_is_silent(tmp_path):
    module = _load_module()
    contract = _contract(path=tmp_path / "does-not-exist.json")
    findings, evidence = module._check_operational_contracts([contract], home=tmp_path, now=NOW)
    assert findings == []
    assert evidence[0]["providers"] == 0


def test_credential_taxonomy_pages_zero_entry_expected_provider(tmp_path):
    module = _load_module()
    auth_path = _write_auth(tmp_path, {"anthropic": []})
    contract = _contract(path=auth_path, expected_providers=["anthropic"])
    findings, _evidence = module._check_operational_contracts([contract], home=tmp_path, now=NOW)
    assert [item["code"] for item in findings] == ["unconfigured_expected_provider"]
    assert "anthropic" in findings[0]["detail"]


def test_credential_taxonomy_never_pages_unexpected_zero_entry_provider(tmp_path):
    """86e2mb8p5's fixed false page (copilot/gemini stale-empty keys) must
    stay fixed here too: an unlisted provider with zero entries is normal
    and this contract only cares about providers Colin has declared
    expected_providers."""
    module = _load_module()
    auth_path = _write_auth(tmp_path, {
        "anthropic": [{"id": "cred-0", "last_status": "ok"}],
        "gemini": [],
    })
    contract = _contract(path=auth_path, expected_providers=["anthropic"])
    findings, _evidence = module._check_operational_contracts([contract], home=tmp_path, now=NOW)
    assert findings == []


def test_credential_taxonomy_pages_dead_credential_past_stale_threshold_not_before(tmp_path):
    module = _load_module()
    fresh_dead_at = (NOW - timedelta(hours=1)).timestamp()
    pool = {"copilot": [{"id": "cred-1", "last_status": "dead", "last_status_at": fresh_dead_at}]}
    auth_path = _write_auth(tmp_path, pool)
    contract = _contract(path=auth_path)

    # A fresh terminal mark is not this contract's job (the existing
    # per-hit Slack/ClickUp alerting already pages it immediately) — must
    # stay quiet before dead_stale_hours elapses.
    findings, _evidence = module._check_operational_contracts([contract], home=tmp_path, now=NOW)
    assert findings == []

    # Stuck past the threshold with no recovery: alarm distinguishably.
    pool["copilot"][0]["last_status_at"] = (NOW - timedelta(hours=7)).timestamp()
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["credential_stuck_dead"]
    assert "copilot:cred-1" in findings[0]["detail"]


def test_credential_taxonomy_pages_auth_permanent_quarantine_past_stale_threshold(tmp_path):
    module = _load_module()
    stuck_at = (NOW - timedelta(hours=9)).timestamp()
    pool = {
        "nous": [{
            "id": "cred-2",
            "last_status": "exhausted",
            "last_failure_kind": "auth_permanent",
            "last_status_at": stuck_at,
        }],
    }
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["credential_stuck_dead"]
    assert "nous:cred-2" in findings[0]["detail"]


def test_credential_taxonomy_pages_stale_probe_receipt_for_non_cap_exhausted(tmp_path):
    module = _load_module()
    stale_probe_at = (NOW - timedelta(hours=30)).timestamp()
    pool = {
        "xai-oauth": [{
            "id": "cred-3",
            "last_status": "exhausted",
            "last_failure_kind": "rate_limit_session",
            "last_probe_verdict": "still_unusable",
            "last_probe_verdict_at": stale_probe_at,
        }],
    }
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["stale_probe_receipt"]
    assert "xai-oauth:cred-3" in findings[0]["detail"]

    # A fresh probe receipt on the same still-exhausted entry means the
    # probe loop is alive — must clear.
    pool["xai-oauth"][0]["last_probe_verdict_at"] = (NOW - timedelta(hours=1)).timestamp()
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert findings == []


def test_credential_taxonomy_never_probed_entry_is_never_a_permanently_red_flag(tmp_path):
    """provider_probe.py (PR 2) ships with no scheduled cron yet — an entry
    that has NEVER been probed (no last_probe_verdict_at at all) must
    produce zero findings, not an alarm that can never clear because the
    infrastructure that would clear it doesn't run yet."""
    module = _load_module()
    pool = {
        "xai-oauth": [{
            "id": "cred-4",
            "last_status": "exhausted",
            "last_failure_kind": "rate_limit_session",
        }],
    }
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert findings == []


def test_credential_taxonomy_never_alarms_on_plain_usage_cap(tmp_path):
    """Deliberately no alarm on a plain exhausted_cap credential, even with
    a stale probe receipt: a usage cap self-clears at its own reset_at and
    already has dedicated alerting via hermes_usage_alert.py's usage_cap
    kind (86e2mb8p5) — re-paging it here would be a second, redundant
    alarm for an expected, self-resolving condition."""
    module = _load_module()
    stale_probe_at = (NOW - timedelta(hours=48)).timestamp()
    pool = {
        "openrouter": [{
            "id": "cred-5",
            "last_status": "exhausted",
            "last_failure_kind": "usage_cap",
            "last_probe_verdict": "still_unusable",
            "last_probe_verdict_at": stale_probe_at,
        }],
    }
    auth_path = _write_auth(tmp_path, pool)
    findings, _evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert findings == []


def test_credential_taxonomy_malformed_entries_do_not_crash(tmp_path):
    module = _load_module()
    pool = {"anthropic": ["not-a-dict", None], "openai-codex": [{"id": "cred-6"}]}
    auth_path = _write_auth(tmp_path, pool)
    findings, evidence = module._check_operational_contracts(
        [_contract(path=auth_path)], home=tmp_path, now=NOW
    )
    assert findings == []
    assert evidence[0]["providers"] == 2
