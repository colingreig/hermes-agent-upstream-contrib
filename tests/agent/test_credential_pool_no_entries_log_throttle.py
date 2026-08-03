"""Regression: the credential-pool "no available entries" INFO line must be
throttled so an empty/exhausted pool cannot storm the shared rotating log.

Selection runs on a hot path (every model call plus auxiliary tasks). Before
the throttle, an empty/exhausted pool logged this line on *every* select(),
which on Windows storms concurrent-log-handler's cross-process lock
("Cannot acquire lock after 20 attempts"), stalls the asyncio event loop, and
fails the Desktop backend readiness handshake ("Timed out connecting to Hermes
backend after 15000ms"). See #58265 for the same fix class on another message.

Also covers 86e2mb8nv: a provider with ZERO configured entries (nobody ever
authenticated it) must log a distinct "no configured entries" line instead of
the exhausted-pool alert line — see test_credential_pool.py's
test_zero_entry_pool_does_not_trip_the_exhausted_pool_alert for the
alert-substring contract itself. The throttle mechanics (this file) apply
identically to both lines, so both get their own coverage below.
"""

from __future__ import annotations

import logging
import time

from agent.credential_pool import (
    NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS,
    STATUS_EXHAUSTED,
    CredentialPool,
    PooledCredential,
)

_NO_ENTRIES_MSG = "credential pool: no available entries (all exhausted or empty)"
_NO_CONFIGURED_ENTRIES_MSG = "credential pool: provider has no configured entries [provider=test]"


class _FakeClock:
    """Deterministic monotonic clock driven by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _no_entries_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == _NO_ENTRIES_MSG]


def _no_configured_entries_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == _NO_CONFIGURED_ENTRIES_MSG]


def _make_entry(entry_id: str) -> PooledCredential:
    return PooledCredential(
        provider="test",
        id=entry_id,
        label=entry_id,
        auth_type="api_key",
        source="manual",
        access_token=f"tok-{entry_id}",
        priority=0,
    )


def _make_exhausted_entry(entry_id: str) -> PooledCredential:
    """A genuinely-configured entry that's currently exhausted (not a
    zero-entry pool) — reset far enough in the future that ``select()``
    treats it as unavailable rather than auto-clearing it mid-test.
    """
    return PooledCredential(
        provider="test",
        id=entry_id,
        label=entry_id,
        auth_type="api_key",
        source="manual",
        access_token=f"tok-{entry_id}",
        priority=0,
        last_status=STATUS_EXHAUSTED,
        last_status_at=time.time(),
        last_error_code=402,
        last_error_reset_at=time.time() + 3600,
    )


def test_exhausted_pool_logs_once_within_throttle_window(monkeypatch, caplog):
    clock = _FakeClock()
    monkeypatch.setattr("agent.credential_pool.time.monotonic", clock)

    pool = CredentialPool("test", [_make_exhausted_entry("a")])

    with caplog.at_level(logging.INFO, logger="agent.credential_pool"):
        for _ in range(50):
            clock.now += 0.1  # tighter than the throttle window
            assert pool.select() is None

    # 50 selections, well inside one window -> exactly one log line.
    assert len(_no_entries_records(caplog)) == 1
    assert not _no_configured_entries_records(caplog)


def test_logs_again_after_throttle_window_elapses(monkeypatch, caplog):
    clock = _FakeClock()
    monkeypatch.setattr("agent.credential_pool.time.monotonic", clock)

    pool = CredentialPool("test", [_make_exhausted_entry("a")])

    with caplog.at_level(logging.INFO, logger="agent.credential_pool"):
        assert pool.select() is None  # log #1
        clock.now += NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS + 1
        assert pool.select() is None  # window elapsed -> log #2

    assert len(_no_entries_records(caplog)) == 2


def test_successful_selection_rearms_throttle(monkeypatch, caplog):
    """A recover -> re-exhaust transition must log immediately, even inside the
    window opened by the previous empty stretch (observability of the flip)."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.credential_pool.time.monotonic", clock)

    pool = CredentialPool("test", [_make_exhausted_entry("a")])

    with caplog.at_level(logging.INFO, logger="agent.credential_pool"):
        assert pool.select() is None  # log #1, throttle armed
        clock.now += 1
        assert pool.select() is None  # within window -> no log

        # Pool recovers: a successful selection re-arms the throttle.
        pool._entries = [_make_entry("b")]
        clock.now += 1
        assert pool.select() is not None

        # Pool exhausts again shortly after (< throttle window since log #1).
        pool._entries = [_make_exhausted_entry("c")]
        clock.now += 1
        assert pool.select() is None  # re-armed -> log #2 immediately

    assert len(_no_entries_records(caplog)) == 2


# ── Zero-entry ("not configured") variant of the same throttle ──────────

def test_zero_entry_pool_logs_not_configured_once_within_throttle_window(monkeypatch, caplog):
    clock = _FakeClock()
    monkeypatch.setattr("agent.credential_pool.time.monotonic", clock)

    pool = CredentialPool("test", [])

    with caplog.at_level(logging.INFO, logger="agent.credential_pool"):
        for _ in range(50):
            clock.now += 0.1  # tighter than the throttle window
            assert pool.select() is None

    assert len(_no_configured_entries_records(caplog)) == 1
    assert not _no_entries_records(caplog)


def test_zero_entry_pool_logs_again_after_throttle_window_elapses(monkeypatch, caplog):
    clock = _FakeClock()
    monkeypatch.setattr("agent.credential_pool.time.monotonic", clock)

    pool = CredentialPool("test", [])

    with caplog.at_level(logging.INFO, logger="agent.credential_pool"):
        assert pool.select() is None  # log #1
        clock.now += NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS + 1
        assert pool.select() is None  # window elapsed -> log #2

    assert len(_no_configured_entries_records(caplog)) == 2
