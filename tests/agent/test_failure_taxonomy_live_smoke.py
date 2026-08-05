"""Real-path smoke coverage for provider failure taxonomy.

The upstream is a loopback HTTP server, never a production account.  Requests
flow through the real OpenAI SDK exception adapter, Hermes's classifier and
credential-recovery helper; Codex usage flows through the production /usage
adapter.  Only Slack delivery and credential storage are replaced at the final
side-effect boundaries.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest

from agent import account_usage, ops_alerts
from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.error_classifier import FailoverReason, classify_api_error
from agent.failure_taxonomy import (
    FAILURE_KIND_RATE_LIMIT_SESSION,
    FAILURE_KIND_USAGE_CAP,
)


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    mode = "weekly"
    usage_percent = 87

    def log_message(self, format, *args):
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            if self.mode == "weekly":
                message = "You have reached your weekly limit. Resets in 40 hours."
            else:
                message = "You've hit your usage limit for this session. Try again in 45 minutes."
            self._json(429, {"error": {"message": message, "type": "rate_limit_error"}})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        if self.path == "/v1/api/codex/usage":
            self._json(
                200,
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {"used_percent": self.usage_percent},
                        "secondary_window": {"used_percent": 12},
                    },
                    "credits": {"has_credits": False},
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})


@contextmanager
def _fake_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _real_429(base_url: str, mode: str):
    _FakeUpstreamHandler.mode = mode
    client = openai.OpenAI(api_key="fake-local-token", base_url=f"{base_url}/v1", max_retries=0)
    with pytest.raises(openai.RateLimitError) as caught:
        client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "smoke"}],
        )
    client.close()
    return caught.value


def _recovery_agent():
    pool = MagicMock()
    pool.provider = "openai-codex"
    pool.current.return_value = SimpleNamespace(last_status=None)
    pool.mark_exhausted_and_rotate.return_value = None
    agent = SimpleNamespace(
        provider="openai-codex",
        api_key="fake-local-token",
        _credential_pool=pool,
        _swap_credential=MagicMock(),
    )
    return agent, pool


def test_fake_429s_reach_distinct_failure_alert_kinds():
    """Weekly caps and short session throttles must never collapse together."""
    ops_alerts.reset_for_tests()
    with _fake_upstream() as (_server, base_url), patch.object(ops_alerts, "_send_slack") as sender:
        observed = []
        for mode, expected_reason, expected_kind, retried in (
            ("weekly", FailoverReason.usage_cap, FAILURE_KIND_USAGE_CAP, False),
            ("session", FailoverReason.rate_limit, FAILURE_KIND_RATE_LIMIT_SESSION, True),
        ):
            classified = classify_api_error(
                _real_429(base_url, mode),
                provider="openai-codex",
                model="gpt-test",
            )
            assert classified.reason == expected_reason

            agent, pool = _recovery_agent()
            recover_with_credential_pool(
                agent,
                status_code=429,
                has_retried_429=retried,
                classified_reason=classified.reason,
                error_context=classified.error_context,
            )
            persisted_kind = pool.mark_exhausted_and_rotate.call_args.kwargs["failure_kind"]
            assert persisted_kind == expected_kind

            observed.append(persisted_kind)

        assert observed == [FAILURE_KIND_USAGE_CAP, FAILURE_KIND_RATE_LIMIT_SESSION]
        assert [call.kwargs["signature"] for call in sender.call_args_list] == [
            "provider_failure:openai-codex:usage_cap",
            "provider_failure:openai-codex:rate_limit_session",
        ]


def test_fake_usage_at_87_percent_emits_one_headroom_warning():
    """A real /usage fetch may warn once, but must not force account exhaustion."""
    ops_alerts.reset_for_tests()
    with _fake_upstream() as (_server, base_url), patch.object(ops_alerts, "_send_slack") as sender:
        snapshot = account_usage.fetch_account_usage(
            "openai-codex",
            base_url=f"{base_url}/v1",
            api_key="fake-local-token",
        )
        assert snapshot is not None
        session = next(window for window in snapshot.windows if window.label == "Session")
        assert session.used_percent == 87
        assert session.used_percent < 100

        sender.assert_called_once()
        assert sender.call_args.kwargs["signature"] == "usage_headroom:openai-codex:session:85"
