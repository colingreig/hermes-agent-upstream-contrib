#!/usr/bin/env python3
"""validator_autonomy.py — shadow-to-live graduation ledger for per-tier autonomy.

"Full autonomy ASAP, but earned" (Colin, 2026-06-18). While the validator runs in
shadow it records, per risk tier, whether each verdict MATCHED the eventual
operator decision (PASS→merged / BLOCK→closed). A tier becomes *eligible* to
graduate after N consecutive matches. Flipping the actual flag
(HERMES_AUTONOMOUS_MERGE_<TIER> in Doppler) stays a MANUAL operator step — this
ledger only reports eligibility; it never enables autonomy itself. High tier is
manual-forever by default (threshold null) until Colin opts in.

Ledger: ~/.hermes/scripts/.autonomy_ledger.json
CLI:
  validator_autonomy.py reconcile --repo R --pr N   # compare verdict to PR outcome, record
  validator_autonomy.py record --tier T --matched true|false
  validator_autonomy.py status                       # per-tier counts + eligibility
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import validator_verdict

LEDGER_PATH = os.path.expanduser("~/.hermes/scripts/.autonomy_ledger.json")
DEFAULT_THRESHOLDS = {"low": 20, "medium": 40, "high": None}  # null = manual only
MAX_EVENTS = 200


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load():
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("thresholds", dict(DEFAULT_THRESHOLDS))
            d.setdefault("tiers", {})
            d.setdefault("events", [])
            return d
    except Exception:
        pass
    return {"thresholds": dict(DEFAULT_THRESHOLDS), "tiers": {}, "events": []}


def _save(d):
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, LEDGER_PATH)


def record(tier, matched, note=""):
    d = _load()
    t = d["tiers"].setdefault(tier, {"consecutive_matches": 0, "total": 0, "mismatches": 0})
    t["total"] += 1
    if matched:
        t["consecutive_matches"] += 1
    else:
        t["consecutive_matches"] = 0
        t["mismatches"] += 1
    t["last"] = _now_iso()
    d["events"].append({"ts": _now_iso(), "tier": tier, "matched": bool(matched), "note": note})
    d["events"] = d["events"][-MAX_EVENTS:]
    _save(d)
    return t


def _eligibility(d):
    out = {}
    for tier, thr in d["thresholds"].items():
        t = d["tiers"].get(tier, {})
        cm = t.get("consecutive_matches", 0)
        if thr is None:
            out[tier] = {"consecutive": cm, "threshold": None, "eligible": False,
                         "reason": "manual-only (operator opt-in required)"}
        else:
            out[tier] = {"consecutive": cm, "threshold": thr, "eligible": cm >= thr,
                         "reason": (f"eligible: set HERMES_AUTONOMOUS_MERGE_{tier.upper()}=1"
                                    if cm >= thr else f"{cm}/{thr} consecutive matches")}
    return out


def cmd_reconcile(a):
    v = validator_verdict.verdict_for(a.repo, a.pr)
    if not v:
        print(f"no verdict for {a.repo}#{a.pr}; nothing to reconcile")
        return 0
    try:
        r = subprocess.run(["gh", "pr", "view", str(a.pr), "--repo", a.repo,
                            "--json", "state", "-q", ".state"],
                           capture_output=True, text=True, timeout=45)
        state = (r.stdout or "").strip().upper()
    except Exception as e:
        print(f"gh pr view failed: {e!r}", file=sys.stderr)
        return 1
    if state not in ("MERGED", "CLOSED"):
        print(f"{a.repo}#{a.pr} still {state or '?'} — outcome undetermined, skip")
        return 0
    verdict = v.get("verdict")
    matched = (verdict == "PASS" and state == "MERGED") or (verdict == "BLOCK" and state == "CLOSED")
    t = record(v.get("tier", "high"), matched,
               note=f"{a.repo}#{a.pr} verdict={verdict} outcome={state}")
    print(f"recorded: tier={v.get('tier')} matched={matched} "
          f"consecutive={t['consecutive_matches']}")
    return 0


def cmd_record(a):
    matched = str(a.matched).strip().lower() in ("1", "true", "yes", "on")
    t = record(a.tier, matched)
    print(json.dumps(t, indent=2))
    return 0


def cmd_status(a):
    d = _load()
    print(json.dumps({"thresholds": d["thresholds"],
                      "tiers": d["tiers"],
                      "eligibility": _eligibility(d)}, indent=2))
    return 0


def main():
    p = argparse.ArgumentParser(description="Per-tier autonomy graduation ledger.")
    sub = p.add_subparsers(dest="cmd", required=True)
    rc = sub.add_parser("reconcile")
    rc.add_argument("--repo", required=True)
    rc.add_argument("--pr", required=True, type=int)
    rc.set_defaults(fn=cmd_reconcile)
    rec = sub.add_parser("record")
    rec.add_argument("--tier", required=True, choices=["low", "medium", "high"])
    rec.add_argument("--matched", required=True)
    rec.set_defaults(fn=cmd_record)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
