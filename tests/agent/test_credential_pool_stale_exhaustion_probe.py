"""Tests for CredentialPool.frozen_exhausted_entries / clear_stale_exhaustion.

These back the codex-quota-probe monitor (ClickUp 86e2kxk50): a provider's
``reset_at`` can be a stale ceiling — these two primitives let a periodic
probe find entries worth re-checking and clear exactly the one that proved
usable again, without touching any sibling entry.
"""
from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


def _read_auth_store(tmp_path) -> dict:
    return json.loads((tmp_path / "hermes" / "auth.json").read_text())


def _codex_entry(entry_id: str, *, last_status="exhausted", reset_at=None, **extra) -> dict:
    base = {
        "id": entry_id,
        "label": entry_id,
        "auth_type": "oauth",
        "priority": 0,
        "source": f"device_code:{entry_id}",
        "access_token": f"access-{entry_id}",
        "refresh_token": f"refresh-{entry_id}",
        "last_status": last_status,
        "last_status_at": time.time(),
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": reset_at,
    }
    base.update(extra)
    return base


def test_frozen_exhausted_entries_includes_only_unexpired_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "credential_pool": {
                "openai-codex": [
                    _codex_entry("stale-frozen", reset_at=now + 5 * 24 * 3600),
                    _codex_entry("already-expired", reset_at=now - 60),
                    _codex_entry("healthy", last_status="ok", reset_at=None),
                ]
            }
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    frozen = pool.frozen_exhausted_entries(now=now)

    assert [e.id for e in frozen] == ["stale-frozen"]


def test_clear_stale_exhaustion_clears_only_the_targeted_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "credential_pool": {
                "openai-codex": [
                    _codex_entry("primary", reset_at=now + 5 * 24 * 3600),
                    _codex_entry("backup", reset_at=now + 5 * 24 * 3600),
                ]
            }
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    cleared = pool.clear_stale_exhaustion("primary")

    assert cleared is True
    store = _read_auth_store(tmp_path)
    entries = {e["id"]: e for e in store["credential_pool"]["openai-codex"]}
    assert entries["primary"]["last_status"] is None
    assert entries["primary"]["last_error_reset_at"] is None
    assert entries["primary"]["last_error_code"] is None
    # The sibling entry is untouched — clearing one account never masks
    # (or resets) another account's independent exhaustion state.
    assert entries["backup"]["last_status"] == "exhausted"
    assert entries["backup"]["last_error_reset_at"] == pytest.approx(now + 5 * 24 * 3600)


def test_clear_stale_exhaustion_is_a_noop_for_unknown_or_non_exhausted_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "credential_pool": {
                "openai-codex": [
                    _codex_entry("healthy", last_status="ok", reset_at=None),
                ]
            }
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")

    assert pool.clear_stale_exhaustion("does-not-exist") is False
    assert pool.clear_stale_exhaustion("healthy") is False
    # No write happened for either no-op.
    store = _read_auth_store(tmp_path)
    assert store["credential_pool"]["openai-codex"][0]["last_status"] == "ok"


def test_dead_entries_are_excluded_from_frozen_exhausted(tmp_path, monkeypatch):
    """DEAD (permanent-auth) entries never recover on a TTL/probe and must
    stay out of the probe's candidate set — they need re-auth, not a retry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "credential_pool": {
                "openai-codex": [
                    _codex_entry("revoked", last_status="dead", reset_at=None),
                ]
            }
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    assert pool.frozen_exhausted_entries(now=now) == []
