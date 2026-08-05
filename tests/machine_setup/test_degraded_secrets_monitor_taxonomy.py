"""Per-class page/no-page contract for degraded_secrets_monitor.py's

credential-pool failure taxonomy (ClickUp 86e2mb8p5, PR 3/4 of the
adversarial provider-failure taxonomy epic, building on 86e2mb8nv PR 1's
classifier + credential-pool taxonomy and 86e2mb8p0 PR 2's provider probe).

Lives under the main ``tests/`` CI lane (not
``machine-setup/mini-scripts/tests/``, which is NOT part of any CI workflow)
— see ``tests/machine_setup/test_provider_probe.py`` for the same per-test
unique-module-name loading pattern this file reuses.

This is the epic's highest-misclassification-risk boundary: treating an
``unconfigured_entry``/``unconfigured_provider``/idle-``exhausted_session``
as a real ``missing_credential`` (or vice versa) reintroduces exactly the
false pages the epic exists to kill. Every test below asserts BOTH the
taxonomy class AND whether it pages (lands in ``hits``) or not (lands in
``credential_pool_notices``) — never just one or the other.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
MODULE_PATH = SCRIPTS / "degraded_secrets_monitor.py"
_COUNTER = 0

NOW = datetime(2026, 8, 3, 22, 25, tzinfo=timezone.utc)


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"degraded_secrets_monitor_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _classify(module, pool):
    return module.classify_credential_pool({"credential_pool": pool}, NOW)


# ── Per-class page/no-page matrix ───────────────────────────────────────────

def test_class_unconfigured_provider_never_pages():
    """A provider key with zero entries — nothing routes to it."""
    module = _load_module()
    result = _classify(module, {"copilot": []})

    assert result["triggered"] is False
    assert result["hits"] == []
    assert result["credential_pool_notices"] == [
        {"provider": "copilot", "id": "pool", "status": "unconfigured_provider", "retry_at": None}
    ]


def test_class_unconfigured_entry_never_pages():
    """A bare placeholder slot (nothing beyond ``id``) nothing has configured."""
    module = _load_module()
    result = _classify(module, {"openrouter": [{"id": "scaffold"}]})

    assert result["triggered"] is False
    assert result["hits"] == []
    assert result["credential_pool_notices"] == [
        {"provider": "openrouter", "id": "scaffold", "status": "unconfigured_entry", "retry_at": None}
    ]


def test_class_missing_credential_always_pages():
    """A terminal (dead/invalid/error) pool status is a genuine defect."""
    module = _load_module()
    result = _classify(module, {"xai": [{"id": "x", "access_token": "tok", "last_status": "invalid"}]})

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "xai", "id": "x", "status": "missing_credential", "retry_at": None}
    ]
    assert result["credential_pool_notices"] == []


def test_class_missing_credential_pages_even_with_extra_but_wrong_field():
    """A mistyped field name (api_key instead of access_token) is still

    'someone tried to configure this' — must page, never silently downgrade
    to unconfigured_entry. This is the misclassification direction that
    would swallow a real misconfiguration.
    """
    module = _load_module()
    result = _classify(module, {"openrouter": [{"id": "wrong-field", "api_key": "secret"}]})

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "openrouter", "id": "wrong-field", "status": "missing_credential", "retry_at": None}
    ]


def test_class_exhausted_session_real_failure_still_pages():
    """A REAL recorded short-horizon exhaustion (an actual 429/timeout the

    runtime observed) still pages — only the idle-never-failed variant
    (tested below) is suppressed. Distinguishing these two is the crux of
    this PR's misclassification risk.
    """
    module = _load_module()
    reset_at = NOW.timestamp() + 60  # 60s out — session-scoped, not a cap
    result = _classify(module, {
        "openai-codex": [{
            "id": "primary",
            "access_token": "tok",
            "last_status": "exhausted",
            "last_error_reset_at": reset_at,
        }]
    })

    assert result["triggered"] is True
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["provider"] == "openai-codex"
    assert hit["id"] == "primary"
    assert hit["status"] == "exhausted_session"
    assert hit["retry_at"] is not None  # a real observed failure carries its reset time


def test_class_exhausted_cap_always_pages():
    """A long-horizon (weekly/monthly) usage cap always pages — it needs a

    human or a long wait, never silently suppressed.
    """
    module = _load_module()
    result = _classify(module, {
        "codex": [{
            "id": "c",
            "access_token": "tok",
            "last_status": "exhausted",
            "last_error_reset_at": "2099-01-01T00:00:00Z",
        }]
    })

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "codex", "id": "c", "status": "exhausted_cap", "retry_at": "2099-01-01T00:00:00+00:00"}
    ]


def test_class_exhausted_cap_via_persisted_failure_kind():
    """When agent.error_classifier's taxonomy (86e2mb8nv PR 1) already

    persisted last_failure_kind=usage_cap, that's authoritative over the
    horizon heuristic — even for a SHORT remaining window (e.g. checked
    right before a weekly reset).
    """
    module = _load_module()
    result = _classify(module, {
        "codex": [{
            "id": "c",
            "access_token": "tok",
            "last_status": "exhausted",
            "last_error_reset_at": (NOW.timestamp() + 30),  # 30s out
            "last_failure_kind": "usage_cap",
        }]
    })

    assert result["hits"][0]["status"] == "exhausted_cap"


def test_class_auth_permanent_exhaustion_reports_as_missing_credential():
    """An auth_permanent quarantine needs repair, not a wait — same

    operational bucket as a terminal/dead credential, not a self-healing
    exhausted_session/cap.
    """
    module = _load_module()
    result = _classify(module, {
        "github": [{
            "id": "g",
            "access_token": "tok",
            "last_status": "exhausted",
            "last_error_reset_at": "2099-01-01T00:00:00Z",
            "last_failure_kind": "auth_permanent",
        }]
    })

    assert result["hits"][0]["status"] == "missing_credential"


def test_malformed_slot_still_pages_and_is_not_a_taxonomy_class():
    """A structurally-broken pool entry (not a dict) is data corruption,

    orthogonal to the 5-class taxonomy — always visible, never silently
    downgraded to unconfigured_entry.
    """
    module = _load_module()
    result = _classify(module, {"codex": ["not-an-entry"]})

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "codex", "id": "index:0", "status": "malformed", "retry_at": None}
    ]


# ── Live-evidence regressions (ClickUp 86e2mdfhx) ───────────────────────────

def test_regression_copilot_and_gemini_stale_empty_keys_do_not_page():
    """Measured live 2026-08-03: the credential hygiene sweep removed pool

    *entries* but left the empty provider *keys* behind for copilot/gemini.
    Nothing routes to either — must not page.
    """
    module = _load_module()
    result = _classify(module, {
        "anthropic": [{"id": "a", "access_token": "tok", "last_status": "ok"}],
        "zai": [{"id": "z", "access_token": "tok", "last_status": "ok"}],
        "openai-codex": [
            {"id": "c1", "access_token": "tok1", "last_status": "ok"},
            {"id": "c2", "access_token": "tok2", "last_status": "ok"},
        ],
        "copilot": [],
        "gemini": [],
    })

    assert result["triggered"] is False
    assert result["hits"] == []
    notice_providers = {n["provider"] for n in result["credential_pool_notices"]}
    assert notice_providers == {"copilot", "gemini"}
    assert all(n["status"] == "unconfigured_provider" for n in result["credential_pool_notices"])


def test_regression_nous_expired_but_refreshable_idle_token_does_not_page():
    """Measured live 2026-08-03: nous's 1h OAuth token expired 45 min

    before this check, last_status="ok" (never actually failed),
    request_count=0 (idle, not exhausted), has refresh_token=True. The
    runtime refreshes it transparently on next use — must NOT page as
    missing_credential (the live defect: it re-paged every idle hour).
    """
    module = _load_module()
    result = _classify(module, {
        "nous": [{
            "id": "primary",
            "last_status": "ok",
            "request_count": 0,
            "refresh_token": "a-real-refresh-token",
            "obtained_at": "2026-08-03T20:40:47Z",
            "expires_at": "2026-08-03T21:40:47Z",  # expired 45 min before NOW (22:25Z)
        }]
    })

    assert result["triggered"] is False
    assert result["hits"] == []
    assert result["credential_pool_notices"] == [
        {"provider": "nous", "id": "primary", "status": "exhausted_session", "retry_at": None}
    ]


def test_regression_nous_genuinely_revoked_token_still_pages():
    """A nous token with NO refresh_token (genuinely revoked/unrefreshable,

    not just idle) must still page as missing_credential — the acceptance
    criterion from 86e2mdfhx: "a genuinely revoked token (refresh attempt
    fails) still pages."
    """
    module = _load_module()
    result = _classify(module, {
        "nous": [{
            "id": "primary",
            "last_status": "ok",
            "request_count": 0,
            "expires_at": "2026-08-03T21:40:47Z",
        }]
    })

    assert result["triggered"] is True
    assert result["hits"] == [
        {"provider": "nous", "id": "primary", "status": "missing_credential", "retry_at": None}
    ]


def test_regression_full_live_fleet_returns_zero_hits_when_healthy():
    """Acceptance criterion from 86e2mdfhx: after the fix, a live run

    against the real fleet shape (anthropic/zai/openai-codex healthy,
    copilot/gemini empty, nous idle-refreshable) returns ZERO hits.
    """
    module = _load_module()
    result = _classify(module, {
        "anthropic": [{"id": "a", "access_token": "tok", "last_status": "ok"}],
        "zai": [{"id": "z", "access_token": "tok", "last_status": "ok"}],
        "openai-codex": [
            {"id": "c1", "access_token": "tok1", "last_status": "ok"},
            {"id": "c2", "access_token": "tok2", "last_status": "ok"},
        ],
        "copilot": [],
        "gemini": [],
        "nous": [{
            "id": "primary",
            "last_status": "ok",
            "request_count": 0,
            "refresh_token": "a-real-refresh-token",
            "expires_at": "2026-08-03T21:40:47Z",
        }],
    })

    assert result["triggered"] is False
    assert result["hits"] == []


# ── Sibling suppression (unchanged from PR 1, still holds under the new taxonomy) ──

def test_sibling_suppression_downgrades_exhausted_cap_to_diagnostic():
    """A capped entry with a healthy sibling never pages — reduced-redundancy

    diagnostic only. Paging requires being the LAST usable entry.
    """
    module = _load_module()
    result = _classify(module, {
        "openai-codex": [
            {
                "id": "primary",
                "access_token": "tok",
                "last_status": "exhausted",
                "last_error_reset_at": "2099-01-01T00:00:00Z",
            },
            {"id": "backup", "access_token": "backup-tok", "last_status": "ok"},
        ]
    })

    assert result["triggered"] is False
    assert result["hits"] == []
    assert result["credential_pool_diagnostics"][0]["unavailable_slots"] == [
        {"id": "primary", "status": "exhausted_cap", "retry_at": "2099-01-01T00:00:00+00:00"}
    ]


def test_sibling_suppression_lifts_when_last_usable_entry_caps():
    """The same capped entry pages once it's the LAST usable one — sibling

    also went dead.
    """
    module = _load_module()
    result = _classify(module, {
        "openai-codex": [
            {
                "id": "primary",
                "access_token": "tok",
                "last_status": "exhausted",
                "last_error_reset_at": "2099-01-01T00:00:00Z",
            },
            {"id": "backup", "access_token": "backup-tok", "last_status": "dead"},
        ]
    })

    assert result["triggered"] is True
    statuses = {(h["provider"], h["id"], h["status"]) for h in result["hits"]}
    assert statuses == {
        ("openai-codex", "primary", "exhausted_cap"),
        ("openai-codex", "backup", "missing_credential"),
    }
