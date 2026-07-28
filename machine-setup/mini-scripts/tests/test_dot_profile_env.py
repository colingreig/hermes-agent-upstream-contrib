#!/usr/bin/env python3
"""Hermetic contracts for the mini login-shell profile environment."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "dot-profile"


def _parse_env_lines(output: str) -> dict[str, str]:
    parsed = {}
    for line in output.splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


class DotProfileEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def _source_profile(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
        }
        env.update(extra_env or {})
        command = (
            f". {shlex.quote(str(SOURCE))}; "
            "printf 'PATH=%s\\n' \"$PATH\"; "
            "printf 'GIT_CONFIG_GLOBAL=%s\\n' \"${GIT_CONFIG_GLOBAL-}\""
        )
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return _parse_env_lines(result.stdout)

    def test_exports_hermes_gitconfig_when_present(self):
        gitconfig = self.home / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")

        env = self._source_profile()

        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(gitconfig))
        self.assertTrue(env["PATH"].startswith(str(self.home / ".hermes" / "bin") + ":"))

    def test_preserves_explicit_git_config_override(self):
        gitconfig = self.home / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")

        env = self._source_profile({"GIT_CONFIG_GLOBAL": "/tmp/custom-gitconfig"})

        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/tmp/custom-gitconfig")

    def test_honors_git_config_noglobal(self):
        gitconfig = self.home / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")

        env = self._source_profile({"GIT_CONFIG_NOGLOBAL": "1"})

        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "")


if __name__ == "__main__":
    unittest.main()
