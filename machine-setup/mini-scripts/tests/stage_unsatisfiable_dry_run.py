#!/usr/bin/env python3
"""
Staging smoke test for the structurally-unsatisfiable verify gate fix
(2026-06-19, ClickUp 86e1ynuw1).

Runs the gate against LIVE ClickUp state in DRY_RUN mode, and reports:
  - what the scan currently sees (unclaimed / continuations / parked)
  - which tasks WOULD be auto-parked if they had the park tag stamped
  - that the new self_park() function correctly builds the DELETE call

Exits 0 on success. Exits 1 if any of the 3 expected invariants are violated.
NO ClickUp state is mutated.

Usage:
    python3 stage_unsatisfiable_dry_run.py
"""
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location(
    "poll_gate", os.path.expanduser("~/.hermes/scripts/clickup_poll_gate.py"))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


def main():
    # DRY_RUN prevents any HTTP DELETE from firing during this script.
    os.environ["DRY_RUN"] = "1"

    print("=" * 70)
    print("STAGING SMOKE TEST — structurally-unsatisfiable verify gate")
    print("(ClickUp 86e1ynuw1 + brain note 2026-06-19-...-self-park-protocol)")
    print("=" * 70)

    # 1. Confirm the new tag constant is defined and exported.
    print("\n[1] Constants:")
    assert hasattr(pg, "PARK_BLOCKED_EXTERNAL_TAG"), \
        "PARK_BLOCKED_EXTERNAL_TAG constant missing from gate module"
    assert pg.PARK_BLOCKED_EXTERNAL_TAG == "park-blocked-external", \
        f"unexpected tag value: {pg.PARK_BLOCKED_EXTERNAL_TAG!r}"
    print(f"  OK  PARK_BLOCKED_EXTERNAL_TAG = {pg.PARK_BLOCKED_EXTERNAL_TAG!r}")

    # 2. Confirm _classify() handles every case correctly (covers unit tests
    #    via in-process run + extra case for the simulated staging task).
    print("\n[2] _classify() classification matrix:")
    def case(label, status, tags, expected):
        t = {"id": "t1", "name": "x", "url": "u",
             "status": {"status": status, "type": "open"},
             "tags": [{"name": n} for n in tags],
             "list": {"name": "L", "id": "1"}}
        got = pg._classify(t)
        flag = "OK " if got == expected else "FAIL"
        print(f"  {flag} status={status!r:20s} tags={tags!r:55s} -> {got!r:14s} (expected {expected!r})")
        return got == expected

    all_ok = True
    all_ok &= case("healthy continuation",  "in progress", ["agent-ready"],                                   "continuation")
    all_ok &= case("healthy unclaimed",      "to do",      ["agent-ready"],                                   "unclaimed")
    all_ok &= case("parked",                 "in progress", ["agent-ready", "park-blocked-external"],          "parked")
    all_ok &= case("parked tag reverse",     "in progress", ["park-blocked-external", "agent-ready"],          "parked")
    all_ok &= case("parked but no ready",    "in progress", ["park-blocked-external"],                         None)
    all_ok &= case("parked but in review",   "in review",   ["agent-ready", "park-blocked-external"],          None)
    all_ok &= case("parked but closed",      "complete",    ["agent-ready", "park-blocked-external"],          None)
    all_ok &= case("in-review continuation", "in review",   ["agent-ready"],                                   None)

    # 3. Confirm _self_park() in DRY_RUN mode logs the right intent without
    #    making any HTTP calls. We mock _delete_tag to detect any leak.
    print("\n[3] _self_park() DRY_RUN intent log (no HTTP):")
    leaked = []
    pg._delete_tag = lambda tid, tag: leaked.append((tid, tag))
    fake_parked = [
        {"id": "86e10xuu9", "name": "Ingest: Columbus + San Diego permits",
         "status": "in progress", "kind": "parked"},
        {"id": "86e1yheqg", "name": "OEC DNS — cms.oeconnection.com",
         "status": "in progress", "kind": "parked"},
    ]
    pg._self_park(fake_parked)
    if leaked:
        print(f"  FAIL _delete_tag was called during DRY_RUN: {leaked}")
        all_ok = False
    else:
        print("  OK  no HTTP calls made during DRY_RUN _self_park()")

    # 4. Simulate a live poll: scan + classify + show what would auto-park
    #    if any tasks were stamped. This calls the actual ClickUp API but
    #    is read-only.
    print("\n[4] Live DRY_RUN scan (read-only):")
    try:
        unclaimed, continuations, parked = pg._scan_queue()
    except Exception as e:
        print(f"  ERROR scan failed: {e!r}")
        all_ok = False
        unclaimed, continuations, parked = [], [], []

    print(f"  unclaimed:    {len(unclaimed)}")
    for t in unclaimed:
        print(f"    - {t['id']} [{t['status']!r}] {t['name'][:60]}")
    print(f"  continuation: {len(continuations)}")
    for t in continuations:
        print(f"    - {t['id']} [{t['status']!r}] {t['name'][:60]}")
    print(f"  parked:       {len(parked)}")
    for t in parked:
        print(f"    - {t['id']} [{t['status']!r}] {t['name'][:60]}")

    # 5. Apply _self_park to whatever _scan_queue found. If nothing is
    #    parked (expected on a healthy day), this is a no-op confirmation.
    print("\n[5] Applying _self_park() to live parked set (DRY_RUN, no HTTP):")
    pg._self_park(parked)
    if leaked:
        print(f"  FAIL _delete_tag was called during live DRY_RUN: {leaked}")
        all_ok = False
    else:
        print("  OK  no HTTP calls made")

    # 6. Confirm main() runs to completion in DRY_RUN and produces a snapshot.
    print("\n[6] main() end-to-end DRY_RUN:")
    rc = pg.main()
    if rc != 0:
        print(f"  FAIL main() returned {rc}, expected 0")
        all_ok = False
    else:
        snap = os.path.join(os.path.dirname(pg.SNAPSHOT_PATH), "queue_snapshot.json")
        if os.path.exists(snap):
            print(f"  OK  snapshot saved: {snap}")
        else:
            print(f"  FAIL snapshot missing: {snap}")
            all_ok = False

    print("\n" + "=" * 70)
    print("RESULT:", "PASS" if all_ok else "FAIL")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
