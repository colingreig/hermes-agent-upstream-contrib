#!/usr/bin/env python3
"""Hermetic contract tests for the source-controlled gateway bootstrap."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parent.parent / "gateway_secrets_wrap.sh"


class GatewaySecretsWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name)
        (self.home / ".config").mkdir()
        (self.home / ".hermes/scripts").mkdir(parents=True)
        (self.home / ".hermes/runtime-current/venv/bin").mkdir(parents=True)
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".config/op-runtime-token").write_text("fake-token\n")
        (self.home / ".hermes/scripts/op-secrets.env").write_text(
            "KEY=op://Test/item/FIELD\n"
        )
        (self.home / ".hermes/scripts/op_sdk_resolve.py").write_text(
            "# fake resolver source\n"
        )
        resolver_python = self.home / ".hermes/runtime-current/venv/bin/python"
        resolver_python.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$FAKE_RESOLVER_CALL\" >> \"$HOME/resolver-calls\"\n"
            "if [ \"${FAKE_RESOLVER_STATUS:-0}\" -eq 0 ]; then\n"
            "  printf 'KEY=\"opaque-secret\"\\n'\n"
            "fi\n"
            "exit \"${FAKE_RESOLVER_STATUS:-0}\"\n"
        )
        resolver_python.chmod(0o755)
        hermes = self.home / ".local/bin/hermes"
        hermes.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$HOME/hermes-args\"\n"
        )
        hermes.chmod(0o755)

    def _run(self, status: int) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "FAKE_RESOLVER_STATUS": str(status),
                "FAKE_RESOLVER_CALL": f"status={status}",
            }
        )
        return subprocess.run(
            ["/bin/bash", str(WRAPPER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_success_launches_gateway_once(self):
        result = self._run(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            (self.home / "hermes-args").read_text().strip(),
            "gateway run --replace",
        )
        self.assertEqual(
            (self.home / "resolver-calls").read_text().splitlines(),
            ["status=0"],
        )

    def test_auth_failure_is_not_retried_or_launched(self):
        result = self._run(77)
        self.assertEqual(result.returncode, 77)
        self.assertFalse((self.home / "hermes-args").exists())
        self.assertEqual(
            (self.home / "resolver-calls").read_text().splitlines(),
            ["status=77"],
        )
        log = (self.home / ".hermes/logs/gateway.error.log").read_text()
        self.assertIn("classification=permanent-auth", log)

    def test_transient_exhaustion_is_observable_and_not_retried_by_launcher(self):
        result = self._run(75)
        self.assertEqual(result.returncode, 75)
        self.assertFalse((self.home / "hermes-args").exists())
        self.assertEqual(
            (self.home / "resolver-calls").read_text().splitlines(),
            ["status=75"],
        )
        log = (self.home / ".hermes/logs/gateway.error.log").read_text()
        self.assertIn("classification=transient-exhausted", log)
        self.assertIn("bounded retries exhausted", log)


if __name__ == "__main__":
    unittest.main()
