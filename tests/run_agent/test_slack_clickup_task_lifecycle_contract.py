"""Real prompt/tool-boundary coverage for ClickUp task 86e2abmkb.

Hermes does not own ClickUp task creation: an LLM chooses an external MCP
tool from the assembled prompt.  This test therefore uses the real
``AIAgent`` prompt and tool loop with a deterministic ClickUp MCP double.  It
does not pretend that a repository function enforces the remote board
policy; it proves that the durable policy reaches a Slack-shaped model call,
the create schema is exposed, and the resulting calls honor the lifecycle
contract at the boundary Hermes actually owns.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from tools.memory_tool import MemoryStore


CREATE_TASK = "mcp_clickup_create_task"
READ_TASK = "mcp_clickup_read_task"
ADD_TAG = "mcp_clickup_add_tag"

CLICKUP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": CREATE_TASK,
            "description": "Create a ClickUp task in a specific list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["list_id", "name", "description", "status", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": READ_TASK,
            "description": "Read one ClickUp task, including dependencies and tags.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ADD_TAG,
            "description": "Add one tag to an existing ClickUp task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["task_id", "tag"],
            },
        },
    },
]

FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "hermes_memory_task_lifecycle_contract.md"
)


def _tool_call(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(*, content: str | None = None, tool_call=None):
    tool_calls = [tool_call] if tool_call is not None else []
    message = SimpleNamespace(
        content=content,
        reasoning=None,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
        usage=None,
    )


def _tool_result_payload(content: str) -> dict:
    """Extract JSON from Hermes' untrusted-external-result wrapper."""
    start = content.find("{")
    end = content.rfind("}")
    assert start >= 0 and end >= start, f"missing JSON tool result: {content!r}"
    return json.loads(content[start : end + 1])


def _assert_prompt_contains_lifecycle_gates(system_prompt: str) -> None:
    """Assert lifecycle semantics, not incidental wording in the fixture."""
    normalized = " ".join(system_prompt.lower().split())
    assert re.search(
        r"(?:new|captured)(?:\s*/\s*(?:new|captured))?\s+tasks?\s+have\s+no\s+"
        r"agent-ready\s+or\s+prepped\s+tag",
        normalized,
    )
    assert re.search(r"prep.*?add\s+agent-ready.*?execution-ready:\s*yes", normalized)
    assert "product decision" in normalized
    assert "predecessor task" in normalized
    assert re.search(r"exactly one.*?model:\*\s+tag", normalized)


class _PolicyEvaluatingClickUpModel:
    """Small deterministic stand-in for the external model.

    Values for the initial create call are read from the assembled durable
    prompt rather than copied from the test expectation.  Promotion is
    emitted only after reading task state through the MCP boundary and
    independently evaluating every gate named in that prompt.
    """

    def __init__(self):
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        messages = kwargs["messages"]
        assert messages[0]["role"] == "system"
        system_prompt = messages[0]["content"]
        assert "You are in a Slack workspace" in system_prompt
        assert "Task lifecycle contract:" in system_prompt
        _assert_prompt_contains_lifecycle_gates(system_prompt)
        assert kwargs["tools"] == CLICKUP_TOOL_SCHEMAS

        if messages[-1]["role"] == "tool":
            result = _tool_result_payload(messages[-1]["content"])
            if result["operation"] in {"create", "add_tag"}:
                return _response(content="Done.")

            assert result["operation"] == "read"
            task = result["task"]
            model_floors = [
                tag
                for tag in task["tags"]
                if re.fullmatch(r"model:(?:haiku|sonnet|opus|fable)", tag)
            ]
            ready = (
                "## ⚙️ Execution Brief" in task["description"]
                and task["description"].rstrip().endswith("Execution-ready: YES")
                and task["product_decisions"] == "resolved"
                and all(
                    dependency["status"] == "complete"
                    for dependency in task["dependencies"]
                )
                and len(model_floors) == 1
            )
            if not ready:
                return _response(content="Not ready for agent-ready.")
            return _response(
                tool_call=_tool_call(
                    ADD_TAG,
                    {"task_id": task["id"], "tag": "agent-ready"},
                    "call_promote",
                )
            )

        user_message = next(
            message["content"]
            for message in reversed(messages)
            if message["role"] == "user"
        )
        if user_message.startswith("Capture this Thermal product task:"):
            route = re.search(
                r"Product Build list `(?P<list_id>\d+)` with initial status "
                r"`(?P<status>[^`]+)`",
                system_prompt,
            )
            assert route is not None, "durable prompt must carry routing policy"
            return _response(
                tool_call=_tool_call(
                    CREATE_TASK,
                    {
                        "list_id": route.group("list_id"),
                        "name": "Add account CSV export",
                        "description": (
                            "Allow account owners to download their data as CSV."
                        ),
                        "status": route.group("status"),
                        "tags": [],
                    },
                    "call_create",
                )
            )

        return _response(
            tool_call=_tool_call(
                READ_TASK,
                {"task_id": "thermal-task-1"},
                f"call_read_{len(self.requests)}",
            )
        )


class _FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


class _StatefulClickUp:
    """Capturing MCP server double with independent state-side invariants."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.tasks: dict[str, dict] = {}

    @property
    def task(self) -> dict:
        return self.tasks["thermal-task-1"]

    def _agent_ready_allowed(self, task: dict) -> bool:
        model_floors = [
            tag
            for tag in task["tags"]
            if re.fullmatch(r"model:(?:haiku|sonnet|opus|fable)", tag)
        ]
        return (
            "## ⚙️ Execution Brief" in task["description"]
            and task["description"].rstrip().endswith("Execution-ready: YES")
            and task["product_decisions"] == "resolved"
            and all(
                dependency["status"] == "complete"
                for dependency in task["dependencies"]
            )
            and len(model_floors) == 1
        )

    def dispatch(self, name, args, task_id=None, **kwargs):
        self.calls.append((name, deepcopy(args)))
        if name == CREATE_TASK:
            task = {
                "id": "thermal-task-1",
                **deepcopy(args),
                "product_decisions": "unresolved",
                "dependencies": [],
            }
            self.tasks[task["id"]] = task
            return json.dumps({"ok": True, "operation": "create", "task": task})

        task = self.tasks[args["task_id"]]
        if name == READ_TASK:
            return json.dumps(
                {"ok": True, "operation": "read", "task": deepcopy(task)}
            )

        assert name == ADD_TAG
        assert args["tag"] == "agent-ready"
        assert self._agent_ready_allowed(task), (
            "the fake ClickUp boundary must reject agent-ready until stored "
            "task state satisfies every Prep prerequisite"
        )
        task["tags"].append(args["tag"])
        return json.dumps(
            {"ok": True, "operation": "add_tag", "task": deepcopy(task)}
        )

    def set_promotion_state(
        self,
        *,
        description: str,
        product_decisions: str,
        dependencies: list[dict],
        tags: list[str],
    ) -> None:
        task = self.task
        task["description"] = description
        task["product_decisions"] = product_decisions
        task["dependencies"] = deepcopy(dependencies)
        task["tags"] = list(tags)


def test_slack_capture_and_prep_promotion_cross_the_real_agent_boundary(
    tmp_path,
    monkeypatch,
):
    from run_agent import AIAgent

    durable_contract = FIXTURE_PATH.read_text(encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(durable_contract, encoding="utf-8")
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

    memory_store = MemoryStore()
    memory_store.load_from_disk()

    model = _PolicyEvaluatingClickUpModel()
    clickup = _StatefulClickUp()

    monkeypatch.setattr("run_agent.OpenAI", lambda **kwargs: _FakeClient(model))
    monkeypatch.setattr(
        "run_agent.get_tool_definitions",
        lambda *args, **kwargs: CLICKUP_TOOL_SCHEMAS,
    )
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.handle_function_call", clickup.dispatch)

    agent = AIAgent(
        model="test-model",
        api_key="test-key",
        base_url="http://localhost:8080/v1",
        platform="slack",
        max_iterations=3,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._disable_streaming = True
    agent._memory_store = memory_store
    agent._memory_enabled = True
    agent._user_profile_enabled = False

    agent.run_conversation(
        "Capture this Thermal product task: account owners need a CSV export."
    )

    assert agent.platform == "slack"
    assert model.requests[0]["messages"][0]["content"] == agent._cached_system_prompt
    assert clickup.calls == [
        (
            CREATE_TASK,
            {
                "list_id": "901714674310",
                "name": "Add account CSV export",
                "description": "Allow account owners to download their data as CSV.",
                "status": "to do",
                "tags": [],
            },
        )
    ]
    initial_task = clickup.task
    assert initial_task["status"] == "to do"
    assert initial_task["product_decisions"] == "unresolved"
    assert initial_task["dependencies"] == []
    initial_tags = initial_task["tags"]
    assert "agent-ready" not in initial_tags
    assert "prepped" not in initial_tags

    canonical_description = (
        "## ⚙️ Execution Brief\n"
        "Implement account CSV export with the recorded privacy constraints.\n"
        "Execution-ready: YES"
    )
    complete_dependencies = [{"id": "predecessor-1", "status": "complete"}]
    invalid_states = [
        {
            "description": canonical_description.replace("⚙️ ", ""),
            "product_decisions": "resolved",
            "dependencies": complete_dependencies,
            "tags": ["model:sonnet"],
        },
        {
            "description": canonical_description,
            "product_decisions": "unresolved",
            "dependencies": complete_dependencies,
            "tags": ["model:sonnet"],
        },
        {
            "description": canonical_description,
            "product_decisions": "resolved",
            "dependencies": [{"id": "predecessor-1", "status": "in progress"}],
            "tags": ["model:sonnet"],
        },
        {
            "description": canonical_description,
            "product_decisions": "resolved",
            "dependencies": complete_dependencies,
            "tags": ["model:sonnet", "model:opus"],
        },
        {
            "description": f"{canonical_description}\nPending final approval.",
            "product_decisions": "resolved",
            "dependencies": complete_dependencies,
            "tags": ["model:sonnet"],
        },
    ]
    for state in invalid_states:
        clickup.set_promotion_state(**state)
        agent.run_conversation("Read thermal-task-1 and promote it only if Prep gates pass.")
        assert clickup.calls[-1] == (READ_TASK, {"task_id": "thermal-task-1"})
        assert "agent-ready" not in clickup.task["tags"]

    assert [name for name, _args in clickup.calls] == [
        CREATE_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
    ]

    clickup.set_promotion_state(
        description=canonical_description,
        product_decisions="resolved",
        dependencies=complete_dependencies,
        tags=["model:sonnet"],
    )
    agent.run_conversation("Read thermal-task-1 and promote it only if Prep gates pass.")

    assert clickup.calls[-2:] == [
        (READ_TASK, {"task_id": "thermal-task-1"}),
        (
            ADD_TAG,
            {"task_id": "thermal-task-1", "tag": "agent-ready"},
        ),
    ]
    assert clickup.task["tags"] == ["model:sonnet", "agent-ready"]
    assert [name for name, _args in clickup.calls] == [
        CREATE_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        READ_TASK,
        ADD_TAG,
    ]

    cached_prompt_bytes = agent._cached_system_prompt.encode("utf-8")
    assert all(
        request["messages"][0]["content"].encode("utf-8") == cached_prompt_bytes
        for request in model.requests
    )
