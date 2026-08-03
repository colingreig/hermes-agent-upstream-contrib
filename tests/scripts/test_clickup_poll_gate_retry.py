"""Regression guard for 86e2kxk4z — ClickUp client intermittency in executor runs.

scripts/clickup_poll_gate.py is EXECUTOR_ID's (62714b869845) admission gate:
every 15-min tick it calls ClickUp to scan the queue and to claim/stamp/unpark
tasks. Before this fix every call site was a single bare
``urllib.request.urlopen`` — a transient 429/5xx or network blip aborted the
whole tick with no retry at all. These tests pin ``_urlopen_with_retry``'s
behavior in isolation from real network/timing.
"""

from __future__ import annotations

import urllib.error

import pytest

import scripts.clickup_poll_gate as gate_mod


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this file exercises retry/backoff — never actually sleep."""
    monkeypatch.setattr(gate_mod.time, "sleep", lambda _s: None)


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.clickup.com/api/v2/x", code, "err", headers or {}, None
    )


def test_succeeds_first_try_no_sleep(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    resp = gate_mod._urlopen_with_retry(object(), timeout=30)

    assert resp.status == 200
    assert calls["n"] == 1


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    resp = gate_mod._urlopen_with_retry(object(), timeout=30)

    assert resp.status == 200
    assert calls["n"] == 3


def test_retries_on_5xx_and_connection_errors(monkeypatch):
    calls = {"n": 0}
    errors = [_http_error(503), OSError("connection reset"), None]

    def fake_urlopen(req, timeout=30):
        idx = calls["n"]
        calls["n"] += 1
        err = errors[idx]
        if err is not None:
            raise err
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    resp = gate_mod._urlopen_with_retry(object(), timeout=30)

    assert resp.status == 200
    assert calls["n"] == 3


def test_honors_retry_after_header(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, headers={"Retry-After": "7"})
        return _FakeResponse(200)

    delays = []
    monkeypatch.setattr(gate_mod.time, "sleep", lambda s: delays.append(s))
    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    gate_mod._urlopen_with_retry(object(), timeout=30)

    assert delays == [7.0]


def test_non_retryable_http_error_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        gate_mod._urlopen_with_retry(object(), timeout=30)

    assert exc_info.value.code == 404
    assert calls["n"] == 1  # no retry burned on a non-retryable error


def test_exhausts_retries_and_raises_last_error(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        gate_mod._urlopen_with_retry(object(), timeout=30)

    assert exc_info.value.code == 503
    assert calls["n"] == gate_mod._CLICKUP_RETRY_MAX_ATTEMPTS


def test_get_uses_retry_wrapper(monkeypatch):
    """`_get` (the queue-scan chokepoint) is wired through the retry wrapper,
    not a bare urlopen — the core fix the task asked for."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(500)
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLICKUP_API_TOKEN", "tok")

    result = gate_mod._get("https://api.clickup.com/api/v2/x")

    assert result == {}
    assert calls["n"] == 2
