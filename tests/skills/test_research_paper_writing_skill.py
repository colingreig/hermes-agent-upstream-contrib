from __future__ import annotations

import re
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "research" / "research-paper-writing"
SKILL_MD = SKILL_DIR / "SKILL.md"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def test_research_paper_writing_skill_stays_below_save_limit_with_headroom():
    text = _skill_text()

    assert len(text) < 100_000
    assert len(text.encode("utf-8")) < 90_000


def test_research_paper_writing_local_links_resolve():
    text = _skill_text()
    missing: list[str] = []

    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
        raw_target = match.group(1).strip()
        if raw_target.startswith(("http://", "https://", "mailto:", "#", "/")):
            continue
        target = raw_target.removeprefix("./").split("#", 1)[0]
        if target and not (SKILL_DIR / target).exists():
            missing.append(raw_target)

    assert missing == []


def test_research_paper_writing_keeps_routing_critical_guidance_in_main_file():
    text = _skill_text()

    required = [
        "## When To Use This Skill",
        "Starting a new research paper",
        "Designing and running experiments",
        "Writing non-empirical papers",
        "NEVER generate BibTeX from memory. ALWAYS fetch programmatically.",
        "If an experiment doesn't map to a claim, don't run it.",
        "Core claims supported, results significant",
        "Final checks, formatting, and submission.",
        "## Common Issues and Solutions",
    ]

    missing = [phrase for phrase in required if phrase not in text]
    assert missing == []


def test_research_paper_writing_split_references_are_linked_and_substantive():
    text = _skill_text()
    expected_references = {
        "references/latex-production.md": [
            "Professional LaTeX Preamble",
            "Pseudocode with algorithm2e",
            "SciencePlots for matplotlib",
        ],
        "references/publication-and-release.md": [
            "Research Code Packaging",
            "Post-Acceptance Deliverables",
            "Anonymous code for submission",
        ],
    }

    for relative_path, required_phrases in expected_references.items():
        assert f"]({relative_path})" in text
        reference_text = (SKILL_DIR / relative_path).read_text(encoding="utf-8")
        missing = [phrase for phrase in required_phrases if phrase not in reference_text]
        assert missing == []
