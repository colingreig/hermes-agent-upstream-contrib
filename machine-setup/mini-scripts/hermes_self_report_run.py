#!/usr/bin/env python3
"""Deterministic Hermes status digest: build → probe → send. No LLM.

fleet-health-digest previously ran as an agent cron. Cheap models freestyled
scoreboards (all zeros) and inventing "MODEL & SPEND" from `hermes auth list`
instead of sending hermes_report_build.py output. This script is the job.

Exit codes:
  0  email delivered (Postmark or hermes-send fallback)
  1  build or send failed
  2  usage / environment error
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


HERMES = Path(os.path.expanduser("~/.hermes"))
SCRIPTS = HERMES / "scripts"
BUILDER = SCRIPTS / "hermes_report_build.py"
SENDER = SCRIPTS / "postmark_send_report.py"
DEFAULT_TO = "colin@colingreig.com"


def _python() -> str:
    """Prefer the Hermes release venv so lazy_secret_resolver / ClickUp token work."""
    venv_python = HERMES / "runtime-current" / "venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


PROBE_COMMANDS = (
    ("Prior delivery", [_python(), str(SCRIPTS / "hermes_self_report_delivery_probe.py")]),
    ("Skill size", [_python(), str(SCRIPTS / "hermes_validate_size_monitor.py")]),
    ("Model deprecation", ["bash", str(SCRIPTS / "ignite-model-deprecation-check.sh")]),
    ("Supabase RLS", [_python(), str(SCRIPTS / "supabase_rls_guard.py")]),
    ("Usage alerts", [_python(), str(SCRIPTS / "hermes_usage_alert.py")]),
)


def _now_local() -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        now = datetime.datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M %Z")


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(HERMES),
    )


def _probe_findings() -> list[tuple[str, str]]:
    """Return (label, detail) for probes that did not exit cleanly."""
    findings: list[tuple[str, str]] = []
    for label, cmd in PROBE_COMMANDS:
        path = Path(cmd[-1])
        if not path.exists():
            findings.append((label, f"probe script missing: {path}"))
            continue
        try:
            result = _run(cmd, timeout=180)
        except subprocess.TimeoutExpired:
            findings.append((label, "probe timed out"))
            continue
        except OSError as exc:
            findings.append((label, f"probe failed to start: {exc}"))
            continue
        if result.returncode == 0:
            continue
        tail = (result.stdout or result.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {result.returncode}"
        findings.append((label, detail[:300]))
    return findings


def _append_probe_section(text_path: Path, html_path: Path, findings: list[tuple[str, str]]) -> None:
    if not findings:
        return
    text = text_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    text_block = ["", "PROBES", *[f"  · {label}: {detail}" for label, detail in findings]]
    html_items = "".join(
        f'<div style="font-size:13px;color:#374151;margin:4px 0">'
        f"<b>{_esc(label)}</b> — {_esc(detail)}</div>"
        for label, detail in findings
    )
    html_block = (
        '<h2 style="margin:28px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.04em;'
        'text-transform:uppercase;color:#6b7280">Probes</h2>'
        f"{html_items}"
    )
    # Insert probe section before the footer / closing body tags when possible.
    marker = "(Read-only status digest"
    if marker in text:
        text = text.replace(marker, "\n".join(text_block) + "\n\n" + marker, 1)
    else:
        text = text.rstrip() + "\n" + "\n".join(text_block) + "\n"
    close = "</div></div></body></html>"
    if close in html:
        html = html.replace(close, html_block + close, 1)
    else:
        html = html.rstrip() + "\n" + html_block + "\n"
    text_path.write_text(text, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")


def _esc(value: str) -> str:
    import html as html_mod

    return html_mod.escape(str(value))


def build_and_send(*, to: str, window_min: int, skip_probes: bool, dry_run: bool) -> int:
    if not BUILDER.is_file():
        print(f"missing builder: {BUILDER}", file=sys.stderr)
        return 2
    if not SENDER.is_file():
        print(f"missing sender: {SENDER}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="hermes-self-report-") as tmp:
        tmp_path = Path(tmp)
        header_path = tmp_path / "header.json"
        out_html = tmp_path / "report.html"
        out_text = tmp_path / "report.txt"
        out_subject = tmp_path / "subject.txt"
        header = {
            "when": _now_local(),
            # Builder overwrites work_stoppage deterministically; leave blank.
            "work_stoppage": "",
            "needs_attention": "",
            "health": "",
            "model": "",
            "auth": "",
        }
        header_path.write_text(json.dumps(header), encoding="utf-8")

        build = _run(
            [
                _python(),
                str(BUILDER),
                "--window-min",
                str(window_min),
                "--header-file",
                str(header_path),
                "--out-html",
                str(out_html),
                "--out-text",
                str(out_text),
                "--out-subject",
                str(out_subject),
            ],
            timeout=300,
        )
        if build.returncode != 0:
            print(build.stderr or build.stdout or "hermes_report_build failed", file=sys.stderr)
            return 1
        summary: dict = {}
        payload = (build.stdout or "").strip()
        start = payload.find("{")
        end = payload.rfind("}")
        if start >= 0 and end > start:
            try:
                summary = json.loads(payload[start : end + 1])
            except json.JSONDecodeError:
                summary = {}

        if not skip_probes:
            _append_probe_section(out_text, out_html, _probe_findings())

        subject = out_subject.read_text(encoding="utf-8").strip() or f"Hermes status — {_now_local()}"
        if dry_run:
            print(subject)
            print(out_text.read_text(encoding="utf-8")[:2000])
            print(json.dumps({"dry_run": True, "summary": summary}, indent=2))
            return 0

        send = _run(
            [
                _python(),
                str(SENDER),
                "--to",
                to,
                "--subject",
                subject,
                "--html-file",
                str(out_html),
                "--body-file",
                str(out_text),
            ],
            timeout=120,
        )
        # Local deliver gets this one-liner (not a freestyled scoreboard).
        if send.returncode == 0:
            print(f"Digest sent: {subject}")
            return 0
        detail = (send.stderr or send.stdout or "send failed").strip()
        print(f"Digest send failed: {detail[-500:]}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default=DEFAULT_TO)
    parser.add_argument("--window-min", type=int, default=360)
    parser.add_argument("--skip-probes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return build_and_send(
            to=args.to,
            window_min=args.window_min,
            skip_probes=args.skip_probes,
            dry_run=args.dry_run,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"timed out: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
