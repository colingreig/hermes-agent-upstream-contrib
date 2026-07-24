"""86e2abmkb (ignite-validate FAIL, 2026-07-22) — AC3: "a representative Slack
request creates a to-do task with no agent-ready until Prep marks it
execution-ready."

Repo-boundary note (confirmed independently across three separate
investigation passes — see the task's comment thread): hermes-agent contains
NO ClickUp task-creation code path at all. Capturing a Slack message as a
ClickUp task is entirely agent/LLM-driven via the live system prompt +
memories content + an external MCP server, none of which live in this repo.
So this cannot be a test of "call function X, assert it doesn't pass
agent-ready" — that function doesn't exist here.

What CAN be scripted and IS the actual mechanism that governs the real
behavior: the corrected MEMORY.md "Task lifecycle contract" wording (applied
live on the mini for this task) must actually reach the assembled system
prompt Hermes runs on, and must not itself regress into the stale
direct-self-tag instruction the doctor diagnostic (AC4,
hermes_cli.doctor._detects_stale_agent_ready_instruction) exists to catch.
This test drives the REAL MemoryStore.load_from_disk() ->
format_for_system_prompt() pipeline (the same one agent/system_prompt.py
calls every session) against that exact corrected content, proving the
"agent-ready only after Prep" gate is what a live session actually sees.
"""
from pathlib import Path

from tools.memory_tool import MemoryStore
from hermes_cli.doctor import _detects_stale_agent_ready_instruction

# Shared verbatim paragraph deployed in the mini's MEMORY.md.  The
# Slack-shaped prompt regression consumes this same source so the two tests
# cannot silently drift to different versions of the lifecycle policy.
LIFECYCLE_CONTRACT_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "hermes_memory_task_lifecycle_contract.md"
)


def _deployed_lifecycle_contract() -> str:
    return LIFECYCLE_CONTRACT_PATH.read_text(encoding="utf-8")


def _write_memory_md(tmp_path, monkeypatch, content: str):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")


def test_deployed_task_lifecycle_contract_reaches_the_live_system_prompt(tmp_path, monkeypatch):
    """The exact wording now live on the mini's MEMORY.md survives the real
    load_from_disk() -> format_for_system_prompt() pipeline unchanged (no
    sanitizer/threat-scan false positive on this legitimate content), so a
    running session actually sees the "agent-ready only after Prep" gate."""
    _write_memory_md(tmp_path, monkeypatch, _deployed_lifecycle_contract())

    store = MemoryStore()
    store.load_from_disk()
    rendered = store.format_for_system_prompt("memory")

    assert rendered is not None, "corrected memory content must not be dropped/blocked"
    assert "New/captured tasks have no agent-ready or prepped tag." in rendered
    assert "Prep may add agent-ready only when" in rendered
    assert "Execution-ready: YES" in rendered
    assert "product decision" in rendered
    assert "predecessor tasks are complete" in rendered
    assert "exactly one colon-form model:* tag" in rendered


def test_deployed_task_lifecycle_contract_does_not_trip_the_stale_self_tag_diagnostic(tmp_path, monkeypatch):
    """Regression pin tying AC2 (corrected wording) to AC4 (the doctor
    diagnostic that flags a stale direct-self-tag instruction) — the new
    wording must read as compliant, not as a fresh violation."""
    _write_memory_md(tmp_path, monkeypatch, _deployed_lifecycle_contract())

    store = MemoryStore()
    store.load_from_disk()
    rendered = store.format_for_system_prompt("memory")

    assert _detects_stale_agent_ready_instruction(rendered) is None


def test_old_bare_self_tag_wording_would_have_been_caught(tmp_path, monkeypatch):
    """Contrast case: the ORIGINAL stale instruction quoted in this task's
    audit evidence ("New Slack work should be captured as a ClickUp task
    tagged agent-ready") — proving the diagnostic would have caught exactly
    the regression this task fixes, through the same real pipeline."""
    stale = "New Slack work should be captured as a ClickUp task tagged agent-ready."
    _write_memory_md(tmp_path, monkeypatch, stale)

    store = MemoryStore()
    store.load_from_disk()
    rendered = store.format_for_system_prompt("memory")

    assert _detects_stale_agent_ready_instruction(rendered) is not None
