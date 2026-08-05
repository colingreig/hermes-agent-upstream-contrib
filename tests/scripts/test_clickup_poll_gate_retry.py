"""Regression guard for 86e2kxk4z — ClickUp client intermittency in executor runs.

scripts/clickup_poll_gate.py is EXECUTOR_ID's (62714b869845) admission gate:
every 15-min tick it calls ClickUp to scan the queue and to claim/stamp/unpark
tasks. Before this fix every call site was a single bare
``urllib.request.urlopen`` — a transient 429/5xx or network blip aborted the
whole tick with no retry at all. These tests pin ``_urlopen_with_retry``'s
behavior in isolation from real network/timing.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import scripts.clickup_poll_gate as gate_mod


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch, tmp_path):
    """Every test in this file exercises retry/backoff — never actually sleep."""
    monkeypatch.setattr(gate_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_PATH", str(tmp_path / "calls.jsonl"))


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


def test_records_one_logical_call_with_recovery_details(monkeypatch, tmp_path):
    metrics = tmp_path / "calls.jsonl"
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_PATH", str(metrics))
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503)
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    gate_mod._urlopen_with_retry(object(), timeout=30)

    records = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["schema"] == "clickup-client-call/v1"
    assert records[0]["outcome"] == "recovered"
    assert records[0]["attempts"] == 2
    assert records[0]["failure_class"] == "http_503"
    assert records[0]["timestamp"].endswith("Z")


def test_metrics_rotation_bounds_retained_files(monkeypatch, tmp_path):
    metrics = tmp_path / "calls.jsonl"
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_PATH", str(metrics))
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_MAX_BYTES", 1)
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_RETAIN_FILES", 2)

    for _ in range(4):
        gate_mod._record_clickup_call_event(
            outcome="success", attempts=1, failure_class=None, elapsed_ms=1
        )

    assert metrics.exists()
    assert (tmp_path / "calls.jsonl.1").exists()
    assert (tmp_path / "calls.jsonl.2").exists()
    assert not (tmp_path / "calls.jsonl.3").exists()


def test_task_index_curl_path_records_terminal_failure_before_stale_fallback(
    monkeypatch, tmp_path
):
    metrics = tmp_path / "calls.jsonl"
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_PATH", str(metrics))
    cached = {"tasks": [{"id": "cached-task"}]}
    monkeypatch.setattr(gate_mod.clickup_sync, "active_list_ids", lambda: ["list-1"])
    monkeypatch.setattr(gate_mod.clickup_sync, "load_cache", lambda _list_id: cached)
    monkeypatch.setattr(
        gate_mod.clickup_sync,
        "sync_list_cache",
        lambda _list_id, force=False: gate_mod.clickup_sync._curl("/list/list-1/task"),
    )

    def fail_curl(path, *, timeout=45):
        raise RuntimeError("ClickUp server/network error (HTTP 503)")

    monkeypatch.setattr(gate_mod, "_CLICKUP_SYNC_CURL", fail_curl)

    index = gate_mod.clickup_sync.load_team_task_index()

    assert [task["id"] for task in index["tasks"]] == ["cached-task"]
    assert len(index["errors"]) == 1
    records = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["client"] == "clickup_poll_gate"
    assert records[0]["outcome"] == "failure"
    assert records[0]["failure_class"] == "runtimeerror"


def test_task_index_curl_429_is_terminal_failure_not_success(monkeypatch, tmp_path):
    metrics = tmp_path / "calls.jsonl"
    monkeypatch.setattr(gate_mod, "_CLICKUP_METRICS_PATH", str(metrics))
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")

    class _CurlResult:
        stdout = '{"err":"rate limited"}\n__HTTP_STATUS__429'

    monkeypatch.setattr(
        gate_mod.clickup_sync.subprocess,
        "run",
        lambda *args, **kwargs: _CurlResult(),
    )
    with pytest.raises(RuntimeError, match="HTTP 429"):
        gate_mod._observed_clickup_sync_curl("/team/9017245888/task")

    records = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["outcome"] == "failure"
    assert records[0]["failure_class"] == "runtimeerror"


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


def test_late_start_does_not_retry_past_process_deadline_reserve(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(gate_mod, "_SCRIPT_START", 1_000.0)
    monkeypatch.setattr(
        gate_mod.time,
        "time",
        lambda: 1_000.0 + gate_mod._CLICKUP_RETRY_DEADLINE_S - 0.5,
    )
    monkeypatch.setattr(gate_mod.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        gate_mod._urlopen_with_retry(object(), timeout=30)

    assert exc_info.value.code == 503
    assert calls["n"] == 1
    assert sleeps == []


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
