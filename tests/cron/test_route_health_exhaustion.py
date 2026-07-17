from __future__ import annotations

from types import SimpleNamespace

from cron.scheduler import _cron_routing_chain_exhausted_message


def test_cron_chain_exhaustion_forces_failure_message():
    agent = SimpleNamespace(
        _routing_chain_exhausted=True,
        _routing_chain_exhausted_reason="timeout",
    )

    message = _cron_routing_chain_exhausted_message(agent)

    assert message is not None
    assert "Routing chain exhausted (timeout)" in message
    assert "no cron objective was completed" in message


def test_cron_chain_not_exhausted_has_no_failure_message():
    agent = SimpleNamespace(_routing_chain_exhausted=False)

    assert _cron_routing_chain_exhausted_message(agent) is None
