"""Transport-level cross-provider failover test.

Closes a coverage gap: ``tests/test_route_health.py`` only asserts the
route-health *resolver* statically reports a configured fallback entry as
``fallback_kind == "cross-provider"`` — it never drives a real request
through a simulated primary-provider transport outage. This test exercises
the actual execution path instead: it raises a transport-shaped exception
(``ReadTimeout``) from the mocked provider-call seam
(``agent._interruptible_api_call``) enough times to trip the pure
transport-failure eager-fallback gate in
``agent/conversation_loop.py::run_conversation`` —

    _is_transport_failure = classified.reason in {timeout, overloaded}
    _should_fallback = is_rate_limited or (_is_transport_failure and retry_count >= 2)

— then asserts the turn actually switches to a *different* provider (not
just a different model on the same provider, which
``test_32646_fallback_429_after_timeout.py`` already covers) and completes
successfully via the fallback provider's client.

Prior coverage of this exact branch:
  * ``test_32646_fallback_429_after_timeout.py`` drives two transport
    timeouts, but they only trigger ``_try_recover_primary_transport``
    (same-provider client rebuild); the fallback activation in that test is
    triggered by a subsequent HTTP 429 (rate_limit), not by the transport
    failure itself, and its fallback entry is same-provider
    (zai/glm-5.1 -> zai/glm-4.7).
  * No existing test drives two consecutive transport failures straight
    into the ``_is_transport_failure and retry_count >= 2`` gate and
    confirms it lands on a cross-provider fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_agent_with_cross_provider_fallback(fb_chain):
    """Build a minimal AIAgent whose primary provider is 'zai' and whose
    fallback chain points at a different provider ('groq')."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider="zai",
            model="glm-5.1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
        )
        agent.client = MagicMock()
        return agent


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


class ReadTimeout(Exception):
    """Transport-shaped exception: matched by class name (duck-typed) in
    both ``agent/error_classifier.py``'s ``_TRANSPORT_ERROR_TYPES`` and
    ``agent/agent_runtime_helpers.py``'s ``_TRANSIENT_TRANSPORT_ERRORS``."""


class TestCrossProviderFailoverSurvivesTransportOutage:
    def test_pure_transport_failures_fail_over_to_different_provider(self):
        """Two consecutive read timeouts against the primary (zai) — with no
        429/billing error anywhere in the sequence — must trip the
        transport-failure eager-fallback gate and land the turn on the
        configured cross-provider fallback (groq), which then answers the
        request successfully.
        """
        fb_chain = [
            {
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "base_url": "https://api.groq.com/openai/v1",
            }
        ]
        agent = _make_agent_with_cross_provider_fallback(fb_chain)
        agent._api_max_retries = 2

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            attempt = len(calls)
            if attempt <= 2:
                raise ReadTimeout("read timed out")
            return _mock_response("Recovered via cross-provider fallback")

        mock_fb_client = MagicMock()
        mock_fb_client.api_key = "groq-fallback-key"
        mock_fb_client.base_url = "https://api.groq.com/openai/v1"
        mock_fb_client._custom_headers = None
        mock_fb_client.default_headers = None

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(mock_fb_client, "llama-3.3-70b-versatile"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert result["final_response"] == "Recovered via cross-provider fallback"
        # Exactly two failed attempts against the primary before the switch —
        # proves the transport-failure gate fired on the transport error
        # itself, not on a rate-limit/billing error reaching max_retries.
        assert calls == [
            ("zai", "glm-5.1"),
            ("zai", "glm-5.1"),
            ("groq", "llama-3.3-70b-versatile"),
        ]
        mock_resolve.assert_called_once()
        fb_provider_arg = mock_resolve.call_args.args[0] if mock_resolve.call_args.args else mock_resolve.call_args.kwargs.get("provider")
        assert fb_provider_arg == "groq"
        assert agent._fallback_activated is True
        assert agent.provider == "groq"
        assert agent.provider != "zai"
        assert agent.model == "llama-3.3-70b-versatile"
