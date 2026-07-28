"""Verification-loop synthetic scaffolding must never reach durable session state.

verify_on_stop / pre_verify append a synthetic assistant "done" plus a synthetic
user nudge to keep the agent going one more turn before it can claim completion.
These messages exist only to drive the loop; persisting them poisons the resumed
transcript and breaks prompt-prefix cache reuse on later turns (#55733).

Both persistence sinks (SQLite flush + JSON snapshot) route through the single
``_is_ephemeral_scaffolding`` chokepoint, which is driven by
``_EPHEMERAL_SCAFFOLDING_FLAGS``. These tests assert that the verification-loop
flags are registered there and that both sinks drop the flagged messages while
keeping the real conversation.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest


def _is_purge_target(mod_name: str) -> bool:
    return (
        mod_name == "run_agent"
        or mod_name.startswith("agent.")
        or mod_name.startswith("tools.")
        or mod_name.startswith("hermes_")
    )


def _restore_purged_modules(saved_modules: dict) -> None:
    """Undo a sys.modules purge/reimport, restoring pre-purge identity.

    Also re-binds each restored dotted submodule as an attribute on its
    parent package object (e.g. ``agent.model_metadata`` on the ``agent``
    package). The purge only ever removes dotted submodule entries, never
    the parent package itself, so a fresh reimport rebinds the parent's
    attribute to the NEW submodule object. Restoring just the sys.modules
    dict entry leaves that attribute pointing at the new object — any test
    that captured the module via attribute access (``agent.model_metadata``)
    and later calls ``importlib.reload()`` on it then fails with
    "module ... not in sys.modules" because the reloaded object no longer
    matches sys.modules[name].
    """
    for mod_name in list(sys.modules):
        if _is_purge_target(mod_name):
            del sys.modules[mod_name]
    sys.modules.update(saved_modules)
    for name, mod in saved_modules.items():
        if "." not in name:
            continue
        parent_name, leaf = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, leaf, mod)


def _fresh_run_agent(request):
    """Reimport run_agent (and its agent./tools./hermes_* deps) from scratch.

    Snapshots the pre-purge module objects and restores them once the
    requesting test finishes, so this helper's forced reimport doesn't leak a
    fresh (and possibly differently-patched) copy of these modules into tests
    that run later in the same worker.
    """
    saved_modules = {k: v for k, v in sys.modules.items() if _is_purge_target(k)}
    request.addfinalizer(lambda: _restore_purged_modules(saved_modules))

    for mod in list(sys.modules):
        if _is_purge_target(mod):
            del sys.modules[mod]
    import run_agent  # noqa: F401
    return sys.modules["run_agent"]


def test_verification_flags_registered_as_ephemeral(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(request)

    assert "_verification_stop_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
    assert "_pre_verify_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS

    # The central classifier drives both persistence sinks.
    assert ra._is_ephemeral_scaffolding(
        {"role": "assistant", "content": "done", "_verification_stop_synthetic": True}
    )
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True}
    )
    # Real messages are not scaffolding.
    assert not ra._is_ephemeral_scaffolding({"role": "user", "content": "hi"})


def _make_agent(ra, session_id, tmp_path):
    agent = ra.AIAgent(
        session_id=session_id,
        api_key="test-key",
        base_url="http://127.0.0.1:8000/v1",
        provider="openai-compat",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "logs"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    return agent


def test_db_flush_drops_verification_scaffolding(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(request)
    agent = _make_agent(ra, "sess_db", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "premature done", "_verification_stop_synthetic": True},
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._flush_messages_to_session_db(messages, conversation_history=[])

    persisted = [
        kwargs.get("content")
        for _args, kwargs in agent._session_db.append_message.call_args_list
    ]
    assert "hi" in persisted
    assert "verified and clean" in persisted
    assert "premature done" not in persisted
    assert "[System: run tests]" not in persisted


def test_json_log_drops_verification_scaffolding(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(request)
    agent = _make_agent(ra, "sess_json", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "premature done", "_pre_verify_synthetic": True},
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._save_session_log(messages)

    log_file = agent.logs_dir / "session_sess_json.json"
    assert log_file.exists()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    contents = [m.get("content") for m in data["messages"]]
    assert contents == ["hi", "verified and clean"]
    assert all(not m.get("_pre_verify_synthetic") for m in data["messages"])
