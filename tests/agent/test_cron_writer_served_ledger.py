"""Regression guard for 86e2ky2e9 — the writer-served ledger wasn't wired to
the ClickUp executor cron path.

``~/.hermes/logs/writer-served.jsonl`` was, until now, only written by the
opencode writer/content delegate (``opencode_exec.py``'s ``_record_served``).
Any cron job that instead runs the normal in-process agent conversation loop
— which includes the ClickUp executor lane — served calls that showed up in
``agent.log`` but never produced a ledger row. These tests pin:

1. ``_record_cron_served_ledger`` writes a row shaped so
   ``hermes_report_build.py``'s tolerant ``.get()``-based parser can read it
   (naming provider/model, per the task's acceptance criterion).
2. Subscription-included cost is stamped with ``billing_mode`` the same way
   the state.db write next to it already does.
3. MoA advisor cost is folded into the recorded ``cost_usd``.
4. A ledger-write failure is fail-open — it must never raise into the turn.
"""

import json
from types import SimpleNamespace

import pytest

from agent import conversation_loop


def _cost_result(amount_usd, status="billed", source="pricing_table"):
    return SimpleNamespace(amount_usd=amount_usd, status=status, source=source)


def _agent(session_id="cron_62714b869845_20260803_010500", platform="cron"):
    return SimpleNamespace(session_id=session_id, platform=platform)


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
