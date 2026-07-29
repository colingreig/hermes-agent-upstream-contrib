#!/usr/bin/env python3
"""Test the ci_green_on_base base-fixing exemption in validate_tripwires.

Proves: (a) an explicitly-listed PR is exempted (info, not high); (b) the
exemption is SCOPED to exactly the listed repo#pr; (c) it FAILS SAFE — no pr,
missing/corrupt file, or empty list => full high block stands.

Run: ~/.hermes/runtime-current/venv/bin/python3.11 test_ci_base_fix_exempt.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import validate_tripwires as vt          # noqa: E402
import ci_health_watch                   # noqa: E402

# Stub the network call: pretend base main is RED on the E2E smoke.
ci_health_watch._red_workflows = lambda repo: {"E2E Smoke — Report Outage": {"conclusion": "failure"}}

REPO = "colingreig/elevatoruptime.com"
fails = []


def has_high(findings):
    return any(f["severity"] == "high" for f in findings)


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


print("== check_ci_green against the REAL exempt file (must list #17) ==")
# The real ci_base_fix_exempt.json carries colingreig/elevatoruptime.com#17.
check("listed PR #17 is EXEMPT (no high finding)",
      not has_high(vt.check_ci_green(REPO, 17)))
check("listed PR #17 still emits a visible info finding (not silent)",
      any(f["severity"] == "info" for f in vt.check_ci_green(REPO, 17)))
check("unlisted PR #14 is BLOCKED (high) — scoped to #17 only",
      has_high(vt.check_ci_green(REPO, 14)))
check("pr=None is BLOCKED (high) — fail-safe when PR unknown",
      has_high(vt.check_ci_green(REPO, None)))
check("same PR number on a DIFFERENT repo is BLOCKED (high)",
      has_high(vt.check_ci_green("colingreig/other-repo", 17)))
check("green base => no finding at all (exemption irrelevant)",
      (lambda orig: (
          setattr(ci_health_watch, "_red_workflows", lambda r: {}),
          vt.check_ci_green(REPO, 17) == [],
          setattr(ci_health_watch, "_red_workflows", orig),
      )[1])(ci_health_watch._red_workflows))

print("== _ci_base_fix_exempt fail-safe (explicit paths) ==")
check("missing file => not exempt",
      vt._ci_base_fix_exempt(REPO, 17, path="/nonexistent/nope.json") is False)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    f.write("{ this is not valid json ]")
    corrupt = f.name
check("corrupt file => not exempt", vt._ci_base_fix_exempt(REPO, 17, path=corrupt) is False)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"exempt": {}}, f)
    empty = f.name
check("empty exempt list => not exempt", vt._ci_base_fix_exempt(REPO, 17, path=empty) is False)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"exempt": {f"{REPO}#99": {"reason": "x"}}}, f)
    other = f.name
check("listed #99 exempts #99 but NOT #17", (
    vt._ci_base_fix_exempt(REPO, 99, path=other) is True
    and vt._ci_base_fix_exempt(REPO, 17, path=other) is False))

for p in (corrupt, empty, other):
    try:
        os.unlink(p)
    except OSError:
        pass

print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
