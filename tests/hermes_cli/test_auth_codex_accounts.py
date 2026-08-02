"""Tests for `hermes auth codex login|list|switch` (multi-account slots)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


def _read_auth_store(tmp_path) -> dict:
    return json.loads((tmp_path / "hermes" / "auth.json").read_text())


def _dual_account_store(preferred: str | None = None) -> dict:
    state = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "access-PRIMARY-secret",
            "refresh_token": "refresh-PRIMARY-secret",
        },
        "last_refresh": "2026-07-28T00:00:00Z",
        "accounts": {
            "work": {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "access-WORK-secret",
                    "refresh_token": "refresh-WORK-secret",
                },
                "last_refresh": "2026-07-29T00:00:00Z",
                "label": "work",
            }
        },
    }
    if preferred is not None:
        state["preferred_account"] = preferred
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {"openai-codex": state},
    }


def _login_args(**overrides):
    defaults = {"codex_action": "login", "account": None, "label": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_login(access: str, refresh: str):
    return lambda: {
        "tokens": {"access_token": access, "refresh_token": refresh},
        "last_refresh": "2026-08-02T00:00:00Z",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }


def test_codex_login_default_writes_primary_slot(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        _fake_login("access-P-secret", "refresh-P-secret"),
    )

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(_login_args())

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    assert state["tokens"]["access_token"] == "access-P-secret"
    entries = store["credential_pool"]["openai-codex"]
    assert [e["source"] for e in entries] == ["device_code"]

    out = capsys.readouterr().out
    assert 'saved to Codex account "primary"' in out
    # Never print full token values.
    assert "access-P-secret" not in out
    assert "refresh-P-secret" not in out


def test_codex_login_named_account_writes_own_slot(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        _fake_login("access-SECOND-secret", "refresh-SECOND-secret"),
    )

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(_login_args(account="Second"))

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    # Existing slots untouched.
    assert state["tokens"]["access_token"] == "access-PRIMARY-secret"
    assert state["accounts"]["work"]["tokens"]["access_token"] == "access-WORK-secret"
    # New slot written (label normalized to lowercase).
    assert state["accounts"]["second"]["tokens"]["access_token"] == "access-SECOND-secret"

    entries = store["credential_pool"]["openai-codex"]
    sources = {e["source"] for e in entries}
    assert sources == {"device_code", "device_code:work", "device_code:second"}

    out = capsys.readouterr().out
    assert 'saved to Codex account "second"' in out
    assert "access-SECOND-secret" not in out
    assert "refresh-SECOND-secret" not in out


def test_codex_login_relogin_updates_only_that_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        _fake_login("access-WORK-2-secret", "refresh-WORK-2-secret"),
    )

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(_login_args(account="work"))

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    assert state["accounts"]["work"]["tokens"]["access_token"] == "access-WORK-2-secret"
    assert state["tokens"]["access_token"] == "access-PRIMARY-secret"
    entries = store["credential_pool"]["openai-codex"]
    primary = next(e for e in entries if e["source"] == "device_code")
    named = next(e for e in entries if e["source"] == "device_code:work")
    assert primary["access_token"] == "access-PRIMARY-secret"
    assert named["access_token"] == "access-WORK-2-secret"


def test_codex_list_shows_accounts_preference_and_redacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(SimpleNamespace(codex_action="list"))

    out = capsys.readouterr().out
    assert "primary" in out
    assert "work" in out
    # Preferred marker on the work row.
    work_line = next(line for line in out.splitlines() if " work " in f" {line} ")
    assert work_line.lstrip().startswith("*")
    # Which entry the pool would select now.
    assert "pool would select" in out
    # Full token values never printed; the 6-char redaction prefix is fine.
    assert "access-PRIMARY-secret" not in out
    assert "access-WORK-secret" not in out
    assert "refresh-PRIMARY-secret" not in out
    assert "access" in out  # redacted prefix still shown for identification


def test_codex_list_selection_follows_preference(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(SimpleNamespace(codex_action="list"))
    out = capsys.readouterr().out
    select_line = next(line for line in out.splitlines() if "pool would select" in line)
    assert "work" in select_line


def test_codex_switch_sets_preference_and_reorders_pool(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(SimpleNamespace(codex_action="switch", account="work"))

    store = _read_auth_store(tmp_path)
    assert store["providers"]["openai-codex"]["preferred_account"] == "work"
    entries = sorted(
        store["credential_pool"]["openai-codex"], key=lambda e: e["priority"]
    )
    assert entries[0]["source"] == "device_code:work"

    out = capsys.readouterr().out
    assert 'Preferred Codex account set to "work"' in out
    assert "No restart needed" in out

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    assert pool.select().source == "device_code:work"


def test_codex_switch_back_to_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store(preferred="work"))

    from hermes_cli.auth_commands import auth_codex_command

    auth_codex_command(SimpleNamespace(codex_action="switch", account="primary"))

    store = _read_auth_store(tmp_path)
    assert store["providers"]["openai-codex"]["preferred_account"] == "primary"

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    assert pool.select().source == "device_code"


def test_codex_switch_unknown_account_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from hermes_cli.auth_commands import auth_codex_command

    with pytest.raises(SystemExit):
        auth_codex_command(SimpleNamespace(codex_action="switch", account="nope"))

    store = _read_auth_store(tmp_path)
    assert "preferred_account" not in store["providers"]["openai-codex"]


def test_codex_parser_wires_subcommands():
    import argparse

    from hermes_cli.subcommands.auth import build_auth_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_auth_parser(subparsers, cmd_auth=lambda args: None)

    args = parser.parse_args(["auth", "codex", "login", "--account", "work"])
    assert args.auth_action == "codex"
    assert args.codex_action == "login"
    assert args.account == "work"

    args = parser.parse_args(["auth", "codex", "list"])
    assert args.codex_action == "list"

    args = parser.parse_args(["auth", "codex", "switch", "primary"])
    assert args.codex_action == "switch"
    assert args.account == "primary"


def test_auth_remove_named_account_clears_slot_and_suppresses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _dual_account_store())

    from agent.credential_pool import load_pool

    load_pool("openai-codex")  # seed pool entries

    from hermes_cli.auth_commands import auth_remove_command

    auth_remove_command(
        SimpleNamespace(provider="openai-codex", target="work")
    )

    store = _read_auth_store(tmp_path)
    state = store["providers"]["openai-codex"]
    # Named slot cleared; primary singleton untouched.
    assert "work" not in state.get("accounts", {})
    assert state["tokens"]["access_token"] == "access-PRIMARY-secret"
    # The named source is suppressed so it stays gone.
    assert "device_code:work" in store.get("suppressed_sources", {}).get(
        "openai-codex", []
    )
    assert "device_code" not in store.get("suppressed_sources", {}).get(
        "openai-codex", []
    )

    # Re-load does not resurrect the removed slot; the primary remains.
    pool = load_pool("openai-codex")
    assert [e.source for e in pool.entries()] == ["device_code"]
