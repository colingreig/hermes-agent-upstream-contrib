#!/usr/bin/env python3
"""
degraded_secrets_monitor.py — DEGRADED-SECRETS DETECTION (2026-07-04)

The gateway's secrets wrappers (gateway_secrets_wrap.sh, dashboard_secrets_wrap.sh,
mcp_secrets_wrap.sh) self-recover from a TRANSIENT 1Password outage: 12s-timeout +
5x retry, and on total failure they log `>>> FATAL: 1Password unreachable ...` and
exit 1 so launchd relaunches them. That's autonomous RECOVERY. This script is the
matching autonomous DETECTION: it alerts a human when recovery ISN'T actually
happening, i.e. either of:

  (a) FATAL-relaunch loop — the FATAL line appears >= FATAL_THRESHOLD times within
      the trailing FATAL_WINDOW_MIN minutes of gateway.error.log. That means 1Password
      has been down long enough that launchd's KeepAlive can't out-wait it.
  (b) Unresolved secret placeholder — the running gateway logged
      tools.mcp_tool._warn_unresolved_header_placeholders' "still contains the
      unresolved placeholder" warning for some MCP server/header, meaning a secret
      never resolved. The known agency-os / MCP_AGENCY_OS_API_KEY case is whitelisted
      (pre-existing until PR #66 deploys — see ClickUp 86e25xwwb lineage).

Alerts via Slack DM to Colin (`hermes send --to slack:D0BA2PM9CFM`, with `@UN4CQ1EGG`
mention) AND a ClickUp comment (same task-comment escalation convention as
verify-hermes-patches.sh §7c: CLICKUP_API_TOKEN only, no destructive write).
Dedup is signature-based (not calendar-day-based): an alert fires once per DISTINCT
degraded signature and stays quiet on repeat checks of the same signature; a recovery
clears the signature so a future recurrence alerts again. State:
~/.hermes/state/degraded-secrets-monitor.json.

Usage:
  degraded_secrets_monitor.py                    # check, print human summary, exit 1 if degraded
  degraded_secrets_monitor.py --json             # emit JSON result
  degraded_secrets_monitor.py --alert            # same + Slack/ClickUp alert on a NEW degraded signature
  degraded_secrets_monitor.py --log-file PATH    # check a fixture instead of the live log (testing)
  degraded_secrets_monitor.py --now ISO8601      # override "now" for deterministic window tests
  DRY_RUN=1 degraded_secrets_monitor.py --alert  # test alert path without posting anywhere

Exit codes: 0 = healthy, 1 = degraded (either condition).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

LOG_PATH = os.path.expanduser("~/.hermes/logs/gateway.error.log")
STATE_PATH = os.path.expanduser("~/.hermes/state/degraded-secrets-monitor.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
TOKEN_FILE = os.path.expanduser("~/.config/op-runtime-token")
ESCALATION_TASK_ID = os.environ.get("DEGRADED_SECRETS_ALERT_TASK_ID", "86e2610g8")
# Default to Colin's Slack DM; override via env if we ever want to revert to a channel target.
SLACK_TARGET = os.environ.get("DEGRADED_SECRETS_ALERT_SLACK", "slack:D0BA2PM9CFM")
SLACK_MENTION = "<@UN4CQ1EGG>"

FATAL_RE = re.compile(r'^(?P<ts>\S+) gateway_secrets_wrap: >>> FATAL: 1Password unreachable')
PLACEHOLDER_RE = re.compile(
    r"MCP server '(?P<server>[^']+)': header '(?P<header>[^']+)' still contains "
    r"the unresolved placeholder '\$\{(?P<var>[^}]+)\}'"
)
# Known pre-existing exception until PR #66 deploys (ClickUp 86e25xwwb lineage).
WHITELIST = {("agency-os", "MCP_AGENCY_OS_API_KEY")}

FATAL_THRESHOLD = 3
FATAL_WINDOW_MIN = 5
TAIL_LINES = 4000  # gateway.error.log isn't rotated hourly; a generous tail is cheap


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def read_tail(path, n=TAIL_LINES):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def check_fatal_loop(lines, now=None, threshold=FATAL_THRESHOLD, window_min=FATAL_WINDOW_MIN):
    now = now or _now()
    hits = []
    for line in lines:
        m = FATAL_RE.search(line)
        if not m:
            continue
        ts = _parse_ts(m.group("ts"))
        if ts and timedelta(0) <= (now - ts) <= timedelta(minutes=window_min):
            hits.append(ts.isoformat())
    return {"triggered": len(hits) >= threshold, "count": len(hits),
            "threshold": threshold, "window_min": window_min, "timestamps": hits}


def check_unresolved_placeholder(lines, whitelist=None):
    whitelist = WHITELIST if whitelist is None else whitelist
    seen, hits = set(), []
    for line in lines:
        m = PLACEHOLDER_RE.search(line)
        if not m:
            continue
        server, header, var = m.group("server"), m.group("header"), m.group("var")
        if (server, var) in whitelist:
            continue
        key = (server, var)
        if key in seen:
            continue
        seen.add(key)
        hits.append({"server": server, "header": header, "var": var})
    return {"triggered": bool(hits), "hits": hits}


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(obj):
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _signature(fatal, placeholder):
    # Lists, not tuples: a tuple survives in-process but round-trips through JSON
    # state as a list, so comparing a freshly-built tuple against a reloaded list
    # would always be unequal (tuple != list in Python) and dedup would never hold.
    return {"fatal": fatal["triggered"],
            "placeholder_keys": sorted([[h["server"], h["var"]] for h in placeholder["hits"]])}


def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[degraded-secrets-monitor] DRY_RUN slack:\n{msg}")
        return True
    try:
        r = subprocess.run([HERMES_BIN, "send", "--to", SLACK_TARGET, msg],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception as e:
        print(f"[degraded-secrets-monitor] slack send failed: {e!r}", file=sys.stderr)
        return False


def _op_read(ref):
    # Resolve a single op:// ref via the 1Password service-account SDK, NEVER the
    # `op` CLI. This script runs under system python3 (no `onepassword` SDK), so we
    # shell out to the gateway venv's python running op_sdk_resolve.resolve_refs().
    #
    # History (2026-07-05 op-cli-dialog-loop incident): the old body shelled out to
    # `op read` — and its FIRST attempt omitted OP_SERVICE_ACCOUNT_TOKEN, so on a box
    # with the 1Password desktop app running that call fell through to desktop-app CLI
    # integration and popped an OS "Allow / Don't Allow" consent dialog. Because this
    # monitor runs every 300s (+ RunAtLoad), it hammered that dialog in a loop after
    # every reboot. The SDK path talks to 1Password's API directly — no CLI, no daemon,
    # no desktop-app consent dialog, no hang.
    RESOLVER_PYTHON = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python")
    RESOLVER_DIR = os.path.expanduser("~/.hermes/scripts")
    if not os.path.isfile(TOKEN_FILE) or not os.path.exists(RESOLVER_PYTHON):
        return None
    try:
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from op_sdk_resolve import resolve_refs; "
            "v = resolve_refs([sys.argv[2]]); "
            "sys.stdout.write(v.get(sys.argv[2], ''))"
        )
        r = subprocess.run(
            [RESOLVER_PYTHON, "-c", code, RESOLVER_DIR, ref],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _post_clickup_comment(task_id, text):
    token = os.environ.get("CLICKUP_API_TOKEN") or _op_read("op://Dev Toolbox/dev/CLICKUP_API_TOKEN")
    if not token:
        print("[degraded-secrets-monitor] no CLICKUP_API_TOKEN available — skipping ClickUp escalation",
              file=sys.stderr)
        return False
    if os.environ.get("DRY_RUN"):
        print(f"[degraded-secrets-monitor] DRY_RUN clickup comment on {task_id}:\n{text}")
        return True
    try:
        body = json.dumps({"comment_text": text, "notify_all": False}).encode()
        req = urllib.request.Request(
            f"https://api.clickup.com/api/v2/task/{task_id}/comment",
            data=body, method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[degraded-secrets-monitor] clickup comment failed: {e!r}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-file", default=LOG_PATH, help="path to gateway error log (or a test fixture)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert", action="store_true", help="send Slack + ClickUp alert on a NEW degraded signature")
    ap.add_argument("--now", help="ISO8601 timestamp to use as 'now' (testing only)")
    args = ap.parse_args()

    now = _parse_ts(args.now) if args.now else _now()
    lines = read_tail(args.log_file)
    fatal = check_fatal_loop(lines, now=now)
    placeholder = check_unresolved_placeholder(lines)
    sig = _signature(fatal, placeholder)
    degraded = fatal["triggered"] or placeholder["triggered"]

    result = {"degraded": degraded, "fatal_loop": fatal, "placeholder": placeholder,
              "checked_at": now.isoformat()}

    if args.json:
        print(json.dumps(result, indent=2))
    elif not degraded:
        print("[degraded-secrets-monitor] healthy")
    else:
        if fatal["triggered"]:
            print(f"[degraded-secrets-monitor] FATAL-loop: {fatal['count']} hits in last {fatal['window_min']}min")
        for h in placeholder["hits"]:
            print(f"[degraded-secrets-monitor] unresolved placeholder: server={h['server']} var={h['var']}")

    if args.alert:
        state = _load_state()
        last_sig = state.get("last_alert_signature")
        if degraded and sig != last_sig:
            msg_lines = ["\U0001F6A8 Hermes degraded-secrets monitor"]
            if fatal["triggered"]:
                msg_lines.append(
                    f"- 1Password unreachable: {fatal['count']} FATAL relaunches in the last "
                    f"{fatal['window_min']} min (gateway can't recover on its own).")
            for h in placeholder["hits"]:
                msg_lines.append(
                    f"- Unresolved secret placeholder: MCP server '{h['server']}' header "
                    f"'{h['header']}' -> ${{{h['var']}}} never resolved. Set {h['var']} in "
                    f"1Password / ~/.hermes/.env and reconnect.")
            msg = "\n".join(msg_lines)
            slack_msg = "\n".join([SLACK_MENTION, *msg_lines])
            slack_ok = _send_slack(slack_msg)
            cu_ok = _post_clickup_comment(ESCALATION_TASK_ID, msg)
            state["last_alert_signature"] = sig
            state["last_alert_at"] = now.isoformat()
            _save_state(state)
            print(f"[degraded-secrets-monitor] alerted (slack={slack_ok} clickup={cu_ok})")
        elif not degraded and last_sig is not None:
            state["last_alert_signature"] = None
            state["recovered_at"] = now.isoformat()
            _save_state(state)
            print("[degraded-secrets-monitor] recovered — dedup state cleared")

    sys.exit(1 if degraded else 0)


if __name__ == "__main__":
    main()
