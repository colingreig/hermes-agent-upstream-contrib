#!/usr/bin/env python3
"""
postmark_send_report.py — send a plain-text/HTML status email via Postmark,
with a deterministic `hermes send` fallback and a delivery receipt.

Built for the hermes-self-report cron (6-hourly Hermes status digest to Colin),
but generic enough for any script/skill that needs to send a simple transactional
email through the account's existing Postmark server.

Reuses the SAME server token ignite-workbench uses to send from
sales@ignitemarketing.com (see lib/notify/postmark.ts in that repo) — no new
credentials. Requires POSTMARK_SERVER_TOKEN, resolved via
`agent.lazy_secret_resolver` (1Password-backed, mirrors the mini's
op_sdk_resolve.py) or, failing that, the environment.

RC4 postmortem (2026-07-23 -> 2026-07-26, ~3 days of zero status emails):
  (a) `agent.lazy_secret_resolver` (and the `onepassword` SDK it needs) is only
      importable under `~/.hermes/runtime-current/venv/bin/python`. A cron-driven
      LLM invoking this script has repeatedly used the system `python3` instead
      of the documented `cd $HERMES/runtime-current && ./venv/bin/python ...`
      shell incantation, which silently breaks the import (caught by a bare
      `except Exception`) and falls through to an empty os.environ — see
      ~/.hermes/logs/errors.log 2026-07-23 22:56:34. Since then, the cron's
      LLM never even re-attempted the send (zero "postmark" mentions anywhere
      in agent.log/errors.log for 07-24 through 07-26): the failure was not
      just wrong-interpreter, it was "gave up silently."
  (b) The documented "if Postmark fails, fall back to `hermes send`" instruction
      lived ONLY as SKILL.md prose for the cron's LLM to notice and act on. A
      cheap cron model (gpt-5.4-mini) never reliably executes that multi-step
      recipe under time/turn budget, so the fallback never fired in practice.
  (c) The cron job's own success/failure tracking only reflects whether the
      LLM turn completed without raising — NOT whether an email or fallback
      message actually reached Colin. `last_status` stayed "ok" through the
      entire outage.

Fix: this script is now the single source of truth for delivery, not the LLM's
judgment.
  - It re-execs itself under the venv python if invoked with the wrong
    interpreter, so token resolution no longer depends on the caller getting a
    multi-part shell command exactly right.
  - On any Postmark failure (missing token, HTTP error, network error), it
    automatically shells out to the standalone `hermes send` CLI (no LLM, no
    agent loop) as a deterministic fallback — this is CODE, not a suggestion in
    a skill file.
  - It writes a receipt to ~/.hermes/logs/hermes-self-report-last-send.json
    (channel + status + short error text, never the token) so an external
    probe can detect "nothing was delivered" even if the calling agent doesn't.
  - It exits non-zero whenever NOTHING was delivered (both Postmark and the
    hermes-send fallback failed), so the caller/probe can treat that as a real
    failure instead of quietly reporting a healthy tick.

Usage:
  python3 ~/.hermes/scripts/postmark_send_report.py \
    --to colin@colingreig.com \
    --subject "Hermes status — 2026-07-01 12:00" \
    --body-file /path/to/body.txt
  # or pipe the body on stdin:
  echo "body text" | python3 ~/.hermes/scripts/postmark_send_report.py --to ... --subject ...

Exit codes: 0 delivered (Postmark or the hermes-send fallback), 1 nothing
delivered, 2 usage error.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

DEFAULT_FROM = "Hermes <sales@ignitemarketing.com>"
# Bare "slack" (home channel) is NOT usable as a fallback target: this mini has
# no SLACK_HOME_CHANNEL configured (verified 2026-07-26 — `hermes send --to
# slack` fails with "No home channel set for slack"). Use Colin's actual DM
# channel from `hermes send --list` instead. If this ID ever rotates, `hermes
# send --list` on the mini will show the current one.
DEFAULT_FALLBACK_TARGET = "slack:D0BA2PM9CFM"

HERMES = os.path.expanduser("~/.hermes")
VENV_PYTHON = os.path.join(HERMES, "runtime-current", "venv", "bin", "python")
HERMES_CLI = os.path.expanduser("~/.local/bin/hermes")
RECEIPT_PATH = os.path.join(HERMES, "logs", "hermes-self-report-last-send.json")
REEXEC_GUARD_ENV = "POSTMARK_SEND_REPORT_REEXEC"

FALLBACK_BODY_MAX_CHARS = 3500


def _maybe_reexec_under_venv():
    """Make correct-interpreter usage independent of how the caller invokes us.

    `from agent import lazy_secret_resolver` (and transitively the
    `onepassword` SDK) is only importable under the hermes-agent release venv.
    If we're not already running under it and it exists, re-exec ourselves
    under it exactly once (guarded by an env var so a broken/looping venv
    python can't recurse forever)."""
    if os.environ.get(REEXEC_GUARD_ENV) == "1":
        return
    try:
        already_correct = os.path.realpath(sys.executable) == os.path.realpath(VENV_PYTHON)
    except OSError:
        already_correct = False
    if already_correct or not os.path.exists(VENV_PYTHON):
        return
    env = dict(os.environ)
    env[REEXEC_GUARD_ENV] = "1"
    try:
        os.execve(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:], env)
    except OSError:
        pass  # fall through and try with whatever interpreter we already have


def _resolve_token():
    token = None
    try:
        from agent import lazy_secret_resolver
        token = lazy_secret_resolver.get("POSTMARK_SERVER_TOKEN")
    except Exception:
        token = None
    if not token:
        token = os.environ.get("POSTMARK_SERVER_TOKEN")
    return token


def _write_receipt(channel, status, detail):
    """Best-effort delivery receipt for an external probe. Never includes the
    token or any other secret — channel names, status words, and short
    diagnostic text only."""
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "channel": channel,  # "postmark" | "hermes-send:<target>" | "none"
        "status": status,  # "sent" | "failed"
        "detail": (detail or "")[:500],
    }
    try:
        os.makedirs(os.path.dirname(RECEIPT_PATH), exist_ok=True)
        with open(RECEIPT_PATH, "w") as f:
            json.dump(rec, f)
    except Exception:
        pass


def _send_postmark(token, args, text_body, html_body):
    payload = {
        "From": args.from_addr,
        "To": args.to,
        "Subject": args.subject,
        "TextBody": text_body,
        "MessageStream": "outbound",
        "Tag": args.tag,
    }
    if html_body is not None:
        payload["HtmlBody"] = html_body

    req = urllib.request.Request(
        "https://api.postmarkapp.com/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return True, result.get("MessageID"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return False, None, f"http {e.code}: {detail[:300]}"
    except Exception as e:
        return False, None, str(e)


def _send_hermes_fallback(target, subject, note, text_body):
    """Deterministic code fallback: the standalone `hermes send` CLI needs no
    LLM/agent loop, so it works even when the cron agent never gets around to
    the retry-then-fallback prose in SKILL.md."""
    if not os.path.exists(HERMES_CLI):
        return False, f"hermes CLI not found at {HERMES_CLI}"

    body = text_body
    if len(body) > FALLBACK_BODY_MAX_CHARS:
        body = body[:FALLBACK_BODY_MAX_CHARS] + f"\n\n...[truncated {len(text_body) - FALLBACK_BODY_MAX_CHARS} chars; full report only reached the send step, not your inbox — see mini logs]"
    message = f"{note}\n\n{body}"

    try:
        proc = subprocess.run(
            [HERMES_CLI, "send", "--to", target, "-s", subject, "-q", "--json"],
            input=message,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {(proc.stderr or proc.stdout or '')[:300]}"
    return True, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body-file", help="Path to a text file with the plain-text email body. Omit to read stdin.")
    p.add_argument("--html-file", help="Optional path to an HTML body. When given, the email is "
                                       "multipart (HtmlBody + TextBody); the text body is the plain-text fallback.")
    p.add_argument("--from-addr", default=DEFAULT_FROM)
    p.add_argument("--tag", default="hermes-self-report")
    p.add_argument("--fallback-to", default=DEFAULT_FALLBACK_TARGET,
                    help="hermes-send target used ONLY if Postmark delivery fails entirely "
                         "(default: %(default)s home channel).")
    p.add_argument("--no-fallback", action="store_true",
                    help="Disable the hermes-send fallback (Postmark-only, for testing).")
    args = p.parse_args()

    if args.body_file:
        with open(args.body_file, "r") as f:
            text_body = f.read()
    else:
        text_body = sys.stdin.read()

    html_body = None
    if args.html_file:
        with open(args.html_file, "r") as f:
            html_body = f.read()

    token = _resolve_token()
    postmark_ok = False
    message_id = None
    detail = None

    if not token:
        detail = "POSTMARK_SERVER_TOKEN not set (checked agent.lazy_secret_resolver and os.environ)"
        print(f"ERROR: {detail}", file=sys.stderr)
    else:
        postmark_ok, message_id, detail = _send_postmark(token, args, text_body, html_body)
        if not postmark_ok:
            print(json.dumps({"status": "error", "channel": "postmark", "detail": detail}), file=sys.stderr)

    if postmark_ok:
        print(json.dumps({"status": "sent", "channel": "postmark", "message_id": message_id}))
        print(f"DELIVERED via postmark (message_id={message_id})", file=sys.stderr)
        _write_receipt("postmark", "sent", f"message_id={message_id}")
        return 0

    # Postmark failed (or token missing) — fire the fallback in code, not prose.
    if args.no_fallback:
        _write_receipt("none", "failed", detail or "postmark failed, --no-fallback set")
        print("ERROR: postmark delivery failed and --no-fallback is set; nothing delivered", file=sys.stderr)
        return 1

    note = f"[Hermes report — email delivery failed ({detail}); sending via fallback channel]"
    fallback_ok, fallback_err = _send_hermes_fallback(args.fallback_to, args.subject, note, text_body)

    if fallback_ok:
        print(json.dumps({"status": "sent", "channel": f"hermes-send:{args.fallback_to}", "postmark_error": detail}))
        print(f"DELIVERED via hermes-send fallback (target={args.fallback_to}); postmark failed: {detail}",
              file=sys.stderr)
        _write_receipt(f"hermes-send:{args.fallback_to}", "sent", f"postmark_error={detail}")
        return 0

    print(json.dumps({
        "status": "error", "channel": "none",
        "postmark_error": detail, "fallback_error": fallback_err,
    }), file=sys.stderr)
    print("ERROR: ALL delivery channels failed (postmark AND hermes-send fallback) — nothing delivered to Colin",
          file=sys.stderr)
    _write_receipt("none", "failed", f"postmark_error={detail}; fallback_error={fallback_err}")
    return 1


if __name__ == "__main__":
    _maybe_reexec_under_venv()
    sys.exit(main())
