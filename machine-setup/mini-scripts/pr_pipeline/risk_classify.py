#!/usr/bin/env python3
"""risk_classify.py — deterministic blast-radius classifier for a PR/worktree diff.

Single source of truth for "how dangerous is this change" — consumed by
validate_tripwires.py (to decide panel depth) and merge_guard.py (to pick the
per-tier autonomy flag). Pure pattern + path rules, zero LLM, zero network beyond
the optional `gh` fetch in validator_common.

Output (JSON): {"tier": "low|medium|high", "surfaces": [...], "files": N}

Tier = high if ANY high-surface matches; else medium if any medium; else low.
High surfaces are the classes that bit us 2026-06-18: auth/session endpoints,
secrets/env, workflow-dispatch / writes to protected main, CI/deploy config, and
DB/RLS migrations. The principle: spend (Claude review, human gate) only where
blast radius is real; newsletter copy ≠ an auth endpoint.

Usage:
  risk_classify.py --repo owner/repo --pr 123
  risk_classify.py --diff /tmp/x.diff
  git diff origin/main... | risk_classify.py
"""
import argparse
import json
import re
import sys

if __package__:
    from . import validator_common as vc
else:
    import validator_common as vc

# Each rule: (label, tier, path_regex_or_None, content_regex_or_None).
# A rule matches a file if its path regex matches the path AND/OR its content
# regex matches any added line. (None means "don't constrain on this axis".)
HIGH_RULES = [
    ("auth/session endpoint", "high",
     re.compile(r"(^|/)(api|auth|middleware|login|session)[^/]*\.(t|j)sx?$|/api/", re.I),
     re.compile(r"\b(cookie|set-cookie|authorization|isSignedIn|requireAuth|"
                r"getSession|jwt|bearer)\b", re.I)),
    ("secrets/env", "high",
     re.compile(r"(^|/)\.env|(^|/)(secrets?|credentials?)\b|wrangler\.(toml|jsonc?)$", re.I),
     re.compile(r"\b(api[_-]?key|client[_-]?secret|access[_-]?token|private[_-]?key|"
                r"password)\b", re.I)),
    ("workflow-dispatch / protected-main write", "high",
     None,
     re.compile(r"\b(repository_dispatch|workflow_dispatch|createWorkflowDispatch|"
                r"/dispatches\b|gh\s+workflow\s+run|protected.?main)\b", re.I)),
    ("CI / deploy config", "high",
     re.compile(r"\.github/workflows/.*\.ya?ml$|(^|/)(vercel|wrangler)\.|"
                r"Dockerfile$|fly\.toml$", re.I),
     re.compile(r"\b(wrangler\s+deploy|vercel\s+(deploy|--prod)|gh\s+pr\s+merge|"
                r"deploy[-_]?(admin|worker|prod))\b", re.I)),
    ("DB migration / RLS", "high",
     re.compile(r"(^|/)(migrations?|supabase)/.*\.(sql|ts)$|\.sql$", re.I),
     re.compile(r"\b(create\s+table|alter\s+table|drop\s+table|create\s+policy|"
                r"row\s+level\s+security|grant\s+)\b", re.I)),
    # Money-movement / spend-gating logic: budgets, payments, orders, billing.
    # Added 2026-06-20 after PR #37 (PressWhizz budget gating, "skip premature
    # client_budget_exceeded auto-resolve") classified MEDIUM and drew a single
    # nondeterministic gemini-flash lens that flip-flopped PASS/BLOCK across runs
    # on the SAME diff. Financial logic is real blast radius — route it to the
    # high-tier panel (3 deterministic Claude lenses), never a coin-flip reviewer.
    ("money / spend-gating logic", "high",
     re.compile(r"(^|/)(billing|payments?|orders?|invoice|budget|spend|wallet|pricing)[^/]*\."
                r"(t|j)sx?$", re.I),
     re.compile(r"\b(client_budget_exceeded|budgetExceeded\w*|budget_exceeded\w*|"
                r"sendOrder|markOrderPaid|mark_order_paid|order_paid|"
                r"amount_due|amount_cents|price_cents|spend_cap|payout|refund|"
                r"invoice|charge_amount|stripe)\b", re.I)),
]

MEDIUM_RULES = [
    ("application source", "medium",
     re.compile(r"\.(t|j)sx?$|\.(py|go|rb|rs|java|mjs|cjs)$", re.I), None),
    ("config", "medium",
     re.compile(r"\.(json|ya?ml|toml|ini|conf)$|package(-lock)?\.json$", re.I), None),
]

# Low = everything else (markdown, .mdoc content, images, copy, translations).


def _rule_hits(rule, fd):
    _, _, path_re, content_re = rule
    path_ok = path_re.search(fd.get("path", "")) if path_re else False
    content_ok = content_re.search(fd.added_text) if content_re else False
    # If both axes are specified, a hit on EITHER counts (path OR content) —
    # an auth endpoint by name, or auth-shaped content anywhere.
    if path_re and content_re:
        return bool(path_ok or content_ok)
    if path_re:
        return bool(path_ok)
    if content_re:
        return bool(content_ok)
    return False


def classify(diff_text: str) -> dict:
    files = vc.parse_unified_diff(diff_text)
    surfaces = []
    tier = "low"
    for fd in files:
        for rule in HIGH_RULES:
            if _rule_hits(rule, fd):
                surfaces.append({"label": rule[0], "file": fd.get("path")})
                tier = "high"
    if tier != "high":
        for fd in files:
            for rule in MEDIUM_RULES:
                if _rule_hits(rule, fd):
                    surfaces.append({"label": rule[0], "file": fd.get("path")})
                    tier = "medium"
    # dedupe surfaces by (label, file)
    seen = set()
    uniq = []
    for s in surfaces:
        k = (s["label"], s["file"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return {"tier": tier, "surfaces": uniq, "files": len(files)}


def main():
    p = argparse.ArgumentParser(description="Deterministic diff risk classifier.")
    vc.add_source_args(p)
    args = p.parse_args()
    diff_text, src = vc.load_diff_from_args(args)
    if not diff_text.strip():
        print(json.dumps({"tier": "low", "surfaces": [], "files": 0,
                          "note": f"empty diff ({src})"}))
        return 0
    print(json.dumps(classify(diff_text), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
