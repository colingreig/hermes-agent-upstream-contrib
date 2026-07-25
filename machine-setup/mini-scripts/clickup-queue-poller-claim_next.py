#!/usr/bin/env python3
"""claim_next.py — STEP ZERO first-claim scan WITH atomic per-task claim (N>=2 safe).

Replaces the bare first_claim_scan.py for concurrent executors. It scans the
queue snapshot for claimable `to do` + `agent-ready` tasks (same filter as
first_claim_check.py), then walks them in priority order and ATOMICALLY ACQUIRES
the claim (via ~/.hermes/scripts/claim_store.py) on the first one no live peer
already holds. The task it prints is therefore already LOCKED by this executor —
a concurrent executor running the same scan will skip it and lock a different one.

This makes divergence a CODE guarantee, not an LLM-compliance hope. At N=1 it is
a near no-op: the single executor locks candidates[0] and works it.

Outputs (for the skill to consume):
  - stdout: "FIRST-CLAIM CANDIDATES: <n>", the top-5 list, then on success a
    ---JSON-CANDIDATE-0--- block with the LOCKED candidate plus its claim run id.
  - /tmp/first_claim_candidate.json : the locked candidate object (+ _claim_run)
  - /tmp/first_claim_run.txt        : the claim run id (for release/heartbeat)
Exit 0 always (0 candidates is a valid "queue-emptied for me" outcome).

On disqualify: the skill MUST `claim_store.py release <id> --run <run>` and re-run
this script (it will then lock the next free candidate). On closeout: release.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
try:
    import claim_store
except Exception:  # fail-open: behave like the old scan (no locking)
    claim_store = None

SNAP = os.path.expanduser("~/.hermes/scripts/queue_snapshot.json")
PRI = {"urgent": 0, "high": 1, "normal": 2, "low": 3, None: 4}

# Disqualify-cooldown (86e260vnw): when the STEP ZERO disqualifier flow parks a
# task on an operator/product decision, it tags it "blocked-operator-<unix-ts>"
# (see SKILL.md). Encoding the timestamp IN the tag name keeps the cooldown
# fully ClickUp-native (portable across executor/Hermes sessions on any
# machine) with zero extra API calls for the common case — the expiry is
# parseable straight out of the already-fetched snapshot. Without this, the
# executor re-picked the same operator-blocked task 3x/hour with near-identical
# "requires operator scoping decision" comments (86e1uxqkn, 2026-07-04).
_BLOCKED_OPERATOR_RE = re.compile(r"^blocked-operator-(\d+)$")
COOLDOWN_SECONDS = 24 * 60 * 60


def _tags(t):
    out = []
    for tg in (t.get("tags") or []):
        if isinstance(tg, dict):
            out.append(tg.get("name"))
        elif isinstance(tg, str):
            out.append(tg)
    return [x for x in out if x]


def _blocked_operator_ts(tags):
    """Return the embedded unix timestamp if a blocked-operator-<ts> tag is
    present, else None."""
    for tg in tags:
        m = _BLOCKED_OPERATOR_RE.match(tg or "")
        if m:
            return int(m.group(1))
    return None


def _has_newer_human_comment(task_id, since_ts):
    """True if the task's most recent comment is newer than since_ts and does
    NOT look like an automated ignite-/hermes- disqualify comment — i.e. a
    human (Colin) responded, which should clear the cooldown immediately
    rather than waiting out the full 24h. Fails CLOSED (still cooled down) on
    any error — a live-check failure must never cause an early re-pick."""
    try:
        r = subprocess.run(
            ["node", os.path.expanduser("~/.hermes/scripts/clickup/clickup.mjs"),
             "comments", task_id, "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        comments = json.loads(r.stdout)
        if not comments:
            return False
        latest = comments[-1]
        latest_ts = int(latest.get("date") or 0) // 1000 if int(latest.get("date") or 0) > 10**12 else int(latest.get("date") or 0)
        text = (latest.get("comment_text") or latest.get("text") or "")
        looks_automated = text.strip().lower().startswith("ignite-")
        return latest_ts > since_ts and not looks_automated
    except Exception:
        return False


def _age(x):
    try:
        return int(x.get("date_created"))
    except (TypeError, ValueError):
        return 9_999_999_999_999


def _run_id():
    # Unique per executor invocation. No Math.random/uuid dependency assumptions:
    # pid + monotonic-ish ts is unique enough across concurrent gateway threads
    # (distinct call times) and processes (distinct pids).
    return os.environ.get("HERMES_EXECUTOR_RUN_ID") or f"r{os.getpid()}-{int(time.time()*1000)}"


def main():
    # Self-heal (2026-06-24): sweep dead/stale claim files before scanning so a
    # crashed same-host peer's claim (or any claim past TTL) can never shadow its
    # task from the candidate list. is_claimed() already treats such claims as
    # free; this additionally unlinks the orphan files so the claims dir stays
    # clean. Fail-open — a reap error must never block claiming work.
    if claim_store is not None:
        try:
            reaped = claim_store.reap_stale()
            if reaped:
                print(f"  [claim_next] reaped {len(reaped)} dead/stale claim(s): {', '.join(reaped)}")
        except Exception as e:
            print(f"  [claim_next] reap skipped: {e}")

    if not os.path.exists(SNAP):
        print("snapshot not found:", SNAP)
        return 2
    d = json.load(open(SNAP))
    now = int(time.time())
    candidates = [
        t for t in d.get("tasks", [])
        if t.get("status") == "to do"
        and "agent-ready" in _tags(t)
        and "agent-avoid" not in _tags(t)
        and "agent-fenced" not in _tags(t)
    ]
    claimable = []
    cooling_down = []
    merged_lifecycle = []
    for t in candidates:
        tags = _tags(t)
        if "merged" in tags:
            # A merged task must be reconciled by the merged-before-claim
            # handler; it is never safe to send it through fresh execution.
            merged_lifecycle.append(t.get("id"))
            continue
        blocked_ts = _blocked_operator_ts(tags)
        if blocked_ts is None:
            claimable.append(t)
            continue
        if now - blocked_ts >= COOLDOWN_SECONDS:
            claimable.append(t)  # cooldown expired — claimable again
            continue
        if _has_newer_human_comment(t.get("id"), blocked_ts):
            claimable.append(t)  # Colin responded — cooldown cleared early
            continue
        cooling_down.append(t.get("id"))
    if cooling_down:
        print(f"  [claim_next] {len(cooling_down)} task(s) skipped — operator-disqualify cooldown active: "
              f"{', '.join(cooling_down)}")
    if merged_lifecycle:
        print(f"  [claim_next] {len(merged_lifecycle)} task(s) skipped — merged lifecycle requires writeback: "
              f"{', '.join(merged_lifecycle)}")
    claimable.sort(key=lambda x: (PRI.get(x.get("priority"), 4), _age(x)))
    print("FIRST-CLAIM CANDIDATES:", len(claimable))
    for t in claimable[:5]:
        print("  ", t.get("id"), "|", t.get("priority"), "|", t.get("date_created"),
              "|", (t.get("name") or "")[:60], "| tags:", _tags(t))

    run = _run_id()
    chosen = None
    skipped = []
    for t in claimable:
        tid = t.get("id")
        if claim_store is None:
            chosen = t  # fail-open: no locking available, behave like old scan
            break
        if claim_store.is_claimed(tid):
            skipped.append(tid)
            continue
        if claim_store.acquire(tid, run):
            chosen = t
            break
        skipped.append(tid)  # lost an acquire race — try next

    if skipped:
        print(f"  [claim_next] skipped {len(skipped)} already-claimed-by-peer: {', '.join(skipped)}")

    if chosen is None:
        # Either no claimable tasks, or every claimable one is locked by a peer.
        print("  [claim_next] no free claimable candidate for this executor"
              if claimable else "  [claim_next] queue-emptied")
        return 0

    chosen = dict(chosen)
    chosen["_claim_run"] = run
    with open("/tmp/first_claim_candidate.json", "w") as f:
        f.write(json.dumps(chosen))
    with open("/tmp/first_claim_run.txt", "w") as f:
        f.write(run)
    print(f"  [claim_next] LOCKED {chosen.get('id')} run={run}")
    print("\n---JSON-CANDIDATE-0---")
    print(json.dumps(chosen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
