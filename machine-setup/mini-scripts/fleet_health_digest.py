#!/usr/bin/env python3
"""Compose and deliver the fleet digest without an agent or filesystem writes.

The only side effect is a Postmark request to the fixed email recipient, or the
fixed Slack DM fallback when Postmark is unavailable.  Recipient, sender,
fallback, and body provenance are constants rather than command-line inputs.
"""
from __future__ import annotations

import argparse
import datetime
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import hermes_report_build as builder
import postmark_send_report as sender

FIXED_RECIPIENT = "colin@colingreig.com"
FIXED_FALLBACK = "slack:D0BA2PM9CFM"
FIXED_FROM = sender.DEFAULT_FROM
TRUST_MANIFEST = Path(__file__).resolve().with_name("digest_trusted_scripts.json")


@dataclass(frozen=True)
class CheckResult:
    name: str
    returncode: int
    stdout: str
    stderr: str


def run_folded_checks() -> list[CheckResult]:
    """Run only manifest-hashed pure probe modes, under this trusted Python."""
    root = Path(__file__).resolve().parent
    manifest = json.loads(TRUST_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("interpreter_selector") != "release-python-pyyaml-onepassword-v1":
        raise RuntimeError("digest trust manifest has the wrong interpreter selector")
    results = []
    for filename, contract in manifest.get("scripts", {}).items():
        path = root / filename
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            results.append(CheckResult(filename, 126, "", "untrusted or missing check path"))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != contract.get("sha256"):
            results.append(CheckResult(filename, 126, "", "governed check hash mismatch"))
            continue
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(path), *contract.get("arguments", [])],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=120, check=False, cwd=str(root),
            )
            results.append(CheckResult(
                filename.removesuffix(".py").replace("_", "-"), completed.returncode,
                (completed.stdout or "").strip(), (completed.stderr or "").strip(),
            ))
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(CheckResult(filename, 125, "", str(exc)))
    return results


def render_check_findings(results: list[CheckResult]) -> tuple[str, str]:
    failed = [result for result in results if result.returncode != 0]
    if not failed:
        return "", ""
    text_lines = ["", "Consolidated health checks", "=========================="]
    html_lines = ["<h2>Consolidated health checks</h2>", "<ul>"]
    for result in failed:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part) or "no output"
        text_lines.extend([f"[{result.name}] exit={result.returncode}", detail, ""])
        html_lines.append(
            f"<li><strong>{html.escape(result.name)}</strong> exit={result.returncode}"
            f"<pre>{html.escape(detail)}</pre></li>"
        )
    html_lines.append("</ul>")
    return "\n".join(text_lines).rstrip() + "\n", "".join(html_lines)


def _delivery_args(subject: str) -> SimpleNamespace:
    return SimpleNamespace(
        to=FIXED_RECIPIENT,
        from_addr=FIXED_FROM,
        subject=subject,
        tag="hermes-self-report",
        fallback_to=FIXED_FALLBACK,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scheduled-slot",
        default=None,
        help="Nominal six-hour UTC schedule slot; retries reuse the original slot.",
    )
    args = parser.parse_args(argv)

    now = datetime.datetime.now(datetime.timezone.utc)
    scheduled_slot = args.scheduled_slot or now.replace(
        hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")
    report = builder.compose_report(scheduled_slot=scheduled_slot)
    text_body = report["text_body"]
    html_body = report["html_body"]
    try:
        check_text, check_html = render_check_findings(run_folded_checks())
    except Exception as exc:
        check_text, check_html = render_check_findings([
            CheckResult("governed-check-runner", 125, "", str(exc))
        ])
    text_body += check_text
    if html_body is not None:
        html_body += check_html
    delivery = _delivery_args(sender.flag_subject_for_alert(report["subject"], text_body, html_body))
    token = sender._resolve_token()
    postmark_detail = None
    if token:
        ok, message_id, postmark_detail = sender._send_postmark(
            token, delivery, text_body, html_body
        )
        if ok:
            print(json.dumps({"status": "sent", "channel": "postmark", "message_id": message_id}))
            return 0
    else:
        postmark_detail = "POSTMARK_SERVER_TOKEN unavailable"

    note = f"[Hermes report — email delivery failed ({postmark_detail}); sending via fallback channel]"
    ok, fallback_error = sender._send_hermes_fallback(
        FIXED_FALLBACK, delivery.subject, note, text_body
    )
    if ok:
        print(json.dumps({"status": "sent", "channel": f"hermes-send:{FIXED_FALLBACK}"}))
        return 0
    print(json.dumps({
        "status": "error",
        "channel": "none",
        "postmark_error": postmark_detail,
        "fallback_error": fallback_error,
    }))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
