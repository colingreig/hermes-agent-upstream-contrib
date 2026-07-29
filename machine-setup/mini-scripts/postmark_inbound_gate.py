#!/usr/bin/env python3
"""
Postmark inbound GATE — cheap, deterministic, zero-LLM.

Runs every 5 min as a --no-agent cron. Polls the Postmark *inbound* server
(Hermes Inbound, server 19552102) for newly received emails forwarded to
hermes@ignitemarketing.com, dedupes them against a processed-id ledger, and —
when there is new mail — writes a snapshot + wakes the `postmark-inbound-filer`
agent (`hermes cron run <id>`), which classifies each email and files a ClickUp
task in the right project. Empty inbox -> exit silently, $0 spent.

WHY A GATE (mirrors clickup_poll_gate.py): an LLM agent polling every 5 min just
to discover an empty inbox would burn ~288 runs/day. This no-agent script does
the cheap detection and only wakes the agent when Colin has actually forwarded
something.

State ledger: ~/.hermes/scripts/.postmark_inbound_state.json
  { "processed": {message_id: iso_ts},   # filed OK (filer moves ids here via
                                          #   postmark_mark_processed.py)
    "pending":   {message_id: iso_ts},   # handed to agent, awaiting confirm;
                                          #   re-presented after RETRY_AFTER
    "last_poll": iso_ts }

Snapshot handoff: ~/.hermes/scripts/postmark_inbound_snapshot.json
  { generated_at, count, emails: [ {message_id, from, from_name, to, subject,
    received_at, mailbox_hash, stripped_reply, text_body} ] }

Token: POSTMARK_HERMES_INBOUND_TOKEN (Doppler-injected into the gateway env;
read at runtime only via os.environ.get — never assigned as a file literal, per
the create_clickup_task.py truncation note).

Never raises out of main(): logs to stderr and returns 0 so a transient
Postmark/HTTP error never breaks the cron tick.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_PATH = os.path.join(SCRIPTS_DIR, ".postmark_inbound_state.json")
SNAPSHOT_PATH = os.path.join(SCRIPTS_DIR, "postmark_inbound_snapshot.json")
ROUTING_PATH = os.path.join(SCRIPTS_DIR, "inbound_routing.json")
JOBS_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")

# Security gate: only process mail whose From-domain is one of these (or a
# subdomain). Sourced from inbound_routing.json -> allowed_sender_domains;
# this is the hard-coded fallback if that file is missing/malformed.
DEFAULT_ALLOWED_DOMAINS = ["colingreig.com", "colingreig.ca", "ignitemarketing.com", "vercel.com", "github.com"]
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
FILER_JOB_NAME = "postmark-inbound-filer"

API_BASE = "https://api.postmarkapp.com"
POLL_COUNT = 50
RETRY_AFTER = 1200                 # re-present a pending id after 20 min unconfirmed
PROCESSED_TTL = 60 * 60 * 24 * 45  # prune processed ids older than Postmark's 45-day retention
HTTP_TIMEOUT = 30


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _iso_to_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _token():
    return os.environ.get("POSTMARK_HERMES_INBOUND_TOKEN", "").strip()


def _load_allowed_domains():
    try:
        with open(ROUTING_PATH) as f:
            doms = json.load(f).get("allowed_sender_domains")
        if isinstance(doms, list) and doms:
            return [d.strip().lower() for d in doms if d and d.strip()]
    except Exception as e:
        print(f"[gate] could not read allowed_sender_domains: {e}", file=sys.stderr)
    return list(DEFAULT_ALLOWED_DOMAINS)


def _email_domain(addr):
    """Pull the domain from 'Name <a@b.com>' or 'a@b.com'. Lowercased."""
    if not addr:
        return ""
    if "<" in addr and ">" in addr:
        addr = addr[addr.find("<") + 1:addr.find(">")]
    addr = addr.strip().strip("<>").lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def _sender_allowed(addr, allowed):
    dom = _email_domain(addr)
    if not dom:
        return False
    return any(dom == a or dom.endswith("." + a) for a in allowed)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("processed", {})
    d.setdefault("pending", {})
    d.setdefault("last_poll", None)
    return d


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _api_get(path, token):
    req = urllib.request.Request(API_BASE + path, headers={
        "Accept": "application/json",
        "X-Postmark-Server-Token": token,
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _find_filer_id():
    try:
        d = json.load(open(JOBS_PATH))
        for j in d.get("jobs", []):
            if j.get("name") == FILER_JOB_NAME:
                return j.get("id")
    except Exception as e:
        print(f"[gate] could not read jobs.json: {e}", file=sys.stderr)
    return None


def _wake_filer():
    fid = _find_filer_id()
    if not fid:
        print("[gate] filer job not found in jobs.json; cannot wake agent", file=sys.stderr)
        return False
    try:
        res = subprocess.run([HERMES_BIN, "cron", "run", fid],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            print(f"[gate] wake failed rc={res.returncode}: {res.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[gate] wake exception: {e}", file=sys.stderr)
        return False


def _prune_processed(state):
    now = time.time()
    state["processed"] = {
        mid: ts for mid, ts in state["processed"].items()
        if now - _iso_to_ts(ts) < PROCESSED_TTL
    }


def main():
    token = _token()
    if not token:
        print("[gate] POSTMARK_HERMES_INBOUND_TOKEN not set; skipping", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    allowed = _load_allowed_domains()
    state = _load_state()
    _prune_processed(state)
    processed = state["processed"]
    pending = state["pending"]
    now = time.time()
    skipped_blocked = 0

    try:
        listing = _api_get(f"/messages/inbound?count={POLL_COUNT}&offset=0", token)
    except Exception as e:
        print(f"[gate] inbound list failed: {e}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    msgs = listing.get("InboundMessages", []) or []
    new_emails = []
    for m in msgs:
        mid = m.get("MessageID")
        if not mid or mid in processed:
            continue
        if mid in pending and (now - _iso_to_ts(pending[mid])) < RETRY_AFTER:
            continue  # recently handed off; give the agent time to confirm
        # SECURITY GATE: only process mail from a whitelisted sender domain.
        sender = m.get("From") or ""
        if not _sender_allowed(sender, allowed):
            processed[mid] = _now_iso()   # mark handled (ignored) so it never re-presents
            pending.pop(mid, None)
            skipped_blocked += 1
            print(f"[gate] BLOCKED non-whitelisted sender {sender!r} (mid {mid}); ignored",
                  file=sys.stderr)
            continue
        try:
            det = _api_get(f"/messages/inbound/{mid}/details", token)
        except Exception as e:
            print(f"[gate] details failed for {mid}: {e}", file=sys.stderr)
            continue
        ff = det.get("FromFull") or {}
        new_emails.append({
            "message_id": mid,
            "from": det.get("From") or ff.get("Email") or m.get("From") or "",
            "from_name": ff.get("Name") or m.get("FromName") or "",
            "to": det.get("To") or m.get("To") or "",
            "subject": det.get("Subject") or m.get("Subject") or "(no subject)",
            "received_at": det.get("Date") or m.get("ReceivedAt") or "",
            "mailbox_hash": det.get("MailboxHash") or m.get("MailboxHash") or "",
            "stripped_reply": det.get("StrippedTextReply") or "",
            "text_body": det.get("TextBody") or "",
        })
        pending[mid] = _now_iso()

    state["last_poll"] = _now_iso()

    if not new_emails:
        _save_json(STATE_PATH, state)
        if skipped_blocked:
            print(f"[gate] {skipped_blocked} non-whitelisted email(s) ignored this poll",
                  file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    _save_json(SNAPSHOT_PATH, {
        "generated_at": _now_iso(),
        "count": len(new_emails),
        "emails": new_emails,
    })
    _save_json(STATE_PATH, state)

    woke = _wake_filer()
    subjects = ", ".join(e["subject"][:60] for e in new_emails[:5])
    print(f"Postmark inbound: {len(new_emails)} new email(s) queued for ClickUp "
          f"filing [{subjects}] — filer woke={woke}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[gate] unexpected: {e}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        sys.exit(0)
