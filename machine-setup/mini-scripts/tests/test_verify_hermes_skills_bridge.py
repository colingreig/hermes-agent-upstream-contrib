"""Behavioral fixture for verify-hermes-patches.sh section 7."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
VERIFIER = SCRIPTS / "verify-hermes-patches.sh"


def _probe_source() -> str:
    source = VERIFIER.read_text(encoding="utf-8")
    section = source.split('hdr "7. Skills bridge', 1)[1].split(
        "# 7c. The reconciler receipt", 1
    )[0]
    match = re.search(
        r"SNAP_RESULT=.*?<<'PY' 2>&1\n(?P<source>.*?)\nPY\n  \)",
        section,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("section 7 live-index probe heredoc not found")
    return match.group("source")


class SkillsBridgeVerifierTests(unittest.TestCase):
    def test_reconciler_verification_uses_explicit_release_source_root(self):
        source = VERIFIER.read_text(encoding="utf-8")
        self.assertIn(
            'SKILLS_RECONCILER="$REPO/machine-setup/mini-scripts/'
            'reconcile_marketplace_skills.py"',
            source,
        )
        self.assertIn('[ ! -L "$SKILLS_RECONCILER" ]', source)
        self.assertIn('"$REPO/venv/bin/python" "$SKILLS_RECONCILER" verify', source)
        self.assertIn('--source-root "$REPO/machine-setup/mini-scripts"', source)
        self.assertNotIn(
            '"$REPO/venv/bin/python" "$HOME/.hermes/scripts/'
            'reconcile_marketplace_skills.py"',
            source,
        )

    def test_live_index_parser_strips_yaml_delimiter_from_skill_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = root / "agent"
            agent.mkdir()
            (agent / "__init__.py").write_text("", encoding="utf-8")
            (agent / "prompt_builder.py").write_text(
                "def build_skills_system_prompt(*, available_tools, "
                "available_toolsets):\n"
                '    return "<available_skills>\\n'
                "    - blog-write: Writes a blog.\\n"
                "    - plugin:skill: Namespaced skill.\\n"
                '</available_skills>"\n',
                encoding="utf-8",
            )
            config = root / "config.yaml"
            config.write_text("skills:\n  index_floor: 2\n", encoding="utf-8")
            snapshot = root / "unused-snapshot.json"

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _probe_source(),
                    str(snapshot),
                    str(root),
                    str(config),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COUNT 2", result.stdout)
        self.assertIn("MISSING \n", result.stdout)


if __name__ == "__main__":
    unittest.main()
