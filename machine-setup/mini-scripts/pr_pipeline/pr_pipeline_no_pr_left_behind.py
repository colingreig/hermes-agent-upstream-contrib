#!/usr/bin/env python3
"""
pr_pipeline_no_pr_left_behind.py

Small Hermes runtime helper: list agent branches that have no open PRs,
and list PRs on agent/* branches that are in conflict or need attention.
Intended to be installed to ~/.hermes/scripts/ and run by the executor.

This script is read-only by default; it prints diagnostics for operator review.
"""
import json
import subprocess
import sys
from shutil import which

if which("gh") is None:
    print("ERROR: gh CLI not found on PATH", file=sys.stderr)
    sys.exit(2)

try:
    # list open PRs whose head ref starts with agent/
    pr_list = subprocess.run([
        "gh", "pr", "list", "--search", "head:agent/", "--state", "open", "--json", "number,title,headRefName,state,mergeable"],
        capture_output=True, text=True, check=True
    )
    prs = json.loads(pr_list.stdout)
except subprocess.CalledProcessError as e:
    print("ERROR: gh pr list failed:\n", e.stderr, file=sys.stderr)
    sys.exit(3)

# show summary
print("Open PRs on agent/* (count=", len(prs), ")")
for p in prs:
    print(f"#%d %s — head=%s state=%s mergeable=%s" % (p.get('number'), p.get('title')[:80], p.get('headRefName'), p.get('state'), p.get('mergeable')))

# find local agent branches (best-effort): query `git ls-remote --heads origin` and compare
try:
    rem = subprocess.run(["git", "ls-remote", "--heads", "origin"], capture_output=True, text=True, check=True)
    heads = [line.split()[1].replace('refs/heads/','') for line in rem.stdout.splitlines() if line.strip()]
except subprocess.CalledProcessError:
    heads = []

agent_heads = [h for h in heads if h.startswith('agent/')]
open_head_names = {p.get('headRefName') for p in prs}
no_pr_heads = [h for h in agent_heads if h not in open_head_names]

print("\nAgent branches on origin with NO open PRs (count=", len(no_pr_heads), ")")
for h in no_pr_heads[:50]:
    print(h)

print("\nDiagnostic completed — operator may inspect and run remediation (rebase/push/create-pr) manually.")
