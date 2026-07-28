from __future__ import annotations

import importlib.util
import json
import py_compile
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINI_SCRIPTS = ROOT / "machine-setup" / "mini-scripts"
MANIFEST = MINI_SCRIPTS / "writer-chain.json"
VERIFIER = MINI_SCRIPTS / "verify-writer-chain.py"
SDK_RESOLVER = MINI_SCRIPTS / "op_sdk_resolve.py"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location("verify_writer_chain_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_writer_chain_manifest_json_contract():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert payload["primary"] == {"provider": "openai-codex", "model": "openai/gpt-5.5", "rationale": payload["primary"]["rationale"]}
    assert payload["cascade_source"]["assert"] == 'WRITER_CASCADE[0] == ("openai/gpt-5.5", "openai-codex")'
    assert "op_sdk_resolve.py" in payload["coupled_points"]["flag"]["note"]
    assert "through the SDK resolver" in payload["coupled_points"]["flag"]["note"]


def test_writer_chain_python_sources_compile(tmp_path):
    py_compile.compile(str(VERIFIER), doraise=True)
    py_compile.compile(str(SDK_RESOLVER), doraise=True)


def test_flag_check_uses_adjacent_sdk_resolver_without_op_cli(tmp_path, monkeypatch, capsys):
    scripts_dir = tmp_path / ".hermes" / "scripts"
    scripts_dir.mkdir(parents=True)
    verifier_copy = scripts_dir / "verify-writer-chain.py"
    shutil.copy2(VERIFIER, verifier_copy)
    (scripts_dir / "op_sdk_resolve.py").write_text(
        "def resolve_refs(refs):\n"
        "    assert refs == ['op://Dev Toolbox/dev/HERMES_WRITER_CODEX']\n"
        "    return {refs[0]: '1'}\n",
        encoding="utf-8",
    )

    def fail_on_op(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if command and command[0] == "op":
            raise AssertionError("op CLI must not be invoked")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fail_on_op)
    module = _load_module(verifier_copy)

    result = module.check_flag(json.loads(MANIFEST.read_text(encoding="utf-8")))

    assert result["status"] == "pass"
    output = capsys.readouterr().out
    assert "SDK resolver" in output
    assert "HERMES_WRITER_CODEX=1" not in output
