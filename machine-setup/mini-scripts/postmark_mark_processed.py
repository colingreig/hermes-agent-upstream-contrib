#!/usr/bin/env python3
"""
postmark_mark_processed.py — move a Postmark inbound MessageID from `pending`
to `processed` in the gate ledger, AFTER the postmark-inbound-filer agent has
successfully filed the ClickUp task for it. Atomic + idempotent.

The filer MUST call this for every email it files, otherwise the gate will keep
re-presenting that email (after RETRY_AFTER) and a duplicate task could be made.

Usage:
  python3 ~/.hermes/scripts/postmark_mark_processed.py <MessageID> [<MessageID> ...]
"""
import json
import os
import sys
from datetime import datetime, timezone

STATE_PATH = os.path.expanduser("~/.hermes/scripts/.postmark_inbound_state.json")


def main(argv):
    if not argv:
        print("usage: postmark_mark_processed.py <MessageID> [...]", file=sys.stderr)
        return 2
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("processed", {})
    state.setdefault("pending", {})
    ts = datetime.now(timezone.utc).isoformat()
    for mid in argv:
        state["processed"][mid] = ts
        state["pending"].pop(mid, None)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)
    print(f"marked processed: {', '.join(argv)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
