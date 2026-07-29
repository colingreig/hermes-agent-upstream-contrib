#!/usr/bin/env python3
"""check_routing_sync.py — deterministic drift check between the inbound email
routing source-of-truth and the CF Worker's copy.

`~/.hermes/scripts/inbound_routing.json` is the source of truth; the CF Worker's
`inbound-worker/src/routing.js` is a hand-kept JS mirror. They drift silently when
a client is added to one and not the other — a mis-routed (or dropped) inbound
email. This compares the load-bearing fields (team_id, default_list_id,
allowed_sender_domains, domain_to_list) and reports drift. Exit 0 = in sync, 1 =
drift. Alerts to slack:hermes on drift (CHANGE-gated like the other watchers).
Wire as a no-agent cron. Zero LLM, zero network beyond `hermes send`.
"""
import json
import os
import re
import subprocess
import sys

JSON_PATH = os.path.expanduser("~/.hermes/scripts/inbound_routing.json")
JS_PATH = os.environ.get(
    "ROUTING_JS_PATH",
    os.path.expanduser("~/Projects/localhost/claude-dev-assistant/inbound-worker/src/routing.js"))
STATE_PATH = os.path.expanduser("~/.hermes/scripts/.routing_sync_state.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")


def _parse_js(text):
    out = {"team_id": None, "default_list_id": None,
           "allowed": set(), "domain_to_list": {}}
    m = re.search(r"team_id:\s*[\"'](\d+)[\"']", text)
    if m:
        out["team_id"] = m.group(1)
    m = re.search(r"default_list_id:\s*[\"'](\d+)[\"']", text)
    if m:
        out["default_list_id"] = m.group(1)
    m = re.search(r"allowed_sender_domains:\s*\[(.*?)\]", text, re.S)
    if m:
        out["allowed"] = set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))
    m = re.search(r"domain_to_list:\s*\{(.*?)\n\s*\},", text, re.S)
    if m:
        for dom, lid in re.findall(
                r"[\"']([^\"']+)[\"']\s*:\s*\{\s*list_id:\s*[\"'](\d+)[\"']", m.group(1)):
            out["domain_to_list"][dom] = lid
    return out


def _load_json():
    with open(JSON_PATH) as f:
        d = json.load(f)
    return {
        "team_id": str(d.get("team_id") or ""),
        "default_list_id": str(d.get("default_list_id") or ""),
        "allowed": set(d.get("allowed_sender_domains") or []),
        "domain_to_list": {k: str((v or {}).get("list_id") or "")
                           for k, v in (d.get("domain_to_list") or {}).items()},
    }


def _diff(src, js):
    issues = []
    for f in ("team_id", "default_list_id"):
        if src[f] != js[f]:
            issues.append(f"{f}: json={src[f]} != js={js[f]}")
    only_json = src["allowed"] - js["allowed"]
    only_js = js["allowed"] - src["allowed"]
    if only_json:
        issues.append(f"allowed_sender_domains only in JSON: {sorted(only_json)}")
    if only_js:
        issues.append(f"allowed_sender_domains only in JS: {sorted(only_js)}")
    sk, jk = set(src["domain_to_list"]), set(js["domain_to_list"])
    if sk - jk:
        issues.append(f"domain_to_list domains missing from JS: {sorted(sk - jk)}")
    if jk - sk:
        issues.append(f"domain_to_list domains missing from JSON: {sorted(jk - sk)}")
    for d in sk & jk:
        if src["domain_to_list"][d] != js["domain_to_list"][d]:
            issues.append(f"domain_to_list[{d}] list_id: json={src['domain_to_list'][d]} "
                          f"!= js={js['domain_to_list'][d]}")
    return issues


def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[routing-sync] DRY_RUN slack:\n{msg}")
        return
    try:
        subprocess.run([HERMES_BIN, "send", "--to", "slack:hermes", msg],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[routing-sync] slack send failed: {e!r}", file=sys.stderr)


def main():
    try:
        src = _load_json()
    except Exception as e:
        print(f"[routing-sync] cannot read {JSON_PATH}: {e!r}", file=sys.stderr)
        print(json.dumps({"error": "json"}))
        return 0
    try:
        with open(JS_PATH) as f:
            js = _parse_js(f.read())
    except Exception as e:
        print(f"[routing-sync] cannot read {JS_PATH}: {e!r}", file=sys.stderr)
        print(json.dumps({"error": "js"}))
        return 0

    issues = _diff(src, js)
    # CHANGE-gate the alert (don't re-spam an unchanged drift).
    prev = ""
    try:
        with open(STATE_PATH) as f:
            prev = json.load(f).get("signature", "")
    except Exception:
        pass
    sig = "\n".join(sorted(issues))
    if issues and sig != prev:
        _send_slack("🔀 *Inbound routing drift* (`check_routing_sync.py`) — the CF "
                    "Worker `routing.js` and `inbound_routing.json` disagree:\n  • "
                    + "\n  • ".join(issues)
                    + "\n_Re-sync routing.js (source of truth = the JSON) and redeploy the worker._")
    try:
        with open(STATE_PATH + ".tmp", "w") as f:
            json.dump({"signature": sig}, f)
        os.replace(STATE_PATH + ".tmp", STATE_PATH)
    except Exception:
        pass

    print(json.dumps({"in_sync": not issues, "issues": issues}, indent=2),
          file=sys.stderr)
    print(json.dumps({"in_sync": not issues, "issue_count": len(issues)}))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
