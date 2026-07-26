#!/usr/bin/env python3
"""validator_panel.py — adversarial LLM review, scaled to risk tier.

Runs ONLY after validate_tripwires (the deterministic layer). Spawns diverse,
refute-oriented "lenses" via Hermes's own one-shot path (`hermes -z -m MODEL
--provider P`) so it reuses Hermes's credential pool / failover instead of a raw
API call. Each lens is told to TRY TO BLOCK and to default to BLOCK on
uncertainty — verification, not rubber-stamping.

Tier -> lenses & model (per the plan: medium=cheap stack, high=Claude):
  low     : panel skipped (deterministic verdict stands)
  medium  : 1 lens  (correctness+governance)     VALIDATOR_MED_MODEL  (default: Hermes default)
  high    : 3 lenses (security / governance /     VALIDATOR_HIGH_MODEL (default: claude-opus-4-8
            verified-live)                                              via VALIDATOR_HIGH_PROVIDER=anthropic)

Aggregation: BLOCK if ANY lens blocks.
Fallback when the provider/model is unavailable (e.g. Anthropic not yet wired
into Hermes auth): high -> BLOCK (needs human), medium -> PASS-with-warning
(defer to the clean tripwires). The verdict records model_used so shadow data
shows whether the cheap model alone was sufficient before Claude is enabled.

Output (JSON): {verdict, tier, model_used, panel_ran, lenses:[{lens,verdict,reason}]}
"""
import argparse
import json
import os
import re
import subprocess
import sys

if __package__:
    from . import validator_common as vc
    from . import validator_model
else:
    import validator_common as vc
    import validator_model

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
HERMES_TIMEOUT = 240
# Chars of diff per lens prompt. 14000 was far too small — it truncated real
# PRs MID-CODE, and the lens then BLOCKED because the code "looked incomplete"
# (a self-inflicted false BLOCK; every medium PR died this way, 2026-06-20).
# All chain models hold this easily: gemini-3.5-flash ~1M ctx, MiniMax-M3 512k,
# claude-haiku 200k. 180k chars ≈ 45k tokens — safe headroom on every tier.
DIFF_BUDGET = 180000

LENSES = {
    "medium": [
        ("correctness+governance",
         "Find any correctness bug, broken edge case, or violation of a stated "
         "repo convention/governance rule (e.g. a script doing something a "
         "runbook says must be human-gated)."),
    ],
    "high": [
        ("security",
         "Find any security hole: missing/weak auth on an endpoint, a check that "
         "validates the SHAPE of a credential rather than verifying it, secrets "
         "exposure, an unauthenticated path that writes to protected main or "
         "dispatches a workflow, SSRF/injection, or over-broad token scope."),
        ("governance",
         "Find any violation of a documented repo rule or process: a script "
         "performing a step a human must perform (e.g. approving content), a CI "
         "gate weakened rather than fixed, or an irreversible action taken "
         "without a guard."),
        ("verified-live",
         "Find anything SHIPPED-BLIND: a provider/API migration, integration, or "
         "destructive path that the diff/PR does not demonstrate was actually "
         "exercised (no test, no run output, 'should work' assumptions). "
         "ACCEPTED EVIDENCE — do NOT block when the diff already contains it: a "
         "committed smoke / integration script that exercises the REAL adapter "
         "(live DB / API / money path — NOT a stub or recording double), "
         "especially an alert-suppressed live-DB smoke (a process kill-switch "
         "such as AOE_SUPPRESS_ALERTS that mutes outbound notify/Sentry while the "
         "durable write still runs) and/or its captured stdout committed as a log "
         "or fixture. A real exercised path present in the diff SATISFIES this "
         "lens — PASS it. PREFER the stronger evidence form: committed captured "
         "PASS stdout (proof the run succeeded) outranks bare script presence "
         "(proof only that the path could be exercised) — cite the committed PASS "
         "output when both are present; script-only stays acceptable but weaker. "
         "(You only see the unified diff, not PR comments, so judge the committed "
         "smoke and any committed run output itself; a genuine non-stub live smoke "
         "is the evidence, not prose.) Block only when the live path is truly unexercised "
         "or the only 'test' is a stub that asserts call shape against a double."),
    ],
}

# Strictness is tier-scaled. HIGH-risk surfaces (auth/secrets/CI-dispatch/DB-RLS)
# fail CLOSED — a false BLOCK costs a human glance, a false PASS can ship a
# security hole to prod. MEDIUM-risk app source is calibrated to block on
# DEMONSTRABLE defects only; an adversarial model told "if uncertain, BLOCK"
# nitpicks every PR into a permanent BLOCK, which strands the merge queue just as
# badly as the truncation bug did (observed 2026-06-20). The panel exists to
# catch real shipped failures (unauth endpoints, machine-approving human-gated
# content, unrun migrations) — NOT to enforce code style.
STRICTNESS = {
    "high": ("Be a MAXIMAL skeptic: if you are UNCERTAIN whether this is safe, "
             "BLOCK. A false BLOCK only costs a human review; a false PASS can "
             "ship a security or governance failure to production."),
    "medium": ("Block ONLY on a concrete, demonstrable defect you can point to in "
               "the diff: a correctness bug that yields wrong output or data loss, "
               "a security exposure, or a violation of a documented repo "
               "rule/runbook. Do NOT block on style, naming, formatting, "
               "missing-defensive-coding suggestions, test-coverage gaps, or "
               "speculative 'could be better' concerns — PASS those. If your "
               "findings are only nits, or you are merely uncertain without a "
               "concrete defect in hand, PASS."),
}

_BASE_PROMPT = """You are an adversarial code-review LENS for an autonomous merge gate. \
Your job is to find anything that should BLOCK this change. {strictness}

LENS FOCUS: {focus}

Risk tier: {tier}. Deterministic tripwire findings already raised (context):
{tripwires}

Below is the unified diff{trunc}. Review ONLY what it actually does — do not assume \
unseen code fixes a problem you can see.

IMPORTANT — TRUNCATION: the diff may be cut off at the END for length (see the \
"(truncated …)" note above if present). If the diff appears to end mid-line, \
mid-string, or mid-function, that is a DISPLAY artifact of OUR truncation, NOT a \
defect in the PR. Do NOT block for "incomplete / unclosed / syntactically invalid" \
solely because the visible text ends abruptly — judge only the substance of the \
changes you CAN see.

--- DIFF START ---
{diff}
--- DIFF END ---

Reason in plain English, at most 150 words total. No commit hashes, no PIDs, \
no file-path dumps, no internal IDs, no PR numbers or SHAs, and no raw diff \
snippets. If you BLOCK, lead with the direct fix the author should make next. \
Then end your reply with EXACTLY one final line. The final line MUST start with \
the bare token VERDICT: — no markdown bold, no bullet, no leading backticks or \
punctuation. One of:
VERDICT: PASS    (only if you are confident this lens finds nothing blocking)
VERDICT: BLOCK — <one-line reason>
"""


def _run_lens_chain(focus_name, focus_desc, diff, tier, tripwires_text, chain):
    """Try each (provider, model) in the resolved chain until one returns a real
    verdict — Claude best -> Claude next -> Gemini -> MiniMax. Returns the lens
    result annotated with the model that actually answered. ERROR only if EVERY
    chain entry failed."""
    last = {"lens": focus_name, "verdict": "ERROR", "reason": "empty model chain"}
    for entry in chain:
        res = _run_lens(focus_name, focus_desc, diff, tier, tripwires_text,
                        entry["model"], entry["provider"])
        if res["verdict"] != "ERROR":
            res["model"] = entry["label"]
            return res
        last = res
        last["model"] = entry["label"]
    return last


# A line carries a verdict if "VERDICT:" appears after stripping markdown/list
# decoration (**bold**, `code`, "- ", "> ", "# ") — models wrap the final line
# in formatting often enough that a bare `startswith("VERDICT:")` missed it and
# fell through to default-deny BLOCK. We scan for the token anywhere in the line.
_VERDICT_RE = re.compile(r"VERDICT\s*:\s*(.+)$", re.IGNORECASE)

# hermes -z prints provider/transport failures to STDOUT and STILL EXITS 0 (rc is
# unreliable). Without detecting these, a 404/502/quota error string carries no
# VERDICT token, so _parse_verdict() defaults to BLOCK "no parseable verdict" — a
# FALSE BLOCK that also stops the chain from falling back to the next model. These
# sentinels reclassify such output as a model ERROR so _run_lens advances the chain
# (e.g. openai-api:gpt-5-mini 404/502 -> fall back to minimax:MiniMax-M3).
# Root cause observed 2026-06-23: gpt-5-mini/openai-api one-shots returning HTTP
# 404/502 became permanent high-tier false-BLOCKs (PR jdmbuysell-v4#394 et al.).
_API_FAIL_RE = re.compile(
    r"API call failed|HTTP\s+[45]\d\d|Error code:\s*[45]\d\d|Bad Gateway|"
    r"rate.?limit|quota|insufficient_quota|overloaded|timed?\s*out|timeout|"
    r"no (?:healthy|available) (?:model|provider)|all providers (?:failed|exhausted)|"
    r"connection (?:error|refused|reset)|service unavailable",
    re.IGNORECASE,
)


def _looks_like_api_failure(out):
    """True if output is a provider/transport failure rather than a model answer.
    Only consulted when NO verdict token was found (so a normal answer that merely
    mentions e.g. 'rate limit' in prose is never misread — _parse_verdict wins)."""
    return bool(_API_FAIL_RE.search(out or ""))


def _parse_verdict(out):
    """Extract (verdict, reason) from raw lens output. Robust to markdown
    decoration around the final line. Uses the LAST verdict-bearing line.
    Default-deny (BLOCK) only when NO verdict token is present at all."""
    verdict, reason = "BLOCK", "no parseable verdict (default-deny)"
    for line in (out or "").splitlines():
        m = _VERDICT_RE.search(line)
        if not m:
            continue
        # strip trailing markdown decoration from the captured body
        body = m.group(1).strip().strip("*`_ ").strip()
        if body.upper().startswith("PASS"):
            verdict, reason = "PASS", ""
        elif body.upper().startswith("BLOCK"):
            verdict = "BLOCK"
            reason = body[5:].lstrip(" —-:*`").strip() or "blocked"
    return verdict, reason


def _run_lens(focus_name, focus_desc, diff, tier, tripwires_text, model, provider):
    trunc = ""
    d = diff
    if len(d) > DIFF_BUDGET:
        d = d[:DIFF_BUDGET]
        trunc = f" (truncated to first {DIFF_BUDGET} chars of {len(diff)})"
    prompt = _BASE_PROMPT.format(focus=focus_desc, tier=tier,
                                 strictness=STRICTNESS.get(tier, STRICTNESS["high"]),
                                 tripwires=tripwires_text or "(none)",
                                 trunc=trunc, diff=d)
    cmd = [HERMES_BIN, "-z", prompt]
    if model:
        cmd += ["-m", model]
    if provider:
        cmd += ["--provider", provider]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=HERMES_TIMEOUT)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        if r.returncode != 0 and "VERDICT:" not in out:
            return {"lens": focus_name, "verdict": "ERROR",
                    "reason": f"hermes rc={r.returncode}: {(r.stderr or '').strip()[:160]}"}
        verdict, reason = _parse_verdict(out)
        # No verdict token AND the output looks like a provider/transport failure
        # (hermes exits 0 even on API errors) -> ERROR, not a default-deny BLOCK,
        # so _run_lens_chain falls back to the next model in the chain.
        if verdict == "BLOCK" and reason == "no parseable verdict (default-deny)" \
                and _looks_like_api_failure(out):
            return {"lens": focus_name, "verdict": "ERROR",
                    "reason": f"model call failed (no verdict): {out.strip()[:160]}"}
        return {"lens": focus_name, "verdict": verdict, "reason": reason}
    except Exception as e:
        return {"lens": focus_name, "verdict": "ERROR", "reason": f"{e!r}"}


def run(diff_text, tier, tripwires=None):
    tripwires_text = ""
    if tripwires:
        tripwires_text = "\n".join(
            f"- {f.get('check')} [{f.get('severity')}] {f.get('file')}: {f.get('detail')}"
            for f in tripwires)
    if tier == "low":
        return {"verdict": "PASS", "tier": tier, "model_used": "(none)",
                "panel_ran": False, "lenses": []}

    chain = validator_model.resolve_chain(tier)
    if not chain:
        # no model resolvable at all
        if tier == "high":
            return {"verdict": "BLOCK", "tier": tier, "model_used": "NO-CHAIN",
                    "panel_ran": False, "lenses": [],
                    "note": "no model chain resolved -> BLOCK (needs human)"}
        return {"verdict": "PASS", "tier": tier, "model_used": "NO-CHAIN",
                "panel_ran": False, "lenses": [],
                "note": "no model chain -> defer to clean tripwires (PASS-with-warning)"}

    lenses = []
    for name, desc in LENSES.get(tier, []):
        lenses.append(_run_lens_chain(name, desc, diff_text, tier, tripwires_text, chain))

    errored = [l for l in lenses if l["verdict"] == "ERROR"]
    blocked = [l for l in lenses if l["verdict"] == "BLOCK"]
    models_used = ",".join(sorted({l.get("model", "?") for l in lenses}))

    # Conservative fallback when EVERY lens errored across the WHOLE chain.
    if errored and len(errored) == len(lenses):
        if tier == "high":
            return {"verdict": "BLOCK", "tier": tier,
                    "model_used": f"{models_used}=ALL-UNAVAILABLE", "panel_ran": False,
                    "lenses": lenses,
                    "note": "high-risk: entire model chain unavailable -> BLOCK (needs human)"}
        return {"verdict": "PASS", "tier": tier,
                "model_used": f"{models_used}=ALL-UNAVAILABLE", "panel_ran": False,
                "lenses": lenses,
                "note": "medium: chain unavailable -> defer to clean tripwires (PASS-with-warning)"}

    verdict = "BLOCK" if blocked else "PASS"
    return {"verdict": verdict, "tier": tier, "model_used": models_used,
            "panel_ran": True, "lenses": lenses}


def main():
    p = argparse.ArgumentParser(description="Adversarial LLM review panel.")
    vc.add_source_args(p)
    p.add_argument("--tier", choices=["low", "medium", "high"], help="override tier")
    p.add_argument("--tripwires", help="path to JSON array of tripwire findings (context)")
    args = p.parse_args()
    diff_text, src = vc.load_diff_from_args(args)
    tier = args.tier
    if not tier:
        if __package__:
            from . import risk_classify
        else:
            import risk_classify
        tier = risk_classify.classify(diff_text)["tier"]
    tw = None
    if args.tripwires:
        try:
            with open(args.tripwires, encoding="utf-8") as f:
                tw = json.load(f)
        except Exception:
            pass
    print(json.dumps(run(diff_text, tier, tw), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
