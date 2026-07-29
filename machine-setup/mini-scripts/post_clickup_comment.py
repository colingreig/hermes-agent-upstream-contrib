#!/usr/bin/env python3
# Durable helper: post a long ClickUp comment via the clickup.mjs CLI using Python subprocess.
# Usage: python3 scripts/post_clickup_comment.py <taskId> /path/to/body.txt
#
# Code-enforced truncation guard (86e260vnx, 2026-07-05): a closeout comment on
# task 86e25qckh shipped ending mid-sentence — "Note:  could not be executed in
# the cron environment (next:" — an empty interpolated variable in an
# LLM-composed template with no completeness check before posting. Prompt-level
# discipline alone didn't catch it (same class of failure as the content
# placeholder-token gate: prompt-enforced != code-enforced). This is a fast,
# narrow heuristic — not a grammar checker — aimed at the exact failure shapes
# observed: unbalanced brackets/parens, and a body that ends on a dangling
# clause-opener (a colon, comma, dash, or open bracket with nothing after it).

import os
import subprocess
import sys
from pathlib import Path

DANGLING_ENDINGS = (":", ",", "-", "—", "(", "[", "{")


def find_truncation_issue(body):
    """Return a description of a likely-truncated body, or None if it looks complete."""
    stripped = body.rstrip()
    if not stripped:
        return "comment body is empty"
    for open_ch, close_ch in (("(", ")"), ("[", "]"), ("{", "}")):
        if stripped.count(open_ch) != stripped.count(close_ch):
            return f"unbalanced '{open_ch}{close_ch}' ({stripped.count(open_ch)} open vs {stripped.count(close_ch)} close)"
    if stripped[-1] in DANGLING_ENDINGS:
        return f"body ends on a dangling clause-opener ({stripped[-1]!r}) — looks cut off mid-sentence"
    return None


def post_comment(task_id, body_path):
    with open(body_path, 'r') as f:
        body = f.read()
    issue = find_truncation_issue(body)
    if issue:
        print(f"REFUSING to post — comment looks truncated: {issue}", file=sys.stderr)
        print(f"Body tail: ...{body.rstrip()[-160:]!r}", file=sys.stderr)
        print("Fix the template/interpolation and re-run — see 86e260vnx.", file=sys.stderr)
        return False
    clickup_mjs = resolve_clickup_mjs()
    if not clickup_mjs:
        print('Failed to post comment: could not find clickup.mjs', file=sys.stderr)
        return False
    cmd = ['node', clickup_mjs, 'comment', task_id, body]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print('Failed to post comment', res.returncode, res.stdout, res.stderr, file=sys.stderr)
        return False
    print(res.stdout.strip())
    return True


def resolve_clickup_mjs():
    """Find the local clickup.mjs, preferring the vendored Hermes install."""
    home = Path.home()
    candidates = [
        os.environ.get('CLICKUP_MJS'),
        home / '.hermes' / 'scripts' / 'clickup' / 'clickup.mjs',
        home / '.claude' / 'skills' / 'clickup' / 'clickup.mjs',
        home / '.codex' / 'skills' / 'clickup' / 'clickup.mjs',
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return None

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: post_clickup_comment.py <taskId> <body-file>')
        sys.exit(2)
    task_id = sys.argv[1]
    body_file = sys.argv[2]
    ok = post_comment(task_id, body_file)
    sys.exit(0 if ok else 1)
