from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "hermes_self_report_run.py"
_spec = importlib.util.spec_from_file_location("hermes_self_report_run_ut", SCRIPT)
run = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run
_spec.loader.exec_module(run)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["cmd"], returncode, stdout, stderr)


def test_runner_builds_sends_and_prints_one_line(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    builder = scripts / "hermes_report_build.py"
    sender = scripts / "postmark_send_report.py"
    builder.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    sender.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    monkeypatch.setattr(run, "HERMES", tmp_path)
    monkeypatch.setattr(run, "SCRIPTS", scripts)
    monkeypatch.setattr(run, "BUILDER", builder)
    monkeypatch.setattr(run, "SENDER", sender)

    calls = []

    def fake_run(cmd, *, timeout=120, cwd=None):
        calls.append(cmd)
        if str(cmd[1]).endswith("hermes_report_build.py"):
            # emulate builder argv writing outs
            out_html = Path(cmd[cmd.index("--out-html") + 1])
            out_text = Path(cmd[cmd.index("--out-text") + 1])
            out_subject = Path(cmd[cmd.index("--out-subject") + 1])
            out_html.write_text(
                "<html><body><div><div>body</div></div></body></html>",
                encoding="utf-8",
            )
            out_text.write_text(
                "HERMES · ALL CLEAR\nNo action needed — 2 completed this window.\n"
                "(Read-only status digest. It does not fix anything.)\n",
                encoding="utf-8",
            )
            out_subject.write_text("Hermes: all clear — 2 completed · $0.50", encoding="utf-8")
            return _completed(0, stdout=json.dumps({"work_completed": 2, "ready": 3}))
        if str(cmd[1]).endswith("postmark_send_report.py"):
            assert "Hermes: all clear — 2 completed · $0.50" in cmd
            return _completed(0, stdout='{"status":"sent"}')
        return _completed(0)

    monkeypatch.setattr(run, "_run", fake_run)
    monkeypatch.setattr(run, "_probe_findings", lambda: [("Skill size", "over budget")])

    code = run.build_and_send(to="colin@example.com", window_min=360, skip_probes=False, dry_run=False)
    assert code == 0
    assert any(str(c[1]).endswith("postmark_send_report.py") for c in calls)
    # probe section appended into text before send — verify via rebuilt outs in second call path
    # Re-run dry to inspect append helper directly
    text = tmp_path / "t.txt"
    html = tmp_path / "t.html"
    text.write_text("body\n(Read-only status digest. It does not fix anything.)\n", encoding="utf-8")
    html.write_text("<html><body><div><div>x</div></div></body></html>", encoding="utf-8")
    run._append_probe_section(text, html, [("Skill size", "over budget")])
    assert "PROBES" in text.read_text(encoding="utf-8")
    assert "Skill size" in text.read_text(encoding="utf-8")
    assert "Probes" in html.read_text(encoding="utf-8")


def test_runner_refuses_missing_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "BUILDER", tmp_path / "missing.py")
    monkeypatch.setattr(run, "SENDER", tmp_path / "sender.py")
    (tmp_path / "sender.py").write_text("x", encoding="utf-8")
    assert run.build_and_send(to="x", window_min=60, skip_probes=True, dry_run=True) == 2
