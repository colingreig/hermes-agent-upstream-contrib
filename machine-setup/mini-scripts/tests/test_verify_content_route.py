"""Behavioral fixture for verify-hermes-patches.sh content-route guard."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
VERIFIER = SCRIPTS / "verify-hermes-patches.sh"
DB_PUBLISH_TASK = SCRIPTS / "db_publish_task.py"
STALE_PHRASES = (
    "(NO --model — writer cascade)",
    "(NO --model - writer cascade)",
    "CONTENT cascade: Sonnet > GLM-5.2 > Gemini",
    "prose cascade",
)


def _content_probe_source() -> str:
    source = VERIFIER.read_text(encoding="utf-8")
    section = source.split("# 18b4a. Content-publish route", 1)[1].split(
        "# 18b4b.", 1
    )[0]
    match = re.search(
        r"content_report=.*?<<'PY' 2>/dev/null\n(?P<source>.*?)\nPY\n\)",
        section,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("content-route probe heredoc not found")
    return match.group("source")


class ContentRouteVerifierTests(unittest.TestCase):
    def test_canonical_db_publish_helper_documents_content_route(self):
        source = DB_PUBLISH_TASK.read_text(encoding="utf-8")

        self.assertIn("--content", source)
        self.assertIn("Sonnet-only", source)
        self.assertIn("fail-closed", source)
        for stale in STALE_PHRASES:
            self.assertNotIn(stale, source)

    def test_probe_accepts_sonnet_only_db_publish_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oc = root / "opencode_exec.py"
            db_pub = root / "db_publish_task.py"
            oc.write_text(
                textwrap.dedent(
                    '''
                    CONTENT_CASCADE = [
                        ("anthropic/claude-sonnet-5", "content-anthropic"),
                    ]

                    def resolve_writer_cascade(content=False):
                        if content:
                            return [], "content:<no enabled Sonnet tier>"
                    '''
                ),
                encoding="utf-8",
            )
            db_pub.write_text(
                "opencode_exec.py --content uses the Sonnet-only fail-closed content route.\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "-c", _content_probe_source(), str(oc), str(db_pub)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")

    def test_probe_rejects_stale_glm_gemini_db_publish_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oc = root / "opencode_exec.py"
            db_pub = root / "db_publish_task.py"
            oc.write_text(
                'CONTENT_CASCADE = [("anthropic/claude-sonnet-5", "content-anthropic")]\n'
                'def resolve_writer_cascade(content=False):\n'
                '    return [], "content:<no enabled Sonnet tier>"\n',
                encoding="utf-8",
            )
            db_pub.write_text(
                "(NO --model - writer cascade)\n"
                "CONTENT cascade: Sonnet > GLM-5.2 > Gemini\n"
                "--content\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "-c", _content_probe_source(), str(oc), str(db_pub)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stale-db-publish-GLM-Gemini-help", result.stdout)
        self.assertIn("stale-db-publish-writer-cascade-help", result.stdout)
        self.assertIn("db_publish_task-missing-Sonnet-only-fail-closed-text", result.stdout)


if __name__ == "__main__":
    unittest.main()
