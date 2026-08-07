#!/usr/bin/env python3
"""Hermetic tests for thermal_github_app_probe.py."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROBE_PATH = MINI_SCRIPTS_DIR / "thermal_github_app_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("thermal_github_app_probe", PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ThermalGithubAppProbeTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_default_owner_is_the_verified_post_transfer_owner(self):
        # thermal moved colingreig -> ignitemarketing in the 2026-08-06 org
        # transfer; a stale default here silently probes the wrong repo.
        self.assertEqual(self.mod.DEFAULT_OWNER, "ignitemarketing")

    def test_success_emits_status_codes_only(self):
        calls: list[tuple[str, str]] = []

        def fake_probe(method, url, token):
            calls.append((method, url))
            self.assertEqual(token, "test-token")
            return 200 if method == "GET" else 201

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=False):
            with mock.patch.object(self.mod, "_probe", side_effect=fake_probe):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.mod.main()

        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload, {"GET_runners": 200, "POST_registration_token": 201})
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].endswith("/actions/runners"))
        self.assertEqual(calls[1][0], "POST")
        self.assertTrue(calls[1][1].endswith("/registration-token"))

    def test_unexpected_status_is_nonzero(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=False):
            with mock.patch.object(self.mod, "_probe", return_value=403):
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = self.mod.main()
        self.assertEqual(rc, 1)
        self.assertIn("unexpected status", err.getvalue())

    def test_mint_failure_exits_77(self):
        proc = mock.Mock(returncode=77, stdout="", stderr="permanent error")
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_TOKEN", None)
            with mock.patch("subprocess.run", return_value=proc):
                err = io.StringIO()
                with redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        self.mod._mint_token()
        self.assertEqual(ctx.exception.code, 77)


if __name__ == "__main__":
    unittest.main()
