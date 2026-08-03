"""Regression guard for 86e2ky2e9 — the writer-served ledger wasn't wired to
the ClickUp executor cron path.

``~/.hermes/logs/writer-served.jsonl`` was, until now, only written by the
opencode writer/content delegate (``opencode_exec.py``'s ``_record_served``).
Any cron job that instead runs the normal in-process agent conversation loop
— which includes the ClickUp executor lane — served calls that showed up in
``agent.log`` but never produced a ledger row. These tests pin:

1. The real scheduler boundary constructs cron agents that append ledger rows
   on both chat-completions and codex-app-server runtime paths.
2. Router/provider-reported response models win over requested aliases.
3. ``_record_cron_served_ledger`` writes the compatible schema consumed by
   ``hermes_report_build.py``.
4. Subscription-included cost is stamped with ``billing_mode`` the same way
   the state.db write next to it already does.
5. MoA advisor cost is folded into the recorded ``cost_usd``.
6. A ledger-write failure is fail-open — it must never raise into the turn.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import conversation_loop
from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    TurnResult,
)
from cron import scheduler
from run_agent import AIAgent


def _cost_result(amount_usd, status="billed", source="pricing_table"):
    return SimpleNamespace(amount_usd=amount_usd, status=status, source=source)


def _agent(session_id="cron_62714b869845_20260803_010500", platform="cron"):
    return SimpleNamespace(session_id=session_id, platform=platform)


def _mock_response(model="gpt-5.6-sol"):
    message = SimpleNamespace(content="done", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        ),
    )


def test_records_provider_and_model(tmp_path, monkeypatch):
    ledger = tmp_path / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    conversation_loop._record_cron_served_ledger(
        _agent(), "gpt-5.6-sol", "openai-codex", "https://example/v1",
        _cost_result(0.0123),
    )

    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["served_model"] == "gpt-5.6-sol"
    assert row["served_provider"] == "openai-codex"
    assert row["billing_provider"] == "openai-codex"
    assert row["billing_base_url"] == "https://example/v1"
    assert row["cost_usd"] == 0.0123
    assert row["task_id"] == "cron_62714b869845_20260803_010500"
    assert row["ok"] is True
    assert "ts" in row and row["ts"].endswith("Z")


def test_cron_conversation_run_records_served_provider_and_model(tmp_path, monkeypatch):
    """Exercise the same AIAgent loop that scheduler runs for executor jobs."""
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model="gpt-5.6-sol",
            provider="openai-codex",
            api_mode="chat_completions",
            api_key="test-key",
            base_url="https://example.test/v1",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_id="cron_62714b869845_20260803_010500",
            platform="cron",
        )
    agent.client = MagicMock()
    # A plain MagicMock claims every optional MoA attribute exists. This is a
    # normal OpenAI-compatible client, so keep the MoA-only slot absent.
    agent.client.last_aggregator_slot = None
    agent.client.chat.completions.create.return_value = _mock_response()

    result = agent.run_conversation("Run the executor lane", conversation_history=[])

    assert result["final_response"] == "done"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert [(row["served_provider"], row["served_model"]) for row in rows] == [
        ("openai-codex", "gpt-5.6-sol")
    ]
    assert rows[0]["billing_base_url"] == "https://example.test/v1"


def test_cron_conversation_prefers_provider_reported_model(tmp_path, monkeypatch):
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model="auto",
            provider="openrouter",
            api_mode="chat_completions",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_id="cron_provider_model",
            platform="cron",
        )
    agent.client = MagicMock()
    agent.client.last_aggregator_slot = None
    agent.client.chat.completions.create.return_value = _mock_response(
        "openai/gpt-5.6-sol"
    )

    result = agent.run_conversation("Resolve the automatic route", conversation_history=[])

    assert result["final_response"] == "done"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["served_model"] == "openai/gpt-5.6-sol"


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_app_server"])
def test_scheduler_run_job_records_each_cron_runtime(
    api_mode, tmp_path, monkeypatch
):
    """Drive scheduler.run_job through a real AIAgent for both runtime lanes."""
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)

    real_agent_cls = AIAgent
    built_agents = []

    def build_agent(**kwargs):
        agent = real_agent_cls(**kwargs)
        built_agents.append(agent)
        if api_mode == "chat_completions":
            agent.client = MagicMock()
            agent.client.last_aggregator_slot = None
            agent.client.chat.completions.create.return_value = _mock_response(
                "provider/served-model"
            )
        return agent

    def fake_codex_turn(self, user_input, **kwargs):
        return TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            turn_id="turn-cron-ledger",
            thread_id="thread-cron-ledger",
            resolved_model="provider/served-model",
            model_provenance="thread_start_response",
            resolved_provider="openai",
            token_usage_last={
                "totalTokens": 18,
                "inputTokens": 11,
                "cachedInputTokens": 0,
                "outputTokens": 7,
                "reasoningOutputTokens": 0,
            },
        )

    runtime = {
        "provider": "openai-codex",
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "api_mode": api_mode,
        "command": None,
        "args": None,
    }
    job = {
        "id": f"ledger-{api_mode}",
        "name": "ledger integration",
        "prompt": "Run the executor lane",
        "model": "requested-model",
        "provider": "openai-codex",
        "enabled_toolsets": [],
    }

    with (
        patch("run_agent.AIAgent", side_effect=build_agent),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", side_effect=RuntimeError("disabled for test")),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch("agent.credential_pool.load_pool") as load_pool,
        patch.object(CodexAppServerSession, "run_turn", fake_codex_turn),
    ):
        load_pool.return_value.has_credentials.return_value = False
        success, _output, final_response, error = scheduler.run_job(job)

    assert success is True, error
    assert final_response == "done"
    assert built_agents and built_agents[0].platform == "cron"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["served_provider"] == "openai-codex"
    assert row["served_model"] == "provider/served-model"
    if api_mode == "codex_app_server":
        assert row["served_model_provenance"] == "thread_start_response"


def test_successful_cron_app_server_turn_without_usage_still_records_row(
    tmp_path, monkeypatch
):
    """A successful call is observable even if Codex omits tokenUsage."""
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    def fake_codex_turn(self, user_input, **kwargs):
        return TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            turn_id="turn-no-usage",
            thread_id="thread-no-usage",
            resolved_model="gpt-provider-resolved",
            model_provenance="thread_start_response",
            resolved_provider="openai",
        )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch.object(CodexAppServerSession, "run_turn", fake_codex_turn),
    ):
        agent = AIAgent(
            model="requested-alias",
            provider="openai-codex",
            api_mode="codex_app_server",
            api_key="test-key",
            base_url="https://example.test/v1",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_id="cron_no_usage",
            platform="cron",
        )
        result = agent.run_conversation("Run without usage", conversation_history=[])

    assert result["completed"] is True
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["served_model"] == "gpt-provider-resolved"
    assert rows[0]["served_provider"] == "openai-codex"
    assert rows[0]["served_model_provenance"] == "thread_start_response"
    assert rows[0]["cost_usd"] is None
    assert rows[0]["billing_mode"] is None


@pytest.mark.parametrize(
    ("interrupted", "error"),
    [(True, None), (False, "provider failed")],
)
def test_unsuccessful_cron_app_server_turn_with_usage_does_not_record_served_row(
    interrupted, error, tmp_path, monkeypatch
):
    """Usage emitted before an abort must not make the failed turn look served."""
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    def fake_codex_turn(self, user_input, **kwargs):
        return TurnResult(
            final_text="partial",
            projected_messages=[{"role": "assistant", "content": "partial"}],
            interrupted=interrupted,
            error=error,
            turn_id="turn-unsuccessful-with-usage",
            thread_id="thread-unsuccessful-with-usage",
            resolved_model="gpt-provider-resolved",
            model_provenance="thread_start_response",
            resolved_provider="openai",
            token_usage_last={
                "totalTokens": 18,
                "inputTokens": 11,
                "cachedInputTokens": 0,
                "outputTokens": 7,
                "reasoningOutputTokens": 0,
            },
        )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch.object(CodexAppServerSession, "run_turn", fake_codex_turn),
    ):
        agent = AIAgent(
            model="requested-alias",
            provider="openai-codex",
            api_mode="codex_app_server",
            api_key="test-key",
            base_url="https://example.test/v1",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_id="cron_unsuccessful_with_usage",
            platform="cron",
        )
        result = agent.run_conversation("Abort after usage", conversation_history=[])

    assert result["completed"] is False
    assert not ledger.exists()


def test_subscription_included_sets_billing_mode(tmp_path, monkeypatch):
    ledger = tmp_path / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    conversation_loop._record_cron_served_ledger(
        _agent(), "claude-sonnet-5", "anthropic", "",
        _cost_result(0.0, status="included"),
    )

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["billing_mode"] == "subscription_included"
    assert row["cost_usd"] == 0.0


def test_moa_advisor_cost_is_folded_in(tmp_path, monkeypatch):
    ledger = tmp_path / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    conversation_loop._record_cron_served_ledger(
        _agent(), "closed", "moa", "", _cost_result(0.01), moa_ref_cost=0.05,
    )

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["cost_usd"] == pytest.approx(0.06)


def test_moa_advisor_cost_is_preserved_when_aggregator_cost_is_unknown(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    conversation_loop._record_cron_served_ledger(
        _agent(), "closed", "moa", "", _cost_result(None), moa_ref_cost=0.05,
    )

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["cost_usd"] == pytest.approx(0.05)


def test_missing_cost_result_records_null_cost(tmp_path, monkeypatch):
    ledger = tmp_path / "writer-served.jsonl"
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", ledger)

    conversation_loop._record_cron_served_ledger(
        _agent(), "gpt-5.6-sol", "openai-codex", "", None,
    )

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["cost_usd"] is None
    assert row["served_model"] == "gpt-5.6-sol"


def test_ledger_write_failure_is_fail_open(tmp_path, monkeypatch):
    # Point the ledger at a path whose parent can never be created (a file
    # sitting where a directory needs to go) so the append raises internally
    # — the helper must swallow it rather than propagate into the turn.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("blocking file")
    monkeypatch.setattr(conversation_loop, "_CRON_SERVED_LEDGER", blocker / "writer-served.jsonl")

    # Must not raise.
    conversation_loop._record_cron_served_ledger(
        _agent(), "gpt-5.6-sol", "openai-codex", "", _cost_result(0.01),
    )
