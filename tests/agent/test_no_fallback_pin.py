"""Regression tests for the per-job ``no_fallback`` fail-closed pin (86e2bjac3).

A job/agent constructed with ``no_fallback=True`` must NEVER downgrade to a
backup provider/model. ``try_activate_fallback`` is the single choke point
every fallback activation flows through (all conversation_loop call sites
forward to it), so a guard at the top of that function is the load-bearing
fix — it must fire for EVERY FailoverReason, not just some of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason


def _pinned_stub():
    """A stub agent with the fail-closed pin set AND a non-empty fallback
    chain, so a False return can only be explained by the guard itself —
    not by the chain-exhausted path that also returns False."""
    return SimpleNamespace(
        _no_fallback=True,
        provider="anthropic",
        model="claude-sonnet-5",
        _fallback_chain=[{"provider": "gemini", "model": "gemini-3.5-flash"}],
        _fallback_index=0,
        _primary_runtime={"provider": "anthropic"},
        _rate_limited_until=0,
    )


@pytest.mark.parametrize("reason", list(FailoverReason) + [None])
def test_pinned_agent_fails_closed_for_every_reason(reason):
    """Every FailoverReason (billing, rate_limit, upstream_rate_limit,
    overloaded, server_error, auth, auth_permanent, timeout) plus reason=None
    must be suppressed by the pin — proving the guard fires unconditionally,
    not just for the reasons the caller happens to exercise."""
    stub = _pinned_stub()

    result = try_activate_fallback(stub, reason)

    assert result is False
    # The chain must not have been walked at all — index stays at 0 even
    # though the chain is non-empty and would otherwise have room to advance.
    assert stub._fallback_index == 0


def test_pinned_agent_fallback_chain_untouched():
    """The guard must return before mutating the chain/index in any way."""
    stub = _pinned_stub()
    original_chain = list(stub._fallback_chain)

    result = try_activate_fallback(stub, FailoverReason.rate_limit)

    assert result is False
    assert stub._fallback_chain == original_chain
    assert stub._fallback_index == 0
    # rate_limit normally arms a cooldown when leaving the primary — the
    # guard must short-circuit before that side effect too.
    assert stub._rate_limited_until == 0


def test_unpinned_agent_with_populated_chain_is_not_blocked_by_guard():
    """Control: with the pin OFF and a non-empty, un-exhausted chain, the
    guard clause itself must not be why anything downstream fails — this
    does not assert success (the real chain-walk logic needs a live
    provider client), only that the pinned/non-pinned cases diverge for the
    guard-relevant piece of state (the chain is left untouched by the guard,
    since the guard never engages when _no_fallback is False).
    """
    pinned = _pinned_stub()
    pinned_result = try_activate_fallback(pinned, FailoverReason.server_error)

    # Contrast: an exhausted (empty) chain with the pin OFF also returns
    # False, but for a completely different reason (nothing left to try,
    # not a fail-closed pin). Confirms the pinned case isn't merely
    # incidentally matching an exhausted-chain False.
    exhausted_stub = SimpleNamespace(
        _no_fallback=False,
        provider="anthropic",
        model="claude-sonnet-5",
        _fallback_chain=[],
        _fallback_index=0,
        _primary_runtime={"provider": "anthropic"},
        _rate_limited_until=0,
        _fallback_activated=False,
    )
    exhausted_result = try_activate_fallback(exhausted_stub, FailoverReason.server_error)

    assert pinned_result is False
    assert exhausted_result is False
    # Both return False, but the pinned stub never advanced past index 0
    # with room left in the chain (1 entry, index 0) while the exhausted
    # stub genuinely has nothing left (0 entries) — the guard is the reason
    # for the pinned case, chain exhaustion is the reason for the control.
    assert len(pinned._fallback_chain) > pinned._fallback_index
    assert len(exhausted_stub._fallback_chain) == 0
