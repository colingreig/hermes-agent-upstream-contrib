"""Liveness-based orphan detection and reap behavior for mcp_serve_reaper.py.

Task 86e2hap4g: this must reap `hermes mcp serve` processes ONLY when
genuinely orphaned (dead parent / dead owning sshd session) and old enough —
never a live SSH-attached session, and never based on release-path identity.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "mcp_serve_reaper.py"

spec = importlib.util.spec_from_file_location("mcp_serve_reaper_test", SCRIPT)
reaper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reaper
spec.loader.exec_module(reaper)


HERMES_MCP_SERVE_CMD = "/path/venv/bin/python /Users/colingreig/.local/bin/hermes mcp serve"


def test_parse_bsd_etime_formats():
    assert reaper._parse_bsd_etime("45") == 45
    assert reaper._parse_bsd_etime("46:40") == 46 * 60 + 40
    assert reaper._parse_bsd_etime("01:49:55") == 1 * 3600 + 49 * 60 + 55
    assert reaper._parse_bsd_etime("04-08:30:00") == ((4 * 24 + 8) * 60 + 30) * 60


def test_finds_mcp_serve_candidates_only():
    snapshot = {
        100: {"ppid": 1, "etime_seconds": 100, "command": HERMES_MCP_SERVE_CMD},
        101: {"ppid": 1, "etime_seconds": 100, "command": "/bin/zsh -l"},
        102: {"ppid": 1, "etime_seconds": 100, "command": "hermes mcp serve --extra flag"},
    }
    assert sorted(reaper._find_mcp_serve_candidates(snapshot)) == [100, 102]


def test_orphaned_when_direct_parent_is_pid_1():
    snapshot = {200: {"ppid": 1, "etime_seconds": 5000, "command": HERMES_MCP_SERVE_CMD}}
    assert reaper._is_orphaned(200, snapshot) is True


def test_not_orphaned_when_direct_parent_is_live_sshd_session():
    snapshot = {
        300: {"ppid": 301, "etime_seconds": 5000, "command": HERMES_MCP_SERVE_CMD},
        301: {"ppid": 1, "etime_seconds": 6000, "command": "sshd-session: colingreig@notty"},
    }
    assert reaper._is_orphaned(300, snapshot) is False


def test_orphaned_when_intermediate_wrapper_chain_reaches_pid_1_without_sshd():
    """An intermediate wrapper between mcp-serve and its (now-gone) sshd session
    still counts as orphaned if walking the chain never finds a live sshd-session."""
    snapshot = {
        400: {"ppid": 401, "etime_seconds": 5000, "command": HERMES_MCP_SERVE_CMD},
        401: {"ppid": 1, "etime_seconds": 5000, "command": "/bin/zsh (orphaned wrapper)"},
    }
    assert reaper._is_orphaned(400, snapshot) is True


def test_not_orphaned_when_ancestor_lookup_is_missing():
    """Ambiguous case (a ppid with no snapshot entry, e.g. a race) must be
    protected, never assumed orphaned."""
    snapshot = {500: {"ppid": 501, "etime_seconds": 5000, "command": HERMES_MCP_SERVE_CMD}}
    assert reaper._is_orphaned(500, snapshot) is False


def test_deep_or_cyclic_chain_is_protected_not_assumed_orphaned():
    # A cycle that never reaches PID 1 or an sshd-session must not be treated
    # as orphaned — it fails closed via the MAX_ANCESTOR_HOPS bound.
    snapshot = {
        600: {"ppid": 601, "etime_seconds": 5000, "command": HERMES_MCP_SERVE_CMD},
        601: {"ppid": 600, "etime_seconds": 5000, "command": "/bin/some-wrapper"},
    }
    assert reaper._is_orphaned(600, snapshot) is False


def test_young_orphan_is_skipped_by_age_floor(monkeypatch):
    """A process orphaned moments ago (reconnect race) must not be reaped."""
    snapshot = {
        700: {"ppid": 1, "etime_seconds": 60, "command": HERMES_MCP_SERVE_CMD},
    }
    monkeypatch.setattr(reaper, "_ps_snapshot", lambda: snapshot)
    reaped = []
    monkeypatch.setattr(reaper, "_reap", lambda pid, cmd, grace, dry: reaped.append(pid) or True)

    reaper.main(["--min-age-minutes", "45"])
    assert reaped == []


def test_old_orphan_is_reaped(monkeypatch):
    snapshot = {
        800: {"ppid": 1, "etime_seconds": 3600, "command": HERMES_MCP_SERVE_CMD},
    }
    monkeypatch.setattr(reaper, "_ps_snapshot", lambda: snapshot)
    reaped = []
    monkeypatch.setattr(reaper, "_reap", lambda pid, cmd, grace, dry: reaped.append(pid) or True)

    reaper.main(["--min-age-minutes", "45"])
    assert reaped == [800]


def test_old_live_session_is_never_reaped(monkeypatch):
    """The core safety invariant: an old process whose owning session is still
    alive is never touched, regardless of age."""
    snapshot = {
        900: {"ppid": 901, "etime_seconds": 999999, "command": HERMES_MCP_SERVE_CMD},
        901: {"ppid": 1, "etime_seconds": 999999, "command": "sshd-session: colingreig@notty"},
    }
    monkeypatch.setattr(reaper, "_ps_snapshot", lambda: snapshot)
    reaped = []
    monkeypatch.setattr(reaper, "_reap", lambda pid, cmd, grace, dry: reaped.append(pid) or True)

    reaper.main(["--min-age-minutes", "45"])
    assert reaped == []


def test_reap_sends_sigterm_then_confirms_exit(monkeypatch):
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError()  # process exited after SIGTERM

    monkeypatch.setattr(reaper.os, "kill", fake_kill)
    monkeypatch.setattr(reaper.time, "sleep", lambda _s: None)

    result = reaper._reap(1234, HERMES_MCP_SERVE_CMD, grace_seconds=2, dry_run=False)
    assert result is True
    assert calls[0] == (1234, signal.SIGTERM)
    # No SIGKILL sent — the process exited cleanly during the grace check.
    assert all(sig != signal.SIGKILL for _, sig in calls)


def test_reap_escalates_to_sigkill_if_still_alive_after_grace(monkeypatch):
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        # sig == 0 liveness probes always succeed (still alive) during this test.

    monkeypatch.setattr(reaper.os, "kill", fake_kill)
    monkeypatch.setattr(reaper.time, "sleep", lambda _s: None)

    result = reaper._reap(5678, HERMES_MCP_SERVE_CMD, grace_seconds=1, dry_run=False)
    assert result is True
    assert (5678, signal.SIGTERM) in calls
    assert (5678, signal.SIGKILL) in calls


def test_dry_run_never_calls_kill(monkeypatch):
    def fail_kill(*_a, **_k):
        raise AssertionError("dry-run must never call os.kill")

    monkeypatch.setattr(reaper.os, "kill", fail_kill)
    assert reaper._reap(9999, HERMES_MCP_SERVE_CMD, grace_seconds=1, dry_run=True) is True
