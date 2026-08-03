from agent import shell_hooks


def _result(**overrides):
    value = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "error": None,
    }
    value.update(overrides)
    return value


def test_mandatory_pre_tool_hook_fails_closed_on_spawn_error(monkeypatch):
    spec = shell_hooks.ShellHookSpec("pre_tool_call", "/missing", mandatory=True)
    monkeypatch.setattr(shell_hooks, "_spawn", lambda *_: _result(error="command not found"))
    decision = shell_hooks._make_callback(spec)(tool_name="terminal")
    assert decision is not None and decision["action"] == "block"


def test_mandatory_pre_tool_hook_fails_closed_on_timeout(monkeypatch):
    spec = shell_hooks.ShellHookSpec("pre_tool_call", "/slow", mandatory=True)
    monkeypatch.setattr(shell_hooks, "_spawn", lambda *_: _result(timed_out=True))
    decision = shell_hooks._make_callback(spec)(tool_name="terminal")
    assert decision is not None and decision["action"] == "block"


def test_optional_hook_preserves_fail_open_semantics(monkeypatch):
    spec = shell_hooks.ShellHookSpec("pre_tool_call", "/missing")
    monkeypatch.setattr(shell_hooks, "_spawn", lambda *_: _result(error="command not found"))
    assert shell_hooks._make_callback(spec)(tool_name="terminal") is None