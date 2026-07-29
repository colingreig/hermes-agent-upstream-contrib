#!/usr/bin/env python3
"""Non-mutating Mini proof for ClickUp lifecycle prompt policy.

Loads the live durable MEMORY.md through the active runtime's MemoryStore,
then evaluates a Slack-shaped capture against a recording ClickUp double.
No network calls and no production task mutation occur.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


HOME = Path("/Users/colingreig/.hermes")
LIVE = HOME / "runtime-current"
if not LIVE.is_symlink():
    raise SystemExit("runtime-current is not a symlink")
LIVE = LIVE.resolve()
if not (LIVE / "agent" / "agent_init.py").is_file():
    raise SystemExit(f"active deployed source is incomplete: {LIVE}")
sys.path.insert(0, str(LIVE))

from tools.memory_tool import MemoryStore, get_memory_dir  # noqa: E402


EXPECTED = (
    "Product Build list `901714674310` with initial status `to do`",
    "New/captured tasks have no agent-ready or prepped tag.",
    "canonical `## ⚙️ Execution Brief` ends exactly `Execution-ready: YES`",
    "every product decision is resolved",
    "all native predecessor tasks are complete",
    "exactly one colon-form model:* tag exists",
)

USER_EXPECTED = (
    "eligible low-risk work",
    "Non-low-risk changes remain subject to the applicable human merge/deploy gate",
)

SKILL_PATH = HOME / "skills" / "operations" / "clickup-task-capture" / "SKILL.md"
SKILL_EXPECTED = (
    "/opt/homebrew/bin/node /Users/colingreig/.hermes/scripts/clickup/clickup.mjs",
    "Product Build, list `901714674310`, with status `to do`",
    "no `agent-ready` or `prepped` tag",
)


class RecordingClickUp:
    """Captures a Slack-shaped create request without touching ClickUp."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_task(self, *, list_id: str, name: str, status: str, tags: list[str]) -> None:
        self.calls.append(
            {"list_id": list_id, "name": name, "status": status, "tags": tags}
        )


def main() -> None:
    deployed_loader = (LIVE / "agent" / "agent_init.py").read_text(encoding="utf-8")
    assert "agent._memory_store.load_from_disk()" in deployed_loader
    memory_path = get_memory_dir() / "MEMORY.md"
    assert memory_path == HOME / "memories" / "MEMORY.md", memory_path
    raw = memory_path.read_text(encoding="utf-8")
    assert all(fragment in raw for fragment in EXPECTED), "live MEMORY.md lacks canonical lifecycle gates"

    store = MemoryStore()
    store.load_from_disk()
    prompt = store.format_for_system_prompt("memory")
    user_prompt = store.format_for_system_prompt("user")
    assert prompt is not None
    assert user_prompt is not None
    assert all(fragment in prompt for fragment in EXPECTED), "deployed MemoryStore dropped a lifecycle gate"
    assert all(fragment in user_prompt for fragment in USER_EXPECTED), "deployed MemoryStore dropped a delivery gate"

    skill_raw = SKILL_PATH.read_text(encoding="utf-8")
    assert all(fragment in skill_raw for fragment in SKILL_EXPECTED), "durable CLI skill lacks a required gate"
    from agent.skill_commands import build_skill_invocation_message, scan_skill_commands
    commands = scan_skill_commands()
    assert "/clickup-task-capture" in commands, "CLI helper skill is not slash-discoverable"
    invocation = build_skill_invocation_message("/clickup-task-capture", task_id="slack-eval")
    assert invocation is not None
    assert all(fragment in invocation for fragment in SKILL_EXPECTED), "Slack skill invocation did not load durable CLI instructions"

    from model_tools import get_tool_definitions
    tools = get_tool_definitions(["hermes-slack"], [], quiet_mode=True)
    assert "terminal" in {tool["function"]["name"] for tool in tools}, "Slack lacks terminal tool"

    route = re.search(
        r"Product Build list `(?P<list_id>\d+)` with initial status `(?P<status>[^`]+)`",
        prompt,
    )
    assert route is not None
    clickup = RecordingClickUp()
    clickup.create_task(
        list_id=route.group("list_id"),
        name="Slack capture: account CSV export",
        status=route.group("status"),
        tags=[],
    )
    assert clickup.calls == [
        {
            "list_id": "901714674310",
            "name": "Slack capture: account CSV export",
            "status": "to do",
            "tags": [],
        }
    ]
    assert "agent-ready" not in clickup.calls[0]["tags"]
    assert "prepped" not in clickup.calls[0]["tags"]
    print(f"PASS deployed_source={LIVE}")
    print("PASS memory_loader=live MEMORY.md -> MemoryStore.format_for_system_prompt")
    print("PASS slack_skill=slash invocation loads terminal-backed ClickUp helper instructions")
    print("PASS slack_capture=recording double only; no ClickUp mutation")


if __name__ == "__main__":
    main()
