"""Tests for the "Worked By" stamping helper in scripts/clickup_poll_gate.py.

Covers ClickUp 86e29q8pg: the Worked-By=Hermes custom field was only ever
READ (by the review-SLA staleness sweep) and never WRITTEN anywhere in the
fleet, so that safety net could never fire for tasks Hermes actually worked.
"""

from __future__ import annotations

import json

import scripts.clickup_poll_gate as gate_mod


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_stamp_worked_by_hermes_skips_without_option_id(monkeypatch, capsys):
    monkeypatch.delenv(gate_mod.WORKED_BY_HERMES_OPTION_ENV, raising=False)
    called = {}
    monkeypatch.setattr(
        gate_mod.urllib.request,
        "urlopen",
        lambda *a, **k: called.setdefault("called", True),
    )

    gate_mod._stamp_worked_by_hermes("task-123")

    assert "called" not in called
    err = capsys.readouterr().err
    assert "worked-by stamp skipped" in err
    assert gate_mod.WORKED_BY_HERMES_OPTION_ENV in err


def test_stamp_worked_by_hermes_posts_field_value(monkeypatch):
    monkeypatch.setenv(gate_mod.WORKED_BY_HERMES_OPTION_ENV, "opt-hermes-1")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "tok-abc")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse(200)

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    gate_mod._stamp_worked_by_hermes("task-123")

    assert captured["method"] == "POST"
    assert captured["url"] == (
        f"https://api.clickup.com/api/v2/task/task-123/field/{gate_mod.WORKED_BY_FIELD_ID}"
    )
    assert captured["body"] == {"value": "opt-hermes-1"}
    assert captured["auth"] == "tok-abc"


def test_stamp_worked_by_hermes_never_raises_on_api_error(monkeypatch, capsys):
    monkeypatch.setenv(gate_mod.WORKED_BY_HERMES_OPTION_ENV, "opt-hermes-1")

    def fake_urlopen(req, timeout=30):
        raise OSError("boom")

    monkeypatch.setattr(gate_mod.urllib.request, "urlopen", fake_urlopen)

    gate_mod._stamp_worked_by_hermes("task-123")  # must not raise

    err = capsys.readouterr().err
    assert "worked-by stamp failed" in err
