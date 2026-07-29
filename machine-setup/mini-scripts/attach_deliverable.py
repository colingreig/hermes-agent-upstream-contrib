#!/usr/bin/env python3
"""
attach_deliverable.py — preserve a deliverable to a STABLE dir + attach it to the ClickUp task.

WHY (Colin, 2026-06-24): Hermes writes deliverables to the mini's LOCAL disk, but review
happens from a SEPARATE Windows PC (and the shared brain). Local-disk drafts in the ephemeral
per-task workdir `~/.hermes/worktrees/ignite-<id>` are (a) invisible cross-machine and (b) deleted when the
workdir is cleaned — so decision-park "draft to disk" deliverables were unreviewable and
effectively LOST (5 parked tasks had 0 attachments, only a summary comment survived).

This helper makes deliverable preservation + attachment ONE deterministic call (the
orchestrator is fumble-prone with multi-step glue):
  1. Copy each <file> to the STABLE, non-ephemeral dir `~/.hermes/deliverables/<taskId>/`
     (survives workdir cleanup and /tmp clears).
  2. `node ~/dev/ignite-skills-live/skills/clickup/clickup.mjs attach <taskId> <stable-copy>` for each.
  3. Verify each attach call succeeded; print ONE result JSON.

It does NOT change the task's status, tags, or post any comment — attachment only. Claim +
review-flip + closeout comment stay with the executor.

clickup.mjs lives in the ignite-skills-live git worktree (~dev/ignite-skills-live/skills/clickup/); we only
CALL it, never edit it.

Exit codes: 0 = all requested files preserved + attached.  2 = nothing to attach (no existing
files).  3 = an attach call failed (partial — see result JSON).  4 = usage/setup error.

Usage:
  python3 ~/.hermes/scripts/attach_deliverable.py --task-id <id> <file> [<file> ...]
  python3 ~/.hermes/scripts/attach_deliverable.py --task-id <id> --stable-only <file>...
      (--stable-only: copy to the stable dir but SKIP the ClickUp attach — e.g. --dry-run upstream)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
CLICKUP = os.path.join(HOME, "dev", "ignite-skills-live", "skills", "clickup", "clickup.mjs")
STABLE_ROOT = os.path.join(HOME, ".hermes", "deliverables")


def out(result, code):
    print(json.dumps(result))
    return code


def main():
    ap = argparse.ArgumentParser(description="Preserve a deliverable to a stable dir + attach to the ClickUp task.")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("files", nargs="+", help="One or more deliverable file paths to preserve + attach.")
    ap.add_argument("--stable-only", action="store_true",
                    help="Copy to the stable dir but SKIP the ClickUp attach (preserve only).")
    args = ap.parse_args()

    result = {"ok": False, "task_id": args.task_id, "stable_dir": None,
              "preserved": [], "attached": [], "skipped_missing": [], "errors": []}

    stable_dir = os.path.join(STABLE_ROOT, args.task_id)
    try:
        os.makedirs(stable_dir, exist_ok=True)
    except Exception as e:
        return out({**result, "errors": [f"could not create stable dir {stable_dir}: {e}"]}, 4)
    result["stable_dir"] = stable_dir

    # 1. Preserve each existing file to the stable dir.
    stable_files = []
    for f in args.files:
        p = os.path.abspath(os.path.expanduser(f))
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            result["skipped_missing"].append(f)
            continue
        dest = os.path.join(stable_dir, os.path.basename(p))
        try:
            # If the source already IS the stable copy, don't copy onto itself.
            if os.path.abspath(dest) != p:
                shutil.copy2(p, dest)
            stable_files.append(dest)
            result["preserved"].append(dest)
        except Exception as e:
            result["errors"].append(f"copy {p} → {dest} failed: {e}")

    if not stable_files:
        result["error"] = "no existing non-empty files to preserve/attach"
        return out(result, 2)

    if args.stable_only:
        result["ok"] = True
        result["note"] = "preserved to stable dir only (--stable-only); ClickUp attach skipped"
        return out(result, 0)

    # 2. Attach each preserved file to the task.
    any_fail = False
    for dest in stable_files:
        try:
            r = subprocess.run(["node", CLICKUP, "attach", args.task_id, dest],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and ("attached" in (r.stdout or "").lower()):
                result["attached"].append(dest)
            else:
                any_fail = True
                result["errors"].append(
                    f"attach {os.path.basename(dest)} failed: rc={r.returncode} "
                    f"out={(r.stdout or '').strip()[-200:]} err={(r.stderr or '').strip()[-200:]}")
        except Exception as e:
            any_fail = True
            result["errors"].append(f"attach {os.path.basename(dest)} raised: {e}")

    result["ok"] = (len(result["attached"]) == len(stable_files)) and not any_fail
    return out(result, 0 if result["ok"] else 3)


if __name__ == "__main__":
    sys.exit(main())
