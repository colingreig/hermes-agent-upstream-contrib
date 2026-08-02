#!/usr/bin/env python3
"""
degraded_secrets_monitor.py — DEGRADED-SECRETS DETECTION (2026-07-04)

The gateway's secrets wrappers (gateway_secrets_wrap.sh, dashboard_secrets_wrap.sh,
mcp_secrets_wrap.sh) self-recover from a TRANSIENT 1Password outage with bounded
retry. On terminal failure they log either the legacy
`>>> FATAL: 1Password unreachable ...` line or the classified
`FATAL classification=...` form. This script is the matching autonomous
DETECTION: it alerts a human when recovery ISN'T actually happening, i.e.
any of:

  (a) FATAL-relaunch loop — the FATAL line appears >= FATAL_THRESHOLD times within
      the trailing FATAL_WINDOW_MIN minutes of gateway.error.log. That means 1Password
      has been down long enough that launchd's KeepAlive can't out-wait it.
  (b) Unresolved secret placeholder — the running gateway logged
      tools.mcp_tool._warn_unresolved_header_placeholders' "still contains the
      unresolved placeholder" warning for some MCP server/header, meaning a secret
      never resolved. The known agency-os / MCP_AGENCY_OS_API_KEY case is whitelisted
      (pre-existing until PR #66 deploys — see ClickUp 86e25xwwb lineage).
  (c) Credential-pool outage — an auth.json credential_pool provider has no usable
      entries. Individual unavailable entries are reduced-redundancy diagnostics
      while another entry for the same provider remains usable.

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
  degraded_secrets_monitor.py --auth-file PATH   # check a fixture auth.json instead of runtime auth.json
  degraded_secrets_monitor.py --now ISO8601      # override "now" for deterministic window tests
  DRY_RUN=1 degraded_secrets_monitor.py --alert  # test alert path without posting anywhere

Exit codes: 0 = healthy, 1 = degraded (any condition).
"""
import argparse
import base64
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
TERMINAL_POOL_STATUSES = {"dead", "invalid", "error"}
EXHAUSTED_TTL_401_SECONDS = 5 * 60
EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60
NOUS_INFERENCE_INVOKE_SCOPE = "inference:invoke"
NOUS_INVOKE_JWT_MIN_TTL_SECONDS = 120
ESCALATION_TASK_ID = os.environ.get("DEGRADED_SECRETS_ALERT_TASK_ID", "86e2610g8")
# Default to Colin's Slack DM; override via env if we ever want to revert to a channel target.
SLACK_TARGET = os.environ.get("DEGRADED_SECRETS_ALERT_SLACK", "slack:D0BA2PM9CFM")
SLACK_MENTION = "<@UN4CQ1EGG>"

# Match the legacy wrapper line plus the source-controlled resolver/launcher
# classifications.  Deliberately stop at the classification token: callers
# retain only the parsed timestamp, never the rest of the log line where an
# SDK exception might contain sensitive material.
FATAL_RE = re.compile(
    r"^(?P<ts>\S+) gateway_secrets_wrap: "
    r"(?:>>> FATAL: 1Password unreachable"
    r"|FATAL classification=(?:transient[-_]exhausted|resolver|token-mint))\b"
)
PARKED_AUTH_RE = re.compile(
    r"^(?P<ts>\S+) gateway_secrets_wrap: FATAL "
    r"classification=(?:auth|permanent[-_]auth)\b"
)
PLACEHOLDER_RE = re.compile(
    r"MCP server '(?P<server>[^']+)': header '(?P<header>[^']+)' still contains "
    r"the unresolved placeholder '\$\{(?P<var>[^}]+)\}'"
)
# Known pre-existing exception until PR #66 deploys (ClickUp 86e25xwwb lineage).
WHITELIST = {("agency-os", "MCP_AGENCY_OS_API_KEY")}

FATAL_THRESHOLD = 3
FATAL_WINDOW_MIN = 5
TAIL_LINES = 4000  # gateway.error.log isn't rotated hourly; a generous tail is cheap


def default_auth_path():
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home() / "auth.json")
    except Exception:
        return os.path.expanduser(os.path.join(os.environ.get("HERMES_HOME", "~/.hermes"), "auth.json"))


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


def check_parked_auth(lines, now=None, window_min=FATAL_WINDOW_MIN):
    """A single recent permanent-auth record is degraded: launchd is parked."""
    now = now or _now()
    hits = []
    for line in lines:
        match = PARKED_AUTH_RE.search(line)
        if not match:
            continue
        timestamp = _parse_ts(match.group("ts"))
        if timestamp and timedelta(0) <= (now - timestamp) <= timedelta(minutes=window_min):
            hits.append(timestamp.isoformat())
    return {"triggered": bool(hits), "count": len(hits),
            "window_min": window_min, "timestamps": hits}


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


def _parse_absolute_timestamp(value):
    """Return an aware UTC datetime for epoch seconds/ms or ISO-8601 input."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric > 1_000_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return _parse_absolute_timestamp(float(raw))
        except ValueError:
            parsed = _parse_ts(raw)
            if parsed is None:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


def _exhausted_retry_at(entry):
    reset_at = _parse_absolute_timestamp(entry.get("last_error_reset_at"))
    if reset_at is not None:
        return reset_at
    status_at = _parse_absolute_timestamp(entry.get("last_status_at"))
    if status_at is None:
        return None
    ttl = (
        EXHAUSTED_TTL_401_SECONDS
        if entry.get("last_error_code") == 401
        else EXHAUSTED_TTL_DEFAULT_SECONDS
    )
    return status_at + timedelta(seconds=ttl)


def _scope_values(raw_scope):
    values = set()
    if isinstance(raw_scope, str):
        values.update(part for part in raw_scope.replace(",", " ").split() if part)
    elif isinstance(raw_scope, (list, tuple, set, frozenset)):
        for item in raw_scope:
            if isinstance(item, str):
                values.update(_scope_values(item))
    return values


def _decode_jwt_claims(token):
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _nous_jwt_is_usable(token, *, scope, expires_at, now):
    """Dependency-light parity with hermes_cli.auth._nous_invoke_jwt_is_usable."""
    claims = _decode_jwt_claims(token)
    if not claims:
        return False
    scopes = (
        _scope_values(scope)
        | _scope_values(claims.get("scope"))
        | _scope_values(claims.get("scp"))
    )
    if NOUS_INFERENCE_INVOKE_SCOPE not in scopes:
        return False
    threshold = now + timedelta(seconds=NOUS_INVOKE_JWT_MIN_TTL_SECONDS)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp) > threshold.timestamp()
    expiry = _parse_ts(expires_at) if isinstance(expires_at, str) else None
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc) > threshold


def _has_runtime_credential(provider, entry, now):
    if provider == "nous":
        return any(
            _nous_jwt_is_usable(
                entry.get(token_field),
                scope=entry.get("scope"),
                expires_at=entry.get(expiry_field),
                now=now,
            )
            for token_field, expiry_field in (
                ("agent_key", "agent_key_expires_at"),
                ("access_token", "expires_at"),
            )
        )
    access_token = entry.get("access_token")
    return isinstance(access_token, str) and bool(access_token.strip())


def classify_credential_pool(auth_payload, now):
    """Classify provider pools without retaining any credential material."""
    pool = auth_payload.get("credential_pool") if isinstance(auth_payload, dict) else None
    if not isinstance(pool, dict):
        return {
            "triggered": False,
            "status": "absent",
            "hits": [],
            "credential_pool_diagnostics": [],
        }

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    hits = []
    diagnostics = []
    for raw_provider, raw_entries in sorted(pool.items(), key=lambda item: str(item[0])):
        provider = str(raw_provider)
        entries = raw_entries if isinstance(raw_entries, list) else [None]
        if not entries:
            hits.append({"provider": provider, "id": "pool", "status": "empty"})
            continue

        unavailable_slots = []
        usable_count = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                unavailable_slots.append({
                    "id": f"index:{index}",
                    "status": "malformed",
                    "retry_at": None,
                })
                continue

            slot_id = str(entry.get("id") or f"index:{index}")
            status = str(entry.get("last_status") or "").strip().lower()
            retry_at = None
            unavailable_status = None
            if status in TERMINAL_POOL_STATUSES:
                unavailable_status = status
            elif status == "exhausted":
                retry_at = _exhausted_retry_at(entry)
                if retry_at is not None and retry_at > now:
                    unavailable_status = status

            if unavailable_status is None and not _has_runtime_credential(provider, entry, now):
                unavailable_status = "missing_credential"

            if unavailable_status is None:
                usable_count += 1
            else:
                unavailable_slots.append({
                    "id": slot_id,
                    "status": unavailable_status,
                    "retry_at": retry_at.isoformat() if retry_at is not None else None,
                })

        if usable_count == 0:
            hits.extend(
                {"provider": provider, "id": slot["id"], "status": slot["status"]}
                for slot in unavailable_slots
            )
        elif unavailable_slots:
            diagnostics.append({
                "provider": provider,
                "total_slot_count": len(entries),
                "usable_count": usable_count,
                "unavailable_count": len(unavailable_slots),
                "unavailable_slots": unavailable_slots,
            })

    hits.sort(key=lambda hit: (hit["provider"], hit["id"], hit["status"]))
    return {
        "triggered": bool(hits),
        "status": "ok",
        "hits": hits,
        "credential_pool_diagnostics": diagnostics,
    }


def check_credential_pool(auth_file, now=None):
    if not os.path.isfile(auth_file):
        return {"triggered": False, "status": "missing", "hits": [], "auth_file": auth_file,
                "credential_pool_diagnostics": []}
    try:
        with open(auth_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return {"triggered": False, "status": "malformed", "hits": [], "auth_file": auth_file,
                "error": exc.__class__.__name__, "credential_pool_diagnostics": []}

    result = classify_credential_pool(data, now or _now())
    result["auth_file"] = auth_file
    return result


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


def _signature(fatal, parked_auth, placeholder, credential_pool=None):
    credential_pool = credential_pool or {"hits": []}
    # Lists, not tuples: a tuple survives in-process but round-trips through JSON
    # state as a list, so comparing a freshly-built tuple against a reloaded list
    # would always be unequal (tuple != list in Python) and dedup would never hold.
    return {"fatal": fatal["triggered"],
            "parked_auth": parked_auth["triggered"],
            "placeholder_keys": sorted([[h["server"], h["var"]] for h in placeholder["hits"]]),
            "credential_pool": sorted(
                [[h["provider"], h["id"], h["status"]] for h in credential_pool["hits"]]
            )}


def _normalize_signature(sig):
    if not isinstance(sig, dict):
        return sig
    normalized = dict(sig)
    normalized.setdefault("credential_pool", [])
    return normalized


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
    if os.environ.get("DRY_RUN"):
        print(f"[degraded-secrets-monitor] DRY_RUN clickup comment on {task_id}:\n{text}")
        return True
    token = os.environ.get("CLICKUP_API_TOKEN") or _op_read("op://Dev Toolbox/dev/CLICKUP_API_TOKEN")
    if not token:
        print("[degraded-secrets-monitor] no CLICKUP_API_TOKEN available — skipping ClickUp escalation",
              file=sys.stderr)
        return False
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
    ap.add_argument("--auth-file", default=default_auth_path(), help="path to Hermes auth.json (or a test fixture)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert", action="store_true", help="send Slack + ClickUp alert on a NEW degraded signature")
    ap.add_argument("--now", help="ISO8601 timestamp to use as 'now' (testing only)")
    args = ap.parse_args()

    now = _parse_ts(args.now) if args.now else _now()
    lines = read_tail(args.log_file)
    fatal = check_fatal_loop(lines, now=now)
    parked_auth = check_parked_auth(lines, now=now)
    placeholder = check_unresolved_placeholder(lines)
    credential_pool = check_credential_pool(args.auth_file, now=now)
    credential_pool_diagnostics = credential_pool.get("credential_pool_diagnostics", [])
    sig = _signature(fatal, parked_auth, placeholder, credential_pool)
    degraded = (fatal["triggered"] or parked_auth["triggered"] or placeholder["triggered"]
                or credential_pool["triggered"])

    public_credential_pool = {
        key: value for key, value in credential_pool.items()
        if key != "credential_pool_diagnostics"
    }
    result = {"degraded": degraded, "fatal_loop": fatal, "parked_auth": parked_auth,
              "placeholder": placeholder, "credential_pool": public_credential_pool,
              "checked_at": now.isoformat()}
    if credential_pool_diagnostics:
        result["credential_pool_diagnostics"] = credential_pool_diagnostics

    if args.json:
        print(json.dumps(result, indent=2))
    elif not degraded:
        print("[degraded-secrets-monitor] healthy")
    else:
        if fatal["triggered"]:
            print(f"[degraded-secrets-monitor] FATAL-loop: {fatal['count']} hits in last {fatal['window_min']}min")
        if parked_auth["triggered"]:
            print("[degraded-secrets-monitor] gateway parked after permanent authentication failure")
        for h in placeholder["hits"]:
            print(f"[degraded-secrets-monitor] unresolved placeholder: server={h['server']} var={h['var']}")
        for h in credential_pool["hits"]:
            print(
                f"[degraded-secrets-monitor] credential pool degraded: "
                f"provider={h['provider']} id={h['id']} status={h['status']}"
            )

    if not args.json:
        for diagnostic in credential_pool_diagnostics:
            slots = ",".join(
                f"{slot['id']}:{slot['status']}"
                for slot in diagnostic["unavailable_slots"]
            )
            print(
                "[degraded-secrets-monitor] credential pool reduced redundancy: "
                f"provider={diagnostic['provider']} "
                f"usable={diagnostic['usable_count']}/{diagnostic['total_slot_count']} "
                f"unavailable={slots}"
            )

    if args.alert:
        state = _load_state()
        last_sig = _normalize_signature(state.get("last_alert_signature"))
        if degraded and sig != last_sig:
            msg_lines = ["\U0001F6A8 Hermes degraded-secrets monitor"]
            if fatal["triggered"]:
                msg_lines.append(
                    f"- 1Password unreachable: {fatal['count']} FATAL relaunches in the last "
                    f"{fatal['window_min']} min (gateway can't recover on its own).")
            if parked_auth["triggered"]:
                msg_lines.append(
                    "- Gateway launch is parked after a permanent authentication failure; "
                    "repair credentials, then bootstrap the LaunchAgent.")
            for h in placeholder["hits"]:
                msg_lines.append(
                    f"- Unresolved secret placeholder: MCP server '{h['server']}' header "
                    f"'{h['header']}' -> ${{{h['var']}}} never resolved. Set {h['var']} in "
                    f"1Password / ~/.hermes/.env and reconnect.")
            for h in credential_pool["hits"]:
                msg_lines.append(
                    f"- Credential pool degraded: provider '{h['provider']}' entry "
                    f"'{h['id']}' last_status={h['status']}. Repair or refresh the stored credential."
                )
            msg = "\n".join(msg_lines)
            slack_msg = "\n".join([SLACK_MENTION, *msg_lines])
            pending_sig = _normalize_signature(state.get("pending_alert_signature"))
            deliveries = (
                dict(state.get("pending_deliveries") or {})
                if pending_sig == sig
                else {}
            )
            slack_ok = bool(deliveries.get("slack"))
            cu_ok = bool(deliveries.get("clickup"))
            if not slack_ok:
                slack_ok = _send_slack(slack_msg)
            if not cu_ok:
                cu_ok = _post_clickup_comment(ESCALATION_TASK_ID, msg)
            # Persist success independently per destination. If Slack succeeds
            # while ClickUp is down, the next tick retries only ClickUp instead
            # of paging Slack every five minutes.
            deliveries.update({"slack": slack_ok, "clickup": cu_ok})
            if slack_ok and cu_ok:
                state["last_alert_signature"] = sig
                state["last_alert_at"] = now.isoformat()
                state.pop("pending_alert_signature", None)
                state.pop("pending_deliveries", None)
                _save_state(state)
                print(f"[degraded-secrets-monitor] alerted (slack={slack_ok} clickup={cu_ok})")
            else:
                state["pending_alert_signature"] = sig
                state["pending_deliveries"] = deliveries
                _save_state(state)
                print(
                    f"[degraded-secrets-monitor] alert delivery incomplete "
                    f"(slack={slack_ok} clickup={cu_ok}); dedupe state NOT advanced",
                    file=sys.stderr,
                )
        elif not degraded and last_sig is not None:
            state["last_alert_signature"] = None
            state.pop("pending_alert_signature", None)
            state.pop("pending_deliveries", None)
            state["recovered_at"] = now.isoformat()
            _save_state(state)
            print("[degraded-secrets-monitor] recovered — dedup state cleared")
        elif not degraded and state.get("pending_alert_signature") is not None:
            state.pop("pending_alert_signature", None)
            state.pop("pending_deliveries", None)
            _save_state(state)

    sys.exit(1 if degraded else 0)


if __name__ == "__main__":
    main()
