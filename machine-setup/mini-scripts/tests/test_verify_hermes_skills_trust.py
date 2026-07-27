"""Focused fixtures for verify-hermes-patches.sh section 32."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent.parent
VERIFIER = SCRIPTS / "verify-hermes-patches.sh"
ROOT_NEEDLE = "_trusted_dirs = [active_skills_dir.resolve()]"
SKILL_NEEDLE = "skill_md.resolve().relative_to(_td)"


def _section_32() -> str:
    source = VERIFIER.read_text(encoding="utf-8")
    return source.split("# --- 32.", 1)[1].split('hdr "Result"', 1)[0]


def _probe_source() -> str:
    section = _section_32()
    match = re.search(
        r"SK_RESULT=.*?<<'PY' 2>/dev/null\n(?P<source>.*?)\nPY\n\)",
        section,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("section 32 behavioral probe heredoc not found")
    return match.group("source")


def _structural_contract(source: str) -> bool:
    """Mirror section 32's two exact, intentionally narrow grep sentinels."""
    return ROOT_NEEDLE in source and SKILL_NEEDLE in source


def _run_probe(hermes_home: Path) -> dict[str, int]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [sys.executable, "-c", _probe_source()],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, _, raw = line.partition(" ")
        if key in {"CHECKED", "FALSE_POSITIVES", "OUTSIDE_TRUST"}:
            values[key] = int(raw)
    return values


def _write_skill(skills_dir: Path, name: str = "inside") -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: fixture\n---\n\nFixture.\n",
        encoding="utf-8",
    )
    return skill_md


class SkillsTrustVerifierTests(unittest.TestCase):
    def test_structural_contract_requires_resolved_active_root_and_skill(self):
        section = _section_32()
        self.assertTrue(_structural_contract(section))
        self.assertNotIn("SKILLS_DIR.resolve()", section)

        unresolved_root = section.replace(ROOT_NEEDLE, "_trusted_dirs = [active_skills_dir]")
        self.assertFalse(_structural_contract(unresolved_root))

        unresolved_skill = section.replace(
            SKILL_NEEDLE, "skill_md.relative_to(_td)"
        )
        self.assertFalse(_structural_contract(unresolved_skill))

    def test_behavior_accepts_skill_beneath_resolved_active_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            _write_skill(home / "skills")

            self.assertEqual(
                _run_probe(home),
                {"CHECKED": 1, "FALSE_POSITIVES": 0, "OUTSIDE_TRUST": 0},
            )

    def test_behavior_rejects_skill_that_resolves_outside_active_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "profile"
            skills = home / "skills"
            skills.mkdir(parents=True)
            outside = base / "outside"
            _write_skill(outside, "escaped")
            (skills / "escaped").symlink_to(outside / "escaped", target_is_directory=True)

            self.assertEqual(
                _run_probe(home),
                {"CHECKED": 1, "FALSE_POSITIVES": 0, "OUTSIDE_TRUST": 1},
            )

    def test_behavior_accepts_symlinked_profile_root_after_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real_home = base / "profiles" / "coder"
            _write_skill(real_home / "skills")
            linked_home = base / "active-profile"
            linked_home.symlink_to(real_home, target_is_directory=True)

            self.assertEqual(
                _run_probe(linked_home),
                {"CHECKED": 1, "FALSE_POSITIVES": 0, "OUTSIDE_TRUST": 0},
            )


if __name__ == "__main__":
    unittest.main()
