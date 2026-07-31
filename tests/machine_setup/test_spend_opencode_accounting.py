import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINI_SCRIPTS = ROOT / "machine-setup" / "mini-scripts"


def _load_script(name):
    if str(MINI_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(MINI_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, MINI_SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module: dataclasses using
    # ``from __future__ import annotations`` resolve stringized annotations
    # by looking up the defining module in sys.modules, which raises
    # AttributeError if the module isn't registered yet.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _write_opencode_log(log_dir, today, *events, stem="task"):
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stem}-{today}-120000.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return path


def _route(model, provider, base_url=""):
    spend_opencode = _load_script("spend_opencode")
    return spend_opencode.opencode_route_metadata_event(model, provider, base_url=base_url)


def _step(cost):
    return {"type": "step_finish", "part": {"type": "step-finish", "cost": cost, "tokens": {"input": 10, "output": 5}}}


def test_mini_scripts_import_and_run_without_yaml_dependency(tmp_path):
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import builtins\n"
        "_real_import = builtins.__import__\n"
        "def _blocked(name, *args, **kwargs):\n"
        "    if name == 'yaml' or name.startswith('yaml.'):\n"
        "        raise ModuleNotFoundError('No module named yaml')\n"
        "    return _real_import(name, *args, **kwargs)\n"
        "builtins.__import__ = _blocked\n",
        encoding="utf-8",
    )
    code = (
        "import spend_guard, spend_meter\n"
        "ev={'type':'step_finish','part':{'type':'step-finish','cost':1.25,'tokens':{}}}\n"
        "assert spend_guard.opencode_event_marginal_cost(ev, {'provider':'openai-codex','base_url':'http://127.0.0.1:8646/v1'}) == 0.0\n"
        "assert spend_meter.opencode_event_provider_cost(ev, {'provider':'anthropic'}) == ('anthropic', 1.25)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + str(MINI_SCRIPTS)

    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=20)

    assert result.returncode == 0, result.stderr


def test_guard_counts_codex_oauth_opencode_log_as_zero(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(
        log_dir,
        today,
        _route("openai/gpt-5.5", "openai-codex", base_url="http://127.0.0.1:8647/v1"),
        _step(49.50),
    )
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))

    assert spend_guard._opencode_log_spend(today) == 0.0
    assert spend_guard._opencode_log_spend_strict(today) == 0.0


def test_guard_preserves_genuinely_billed_opencode_cost(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(log_dir, today, _route("anthropic/claude-sonnet-5", "anthropic"), _step(1.25))
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))

    assert spend_guard._opencode_log_spend(today) == 1.25
    assert spend_guard._opencode_log_spend_strict(today) == 1.25


def test_guard_cap_ignores_subscription_covered_opencode_spend(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(
        log_dir,
        today,
        _route("openai/gpt-5.4-mini", "openai-codex", base_url="http://127.0.0.1:8647/v1"),
        _step(60.00),
    )
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))
    monkeypatch.setattr(spend_guard, "LAST_KNOWN_PATH", str(tmp_path / "state" / "last-known.json"))
    monkeypatch.setattr(spend_guard, "_state_db_spend_strict", lambda _epoch: 0.0)
    monkeypatch.delenv("HERMES_SPEND_GUARD_DISABLE", raising=False)

    assert spend_guard.is_over_cap(cap_usd=50.0, today_str=today) is False


def test_meter_includes_opencode_lane_with_subscription_aware_accounting(tmp_path, monkeypatch):
    spend_meter = _load_script("spend_meter")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(
        log_dir,
        today,
        _route("anthropic/claude-sonnet-5", "anthropic"),
        _step(2.50),
    )
    _write_opencode_log(
        log_dir,
        today,
        _route("openai/gpt-5.5", "openai-codex", base_url="http://127.0.0.1:8647/v1"),
        _step(40.00),
        stem="task2",
    )
    monkeypatch.setattr(spend_meter, "STATE_DB", str(tmp_path / "missing-state.db"))
    monkeypatch.setattr(spend_meter, "OC_LOG_DIR", str(log_dir))

    spend = spend_meter.per_provider_spend(today)

    assert spend["openai-codex"] == 0.0
    assert spend["anthropic"] == 2.50
    assert spend_meter.is_over_threshold(cap_usd=2.0, today_str=today) == [("anthropic", 2.50)]


def test_metadata_free_actual_step_finish_counts_without_route(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(log_dir, today, _step(3.75))
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert spend_guard._opencode_log_spend(today) == 3.75


def test_historical_metadata_free_log_uses_actual_configured_codex_proxy_8646(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    spend_opencode = _load_script("spend_opencode")
    opencode_exec = _load_script("opencode_exec")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(log_dir, today, _step(22.0))
    config_dir = tmp_path / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.jsonc").write_text(
        '{"provider":{"openai":{"options":{"baseURL":"http://127.0.0.1:8646/v1"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))
    monkeypatch.setenv("HOME", str(tmp_path))

    metadata = spend_opencode.configured_codex_oauth_proxy_metadata()
    assert metadata["base_url"] == "http://127.0.0.1:8646/v1"
    assert opencode_exec._billing_route_for_model("openai/gpt-5.5") == ("openai-codex", "http://127.0.0.1:8646/v1")
    assert spend_guard._opencode_log_spend(today) == 0.0


def test_unproven_direct_openai_opencode_cost_remains_billed(tmp_path, monkeypatch):
    spend_guard = _load_script("spend_guard")
    spend_opencode = _load_script("spend_opencode")
    opencode_exec = _load_script("opencode_exec")
    log_dir = tmp_path / "opencode"
    today = "20260727"
    _write_opencode_log(
        log_dir,
        today,
        _route("openai/gpt-5.5", "openai", base_url="https://api.openai.com/v1"),
        _step(8.25),
    )
    config_dir = tmp_path / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.jsonc").write_text(
        '{"provider":{"openai":{"options":{"baseURL":"https://api.openai.com/v1"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(spend_guard, "OC_LOG_DIR", str(log_dir))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert spend_opencode.configured_codex_oauth_proxy_metadata() is None
    assert opencode_exec._billing_route_for_model("openai/gpt-5.5") == ("openai", "")
    assert spend_guard._opencode_log_spend(today) == 8.25
