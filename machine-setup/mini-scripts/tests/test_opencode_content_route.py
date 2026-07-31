"""Focused checks for the Mini OpenCode content route."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
OPENCODE_EXEC = SCRIPTS / "opencode_exec.py"


def _load_opencode_exec():
    spec = importlib.util.spec_from_file_location("opencode_exec_content_test", OPENCODE_EXEC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OpencodeContentRouteTests(unittest.TestCase):
    def test_content_cascade_is_exactly_sonnet(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}, clear=False):
            mod = _load_opencode_exec()

        self.assertEqual(
            mod.CONTENT_CASCADE,
            [("anthropic/claude-sonnet-5", "content-anthropic")],
        )

    def test_content_route_fails_closed_when_sonnet_is_disabled(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"HOME": home, "HERMES_CONTENT_SONNET": "0"},
            clear=False,
        ):
            mod = _load_opencode_exec()
            models, cascade = mod.resolve_writer_cascade(content=True)

        self.assertEqual(models, [])
        self.assertEqual(cascade, "content:<no enabled Sonnet tier>")
        self.assertNotIn("openai/gpt-5", models)

    def test_content_route_selects_only_sonnet_by_default(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"HOME": home, "HERMES_CONTENT_SONNET": ""},
            clear=False,
        ):
            mod = _load_opencode_exec()
            models, cascade = mod.resolve_writer_cascade(content=True)

        self.assertEqual(models, ["anthropic/claude-sonnet-5"])
        self.assertEqual(cascade, "content:anthropic/claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
