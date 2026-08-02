"""Tests for multi-account OpenAI Codex OAuth slots.

The primary account lives at ``providers.openai-codex.tokens`` (legacy,
zero-migration back-compat); named accounts live under
``providers.openai-codex.accounts.<label>``.  Each populated slot seeds one
pool entry (``device_code`` / ``device_code:<label>``) that syncs and
refreshes from its own slot only.
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


def _codex_state(
    access: str,
    refresh: str,
    *,
    accounts: dict | None = None,
    preferred: str | None = None,
) -> dict:
    state = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": "id-" + access,
        },
        "last_refresh": "2026-07-28T00:00:00Z",
    }
    if accounts is not None:
        state["accounts"] = accounts
    if preferred is not None:
        state["preferred_account"] = preferred
    return state


def _account_slot(access: str, refresh: str, label: str | None = None) -> dict:
    slot = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": access, "refresh_token": refresh},
        "last_refresh": "2026-07-29T00:00:00Z",
    }
    if label:
        slot["label"] = label
    return slot


def _auth_store(state: dict) -> dict:
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {"openai-codex": state},
    }


def _dual_account_store(preferred: str | None = None) -> dict:
    return _auth_store(
        _codex_state(
            "access-PRIMARY",
            "refresh-PRIMARY",
            accounts={
                "work": _account_slot("access-WORK", "refresh-WORK", label="work"),
            },
            preferred=preferred,
        )
    )


# ── Seeding ────────────────────────────────────────────────────────────────


def test_legacy_single_slot_seeds_one_entry(tmp_path, monkeypatch):
    """Back-compat: a store without ``accounts`` behaves exactly as before."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _auth_store(_codex_state("access-A", "refresh-A")))

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entries = pool.entries()
    assert len(entries) == 1
    assert entries[0].source == "device_code"
    assert entries[0].access_token == "access-A"
    assert entries[0].priority == 0


def test_named_slots_seed_one_entry_each(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        _auth_store(
            _codex_state(
                "access-PRIMARY",
                "refresh-PRIMARY",
                accounts={
                    "work": _account_slot("access-WORK", "refresh-WORK", label="work"),
                    "backup": _account_slot("access-BAK", "refresh-BAK", label="backup"),
                },
            )
        ),
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entries = sorted(pool.entries(), key=lambda e: e.priority)
    assert [e.source for e in entries] == [
        "device_code",
        "device_code:backup",
        "device_code:work",
    ]
    assert entries[0].access_token == "access-PRIMARY"
    assert entries[1].access_token == "access-BAK"
    assert entries[2].access_token == "access-WORK"
    assert entries[1].label == "backup"
    assert entries[2].label == "work"
    # Stable distinct ids
    assert len({e.id for e in entries}) == 3
    # Deterministic priorities: primary first, named slots in sorted order
    assert [e.priority for e in entries] == [0, 1, 2]


def test_named_slot_tokens_persist_to_disk(tmp_path, monkeypatch):
    """Named slots are Hermes-owned device-code state — the disk boundary
    must persist their token material (not sanitize it away as borrowed)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool

    load_pool("openai-codex")
    persisted = _read_auth_store(tmp_path)["credential_pool"]["openai-codex"]
    named = next(e for e in persisted if e["source"] == "device_code:work")
    assert named["access_token"] == "access-WORK"
    assert named["refresh_token"] == "refresh-WORK"


def test_empty_named_slot_not_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        _auth_store(
            _codex_state(
                "access-PRIMARY",
                "refresh-PRIMARY",
                accounts={"empty": {"tokens": {}}},
            )
        ),
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    assert [e.source for e in pool.entries()] == ["device_code"]


# ── Per-account sync isolation ─────────────────────────────────────────────


def test_relogin_of_named_account_does_not_touch_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool
    from hermes_cli.auth import _save_codex_tokens

    pool = load_pool("openai-codex")
    assert len(pool.entries()) == 2

    # Simulate `hermes auth codex login --account work` with fresh tokens.
    _save_codex_tokens(
        {"access_token": "access-WORK-2", "refresh_token": "refresh-WORK-2"},
        "2026-08-01T00:00:00Z",
        account="work",
    )

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    assert state["tokens"]["access_token"] == "access-PRIMARY"
    assert state["accounts"]["work"]["tokens"]["access_token"] == "access-WORK-2"

    pool_entries = store["credential_pool"]["openai-codex"]
    primary = next(e for e in pool_entries if e["source"] == "device_code")
    named = next(e for e in pool_entries if e["source"] == "device_code:work")
    assert primary["access_token"] == "access-PRIMARY"
    assert named["access_token"] == "access-WORK-2"
    assert named["refresh_token"] == "refresh-WORK-2"


def test_relogin_of_primary_does_not_touch_named_account(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool
    from hermes_cli.auth import _save_codex_tokens

    load_pool("openai-codex")
    _save_codex_tokens(
        {"access_token": "access-PRIMARY-2", "refresh_token": "refresh-PRIMARY-2"},
        "2026-08-01T00:00:00Z",
    )

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    assert state["tokens"]["access_token"] == "access-PRIMARY-2"
    assert state["accounts"]["work"]["tokens"]["access_token"] == "access-WORK"

    pool_entries = store["credential_pool"]["openai-codex"]
    primary = next(e for e in pool_entries if e["source"] == "device_code")
    named = next(e for e in pool_entries if e["source"] == "device_code:work")
    assert primary["access_token"] == "access-PRIMARY-2"
    assert named["access_token"] == "access-WORK"


def test_sync_named_entry_rehydrates_from_its_own_slot(tmp_path, monkeypatch):
    """An exhausted named entry recovers when ITS slot re-auths — and only
    from its own slot, never from the primary singleton."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from dataclasses import replace as dc_replace

    from agent.credential_pool import STATUS_EXHAUSTED, load_pool

    pool = load_pool("openai-codex")
    named = next(e for e in pool.entries() if e.source == "device_code:work")

    now = time.time()
    exhausted = dc_replace(
        named,
        last_status=STATUS_EXHAUSTED,
        last_status_at=now,
        last_error_code=429,
        last_error_reset_at=now + 3600,
    )
    pool._replace_entry(named, exhausted)
    pool._persist()

    # Nothing changed on disk: still frozen behind the reset window.
    available = pool._available_entries(clear_expired=True, refresh=False)
    assert [e.source for e in available] == ["device_code"]

    # Out-of-band re-login of the WORK slot only.
    store = _dual_account_store()
    store["providers"]["openai-codex"]["accounts"]["work"] = _account_slot(
        "access-WORK-FRESH", "refresh-WORK-FRESH", label="work"
    )
    _write_auth_store(tmp_path, store)

    available = pool._available_entries(clear_expired=True, refresh=False)
    by_source = {e.source: e for e in available}
    assert by_source["device_code:work"].access_token == "access-WORK-FRESH"
    assert by_source["device_code:work"].last_status is None
    # Primary entry untouched.
    assert by_source["device_code"].access_token == "access-PRIMARY"


def test_primary_relogin_does_not_unfreeze_named_entry(tmp_path, monkeypatch):
    """Isolation in the other direction: a primary re-login must not clear a
    named entry's exhaustion state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from dataclasses import replace as dc_replace

    from agent.credential_pool import STATUS_EXHAUSTED, load_pool

    pool = load_pool("openai-codex")
    named = next(e for e in pool.entries() if e.source == "device_code:work")
    now = time.time()
    pool._replace_entry(
        named,
        dc_replace(
            named,
            last_status=STATUS_EXHAUSTED,
            last_status_at=now,
            last_error_code=429,
            last_error_reset_at=now + 3600,
        ),
    )
    pool._persist()

    # Primary slot re-auths; the work slot is unchanged.  Mutate the on-disk
    # store in place so the persisted pool state (work = exhausted) survives.
    store = _read_auth_store(tmp_path)
    store["providers"]["openai-codex"]["tokens"]["access_token"] = "access-PRIMARY-NEW"
    store["providers"]["openai-codex"]["tokens"]["refresh_token"] = "refresh-PRIMARY-NEW"
    _write_auth_store(tmp_path, store)

    available = pool._available_entries(clear_expired=True, refresh=False)
    assert [e.source for e in available] == ["device_code"]

    # A fresh pool load re-seeds the primary from its slot while the named
    # entry stays frozen behind its own reset window.
    reloaded = load_pool("openai-codex")
    by_source = {e.source: e for e in reloaded.entries()}
    assert by_source["device_code"].access_token == "access-PRIMARY-NEW"
    assert by_source["device_code:work"].last_status == STATUS_EXHAUSTED
    assert [e.source for e in reloaded._available_entries(clear_expired=True, refresh=False)] == ["device_code"]


# ── Preferred account + failover ───────────────────────────────────────────


def test_preferred_account_wins_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.source == "device_code:work"
    assert selected.access_token == "access-WORK"
    # Preference is persisted as priority 0 so every consumer agrees.
    entries = sorted(pool.entries(), key=lambda e: e.priority)
    assert entries[0].source == "device_code:work"


def test_no_preference_keeps_primary_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.source == "device_code"


def test_switch_back_to_primary_restores_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from agent.credential_pool import load_pool
    from hermes_cli.auth import set_codex_preferred_account

    pool = load_pool("openai-codex")
    assert pool.select().source == "device_code:work"

    set_codex_preferred_account("primary")
    pool = load_pool("openai-codex")
    assert pool.select().source == "device_code"


def test_preferred_exhausted_fails_over_to_primary(tmp_path, monkeypatch):
    """429 on the preferred account rotates to the non-preferred slot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected.source == "device_code:work"

    rotated = pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"reason": "usage_limit_reached"},
    )
    assert rotated is not None
    assert rotated.source == "device_code"
    assert rotated.access_token == "access-PRIMARY"

    # The preferred entry is persisted as exhausted; the primary serves.
    store = _read_auth_store(tmp_path)
    named = next(
        e
        for e in store["credential_pool"]["openai-codex"]
        if e["source"] == "device_code:work"
    )
    assert named["last_status"] == "exhausted"
    assert named["last_error_code"] == 429


# ── Runtime resolution (proxy path) ────────────────────────────────────────


def test_resolve_serves_preferred_named_account(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from hermes_cli.auth import resolve_codex_runtime_credentials

    creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
    assert creds["api_key"] == "access-WORK"
    assert creds["account"] == "work"


def test_resolve_defaults_to_primary_without_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from hermes_cli.auth import resolve_codex_runtime_credentials

    creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
    assert creds["api_key"] == "access-PRIMARY"


def test_resolve_fails_over_when_preferred_rate_limited(tmp_path, monkeypatch):
    """A pool-recorded 429 cooldown on the preferred slot makes runtime
    resolution (the proxy path) serve the other account automatically."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    store = _dual_account_store(preferred="work")
    store["credential_pool"] = {
        "openai-codex": [
            {
                "id": "work01",
                "label": "work",
                "auth_type": "oauth",
                "priority": 0,
                "source": "device_code:work",
                "access_token": "access-WORK",
                "refresh_token": "refresh-WORK",
                "last_status": "exhausted",
                "last_status_at": time.time(),
                "last_error_code": 429,
                "last_error_reason": "usage_limit_reached",
                "last_error_reset_at": time.time() + 3600,
            },
        ]
    }
    _write_auth_store(tmp_path, store)

    from hermes_cli.auth import resolve_codex_runtime_credentials

    creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
    assert creds["api_key"] == "access-PRIMARY"


def test_resolve_explicit_account_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from hermes_cli.auth import resolve_codex_runtime_credentials

    creds = resolve_codex_runtime_credentials(
        refresh_if_expiring=False, account="work"
    )
    assert creds["api_key"] == "access-WORK"
    assert creds["account"] == "work"


def test_resolve_missing_named_account_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _auth_store(_codex_state("access-A", "refresh-A")))

    from hermes_cli.auth import AuthError, resolve_codex_runtime_credentials

    with pytest.raises(AuthError):
        resolve_codex_runtime_credentials(
            refresh_if_expiring=False, account="nope"
        )


# ── Named-slot refresh write-back ──────────────────────────────────────────


def test_named_entry_refresh_writes_back_to_own_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    named = next(e for e in pool.entries() if e.source == "device_code:work")

    import hermes_cli.auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda access, refresh, **kw: {
            "access_token": "access-WORK-REFRESHED",
            "refresh_token": "refresh-WORK-REFRESHED",
            "last_refresh": "2026-08-02T00:00:00Z",
        },
    )

    refreshed = pool._refresh_entry(named, force=True)
    assert refreshed is not None
    assert refreshed.access_token == "access-WORK-REFRESHED"

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    # Named slot updated; primary singleton untouched.
    assert state["accounts"]["work"]["tokens"]["access_token"] == "access-WORK-REFRESHED"
    assert state["accounts"]["work"]["tokens"]["refresh_token"] == "refresh-WORK-REFRESHED"
    assert state["tokens"]["access_token"] == "access-PRIMARY"
    assert state["tokens"]["refresh_token"] == "refresh-PRIMARY"


def test_account_slot_helpers_roundtrip():
    from hermes_cli.auth import (
        CODEX_PRIMARY_ACCOUNT,
        codex_account_from_source,
        codex_account_source,
        normalize_codex_account_label,
    )

    assert codex_account_source("primary") == "device_code"
    assert codex_account_source("") == "device_code"
    assert codex_account_source("work") == "device_code:work"
    assert codex_account_from_source("device_code") == CODEX_PRIMARY_ACCOUNT
    assert codex_account_from_source("device_code:work") == "work"
    assert codex_account_from_source("manual:device_code") is None
    assert codex_account_from_source("env:OPENAI_API_KEY") is None
    assert normalize_codex_account_label(None) == CODEX_PRIMARY_ACCOUNT
    assert normalize_codex_account_label(" Work ") == "work"

    from hermes_cli.auth import AuthError

    with pytest.raises(AuthError):
        normalize_codex_account_label("bad label with spaces")
    with pytest.raises(AuthError):
        normalize_codex_account_label("colon:label")
