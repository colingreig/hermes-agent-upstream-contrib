"""Regression guard for 86e2kxk4z — ClickUp client intermittency in executor
runs. opencode_exec.py's ``_stamp_worked_by_hermes`` is the executor's own
best-effort ClickUp write (the "Worked By" field); before this fix it was a
single bare ``urllib.request.urlopen`` with no retry, so one transient
429/5xx/network blip silently dropped the stamp for good.

Run with: PYTHONPATH=machine-setup/mini-scripts pytest machine-setup/mini-scripts/tests/test_opencode_exec_clickup_retry.py
(same convention as test_opencode_content_route.py — opencode_exec.py imports
sibling module spend_opencode, which is only importable via that PYTHONPATH.)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
OPENCODE_EXEC = SCRIPTS / "opencode_exec.py"


def _load_opencode_exec():
    spec = importlib.util.spec_from_file_location("opencode_exec_retry_test", OPENCODE_EXEC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def _http_error(code):
    return urllib.error.HTTPError("https://api.clickup.com/api/v2/x", code, "err", {}, None)


class OpencodeExecClickupRetryTests(unittest.TestCase):
    def setUp(self):
        self._home_ctx = tempfile.TemporaryDirectory()
        home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {"HOME": home}, clear=False)
        self._env_patch.start()
        self.mod = _load_opencode_exec()
        # Never actually sleep during retries in tests.
        self._sleep_patch = mock.patch.object(self.mod.time, "sleep", lambda _s: None)
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_urlopen_with_retry_succeeds_after_transient_5xx(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=30):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(503)
            return _FakeResponse(200)

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            resp = self.mod._urlopen_with_retry(object(), timeout=30)

        self.assertEqual(resp.status, 200)
        self.assertEqual(calls["n"], 2)

    def test_urlopen_with_retry_gives_up_on_non_retryable_error(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=30):
            calls["n"] += 1
            raise _http_error(401)

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.mod._urlopen_with_retry(object(), timeout=30)

        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(calls["n"], 1)

    def test_stamp_worked_by_hermes_retries_transient_failures(self):
        os.environ["CLICKUP_API_TOKEN"] = "tok-abc"
        calls = {"n": 0}

        def fake_urlopen(req, timeout=30):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection reset")
            return _FakeResponse(200)

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            self.mod._stamp_worked_by_hermes("task-123")  # must not raise

        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
