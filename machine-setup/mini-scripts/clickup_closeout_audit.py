#!/usr/bin/env python3
"""
clickup_closeout_audit.py — Hermes cron wrapper around closeout_audit.mjs.

Defense-in-depth backstop for the completion guardrails (G1–G6). Runs the Node
audit over the Hermes-operated ClickUp lists and prints a digest ONLY when a hard
violation (self-complete / FAIL-override / fabricated-auth) is found. Empty stdout
on a clean run → the `--no-agent` cron stays silent (no noise).

Register (already done):
  hermes cron create "0 8 * * *" --name clickup-closeout-audit \
    --script clickup_closeout_audit.py --no-agent --deliver local

Env:
  CLICKUP_CLOSEOUT_AUDIT_LISTS  comma-sep list IDs (default: the core Hermes lists)
  CLICKUP_CLOSEOUT_AUDIT_SINCE  lookback days (default "2")
  CLICKUP_API_TOKEN             required (Doppler-injected by the cron env)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

AUDIT = os.path.expanduser("~/dev/ignite-skills-live/skills/clickup/closeout_audit.mjs")

# Core Hermes-operated lists (high-traffic executor lanes). Extend via env.
DEFAULT_LISTS = [
    ("901714465284", "AI Dev Assistant"),
    ("901713991975", "jdm.com SEO"),
    ("901708886398", "jdm.com v4"),
]

SINCE = os.environ.get("CLICKUP_CLOSEOUT_AUDIT_SINCE", "2")


def _lists() -> list[tuple[str, str]]:
    env = os.environ.get("CLICKUP_CLOSEOUT_AUDIT_LISTS", "").strip()
    if env:
        return [(x.strip(), f"list-{x.strip()}") for x in env.split(",") if x.strip()]
    return DEFAULT_LISTS


def main() -> int:
    if not os.path.exists(AUDIT):
        print(f"closeout-audit: {AUDIT} not found (skill not deployed?)", file=sys.stderr)
        return 0  # never error the cron
    flagged: list[str] = []
    for list_id, label in _lists():
        try:
            proc = subprocess.run(
                ["node", AUDIT, list_id, "--since", SINCE, "--json"],
                capture_output=True, text=True, timeout=180,
            )
        except Exception as e:  # noqa: BLE001
            print(f"closeout-audit: {label} ({list_id}) run error: {e!r}", file=sys.stderr)
            continue
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            print(f"closeout-audit: {label} non-JSON output: {proc.stdout[:200]}", file=sys.stderr)
            continue
        for r in data.get("results", []):
            hard = [v for v in r.get("violations", []) if v.get("severity") == "hard"]
            if not hard:
                continue
            flagged.append(f"• {r['taskId']} [{r.get('status')}] {r.get('name','')}\n  {r.get('url','')}")
            for v in hard:
                flagged.append(f"    ⛔ {v['code']}: {v['detail']}")

    if flagged:
        print("🚨 Hermes closeout audit — HARD violations (completion guardrails G1–G3):\n")
        print("\n".join(flagged))
        print("\nReview + re-validate. See brain: learnings/2026-06-22 Hermes completion guardrails.")
    return 0  # reporter, not a gate — never fail the cron


if __name__ == "__main__":
    raise SystemExit(main())
