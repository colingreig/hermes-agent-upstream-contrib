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

Output (JSON): {verdict, tier, model_used, panel_ran, infra_failure, failure_class,
                lenses:[{lens,verdict,reason}]}

INFRASTRUCTURE FAILURE vs REAL DENIAL (2026-07-26)
--------------------------------------------------
A lens that never produced a verdict is NOT a review. Until 2026-07-26 both
outcomes collapsed into the same BLOCK string ("no parseable verdict
(default-deny)"), so 7 PRs sat for a week looking reviewed-and-rejected when no
model had reviewed them at all. Default-deny is retained (an unobtainable
verdict still holds the merge), but it is now:
  * RETRIED once with a format-repair prompt before being given up on,
  * ADVANCED down the model chain instead of being terminal on entry #1,
  * LOGGED raw (truncated + redacted) to ~/.hermes/logs/validator_panel.jsonl,
  * LABELLED with INFRA_FAIL_PREFIX and infra_failure=True / failure_class so a
    human (or the comment-writing agent) can tell "could not obtain a verdict"
    from "a model reviewed this and denied it".
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

if __package__:
    from . import validator_common as vc
    from . import validator_model
else:
    import validator_common as vc
    import validator_model

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
HERMES_TIMEOUT = 240
# The format-repair retry re-sends only the model's own prior answer plus a
# 3-line instruction, so it is tiny — it does not need the full lens timeout.
REPAIR_TIMEOUT = 90
# Soft wall-clock budget for the WHOLE panel (all lenses, all chain entries).
# staleness_sweep kills validate_pr at 600s; without a budget the new chain
# advancement could push past that and the caller would SIGKILL us with no
# verdict and no log line at all. Once the budget is spent we stop advancing
# the chain and fail closed WITH a logged, labelled reason.
PANEL_BUDGET_S = int(os.environ.get("VALIDATOR_PANEL_BUDGET_S", "540") or 540)

# Every unparseable/absent lens response is appended here (raw, truncated,
# secret-redacted) so a parse failure is diagnosable after the fact. Before
# 2026-07-26 the raw response was discarded and only the parsed verdict reached
# stderr, which made the 2026-07-18/19 outage impossible to root-cause from
# logs.
PANEL_LOG = os.path.expanduser(
    os.environ.get("VALIDATOR_PANEL_LOG", "~/.hermes/logs/validator_panel.jsonl"))
PANEL_LOG_MAX_BYTES = 5 * 1024 * 1024
RAW_LOG_CHARS = 1500

# Prefix that marks a verdict as "we could not obtain a review", NOT "a model
# reviewed this and denied it". Downstream comment writers key off this.
INFRA_FAIL_PREFIX = "INFRASTRUCTURE FAILURE (no verdict obtained — the diff was NOT reviewed)"
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

# Format-repair prompt. Sent ONCE per lens when the model produced a real answer
# but omitted the VERDICT line (truncated tail, markdown drift, a chatty wrapper,
# a reasoning model that buried it). Cheap: it re-sends only the model's own
# prior answer, never the diff.
_REPAIR_PROMPT = """Your previous reply to a code-review lens was missing its required final line, \
so it could not be scored. Below is your previous reply verbatim.

--- PREVIOUS REPLY START ---
{prior}
--- PREVIOUS REPLY END ---

Do NOT re-analyse and do NOT add commentary. Output EXACTLY ONE line and nothing \
else, restating the conclusion your previous reply already reached. The line MUST \
begin with the bare token VERDICT: — no markdown, no bullet, no backticks:
VERDICT: PASS
VERDICT: BLOCK — <one-line reason>
If your previous reply reached no conclusion at all, output: VERDICT: BLOCK — no conclusion reached
"""


# --------------------------------------------------------------------------
# Raw-response logging (2026-07-26)
#
# A parser that fails without recording what it failed on is undebuggable. The
# 2026-07-18/19 all-lenses parse failure could not be root-caused because the
# raw model output was discarded the instant _parse_verdict() returned. Every
# lens call now leaves a JSONL breadcrumb; failures carry the raw head+tail.
# --------------------------------------------------------------------------

# Secret shapes that must never reach the log even though the panel does not
# intentionally print credentials (a provider error body can echo one back).
_REDACT_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-<REDACTED>"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}"), "gh<REDACTED>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_<REDACTED>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "AIza<REDACTED>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "xox<REDACTED>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"), "<JWT-REDACTED>"),
    (re.compile(r"(?i)\b(bearer|api[_-]?key|token|secret|password)\b\s*[:=]\s*\S{8,}"),
     r"\1=<REDACTED>"),
]
# Env vars whose literal VALUE must be scrubbed if it ever appears verbatim.
_SECRET_ENV_HINT = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)")


def _redact(text):
    """Best-effort secret scrub. Never raises — logging must not break a run."""
    if not text:
        return ""
    try:
        for name, value in os.environ.items():
            if value and len(value) >= 16 and _SECRET_ENV_HINT.search(name):
                text = text.replace(value, f"<{name}-REDACTED>")
        for pat, repl in _REDACT_PATTERNS:
            text = pat.sub(repl, text)
    except Exception:
        return "<redaction failed; raw text withheld>"
    return text


def _clip(text, limit=RAW_LOG_CHARS):
    """Head+tail clip — the tail matters most (the VERDICT line lives there)."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} chars elided]…\n{text[-tail:]}"


def _panel_log(event, **fields):
    """Append one JSONL breadcrumb. Best-effort; never raises."""
    try:
        rec = {"ts": datetime.datetime.now(datetime.timezone.utc)
               .isoformat(timespec="seconds").replace("+00:00", "Z"),
               "event": event}
        rec.update(fields)
        path = PANEL_LOG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > PANEL_LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _run_lens_chain(focus_name, focus_desc, diff, tier, tripwires_text, chain,
                    deadline=None):
    """Try each (provider, model) in the resolved chain until one returns a real
    verdict — Claude best -> Claude next -> Gemini -> MiniMax. Returns the lens
    result annotated with the model that actually answered.

    Chain advancement (2026-07-26): an UNPARSEABLE response now advances the
    chain exactly like a hard ERROR. Previously entry #1 returning prose with no
    VERDICT line was TERMINAL — the panel default-denied on the spot and never
    asked the other three models, which is why one bad model-output window
    false-BLOCKed 7 PRs at once. Default-deny is unchanged: if the whole chain
    is unparseable the lens still ends BLOCK, but tagged infra_failure so it
    reads as "could not obtain a verdict", not "reviewed and denied"."""
    last = {"lens": focus_name, "verdict": "ERROR", "reason": "empty model chain"}
    saw_unparseable = False
    unparseable_models = []
    repair_used = False
    for entry in chain:
        if deadline is not None and time.monotonic() > deadline:
            _panel_log("chain_budget_exhausted", lens=focus_name,
                       skipped_model=entry["label"])
            last = {"lens": focus_name, "verdict": "ERROR",
                    "reason": "panel wall-clock budget exhausted before this model was tried",
                    "failure_class": "budget-exhausted", "infra_failure": True,
                    "model": entry["label"]}
            break
        res = _run_lens(focus_name, focus_desc, diff, tier, tripwires_text,
                        entry["model"], entry["provider"],
                        allow_repair=not repair_used, deadline=deadline)
        if res.pop("_repair_attempted", False):
            repair_used = True
        if res["verdict"] in ("PASS", "BLOCK"):
            res["model"] = entry["label"]
            return res
        if res["verdict"] == "UNPARSEABLE":
            saw_unparseable = True
            unparseable_models.append(entry["label"])
        last = res
        last["model"] = entry["label"]

    if saw_unparseable:
        # DEFAULT-DENY PRESERVED — but labelled as infrastructure, not review.
        return {
            "lens": focus_name,
            "verdict": "BLOCK",
            "reason": (f"{INFRA_FAIL_PREFIX}: every model in the chain returned a "
                       f"response with no VERDICT line, including after a "
                       f"format-repair retry ({', '.join(unparseable_models)}). "
                       f"No model reviewed this diff — holding the merge fail-closed. "
                       f"Raw responses logged to {PANEL_LOG}. Re-run validation once "
                       f"the panel is healthy."),
            "infra_failure": True,
            "failure_class": "no-parseable-verdict",
            "model": last.get("model"),
        }
    last.setdefault("infra_failure", True)
    last.setdefault("failure_class", "model-chain-unavailable")
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
#
# WIDENED 2026-07-26: the original list was keyword-thin and missed every
# credential/context/CLI failure shape this box actually emits — verified live
# on the mini: "hermes -z: agent failed: No Anthropic credentials found…",
# "No usable credentials found for provider 'minimax'.", and (from an oversize
# argv prompt) OSError "Argument list too long". None of those matched, so any
# one of them arriving on a rc=0 path became a substantive-looking BLOCK.
_API_FAIL_RE = re.compile(
    r"API call failed|HTTP\s+[45]\d\d|Error code:\s*[45]\d\d|Bad Gateway|"
    r"rate.?limit|quota|insufficient_quota|overloaded|timed?\s*out|timeout|"
    r"no (?:healthy|available) (?:model|provider)|all providers (?:failed|exhausted)|"
    r"connection (?:error|refused|reset)|service unavailable|"
    # --- credential / auth shapes (the 2026-07-18/19 window) ---
    r"hermes -z:|agent failed:|no (?:anthropic|openai|google|gemini|minimax) credentials|"
    r"no usable credentials|credentials? (?:not found|missing|invalid|expired)|"
    r"invalid[ _-]x-api-key|authentication_error|permission_error|unauthorized|"
    r"invalid[ _]api[ _]key|api key not valid|credit balance|billing|"
    # --- request/model shapes ---
    r"invalid_request_error|not_found_error|model_not_found|does not exist|"
    r"context[ _]length|prompt is too long|maximum context|request too large|"
    r"max_tokens|content[ _]filter|safety|refus(?:al|ed)|"
    # --- local/transport shapes ---
    r"argument list too long|no final response was produced|traceback \(most recent",
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

    Returns (None, "") when NO verdict token is present at all. The caller — not
    the parser — decides what an absent verdict means; before 2026-07-26 this
    function silently manufactured a BLOCK with a review-shaped reason string,
    which is precisely how an infrastructure failure came to look like a code
    review. Default-deny still happens, one layer up, where it can be labelled."""
    verdict, reason = None, ""
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


# Flipped off permanently (per-process) if the installed hermes predates
# `-z --usage-file`. See _hermes_oneshot.
_USAGE_FILE_SUPPORTED = True


def _hermes_oneshot(prompt, model, provider, timeout):
    """Run one `hermes -z` call. Returns (rc, combined_out, usage, err_note).

    `usage` is hermes's own --usage-file report; its `failed` / `completed`
    fields are AUTHORITATIVE for "did this run actually work", which beats
    regex-sniffing the text. `hermes -z` exits 0 for a partial/failed agent run
    as long as ANY text was produced (see hermes_cli/oneshot.py — the rc=2 guard
    only fires when the response is blank), so rc alone cannot be trusted."""
    global _USAGE_FILE_SUPPORTED
    cmd = [HERMES_BIN, "-z", prompt]
    if model:
        cmd += ["-m", model]
    if provider:
        cmd += ["--provider", provider]
    usage_path = None
    if _USAGE_FILE_SUPPORTED:
        fd, usage_path = tempfile.mkstemp(prefix="validator_panel_usage_", suffix=".json")
        os.close(fd)
        cmd += ["--usage-file", usage_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        if usage_path and r.returncode == 2 and "unrecognized arguments" in out:
            # Older hermes without --usage-file. Degrade once, permanently, and
            # re-run this call unflagged rather than mis-reporting an infra fail.
            _USAGE_FILE_SUPPORTED = False
            _panel_log("usage_file_unsupported", model=model, provider=provider)
            return _hermes_oneshot(prompt, model, provider, timeout)
        usage = {}
        if usage_path:
            try:
                with open(usage_path, encoding="utf-8") as f:
                    usage = json.load(f) or {}
            except Exception:
                usage = {}
        return r.returncode, out, usage, None
    except subprocess.TimeoutExpired:
        return None, "", {}, f"lens call exceeded {timeout}s and was killed"
    except OSError as exc:
        # errno 7 (E2BIG) is real here: the prompt is passed through argv and
        # DIFF_BUDGET(180k) + env must fit inside the ~1MB macOS ARG_MAX.
        return None, "", {}, f"could not launch the lens subprocess: {exc!r}"
    except Exception as exc:  # noqa: BLE001
        return None, "", {}, f"{exc!r}"
    finally:
        if usage_path:
            try:
                os.unlink(usage_path)
            except OSError:
                pass


def _run_lens(focus_name, focus_desc, diff, tier, tripwires_text, model, provider,
              allow_repair=True, deadline=None):
    """One (model, provider) attempt at one lens.

    Returns one of four verdicts:
      PASS / BLOCK  — the model answered and we parsed its verdict (a REAL review)
      ERROR         — infrastructure: no answer at all (bad creds, transport,
                      timeout, hermes reported the run failed). Chain advances.
      UNPARSEABLE   — the model answered with substantive prose but no VERDICT
                      line, and a format-repair retry did not recover one.
                      Chain advances; if the whole chain is unparseable the
                      caller still default-denies, but labelled as infra.
    """
    trunc = ""
    d = diff
    if len(d) > DIFF_BUDGET:
        d = d[:DIFF_BUDGET]
        trunc = f" (truncated to first {DIFF_BUDGET} chars of {len(diff)})"
    prompt = _BASE_PROMPT.format(focus=focus_desc, tier=tier,
                                 strictness=STRICTNESS.get(tier, STRICTNESS["high"]),
                                 tripwires=tripwires_text or "(none)",
                                 trunc=trunc, diff=d)

    started = time.monotonic()
    rc, out, usage, err_note = _hermes_oneshot(prompt, model, provider, HERMES_TIMEOUT)
    elapsed = round(time.monotonic() - started, 1)
    base = {"lens": focus_name, "model_attempted": model, "provider": provider}
    log_common = dict(lens=focus_name, model=model, provider=provider,
                      rc=rc, elapsed_s=elapsed, out_len=len(out or ""),
                      usage={k: usage.get(k) for k in
                             ("failed", "completed", "output_tokens", "api_calls",
                              "model", "provider")} if usage else {})

    # --- hard infrastructure failure: the subprocess never produced an answer ---
    if err_note is not None:
        _panel_log("lens_infra_failure", reason=err_note, **log_common)
        return {**base, "verdict": "ERROR", "infra_failure": True,
                "failure_class": "subprocess", "reason": err_note}

    verdict, reason = _parse_verdict(out)

    if verdict is None:
        # hermes's OWN report beats our regex sniffing. `failed` true (or
        # `completed` explicitly false) means the agent run did not finish, so
        # whatever text came back is not a review.
        run_failed = bool(usage.get("failed")) or usage.get("completed") is False
        if rc not in (0, None) or run_failed or not (out or "").strip() \
                or _looks_like_api_failure(out):
            _panel_log("lens_infra_failure", reason="no verdict; run did not succeed",
                       run_failed=run_failed, raw=_redact(_clip(out)), **log_common)
            detail = (out or "").strip()[:200] or "(no output)"
            return {**base, "verdict": "ERROR", "infra_failure": True,
                    "failure_class": "provider-error",
                    "reason": f"model call failed (no verdict): {_redact(detail)}"}

        # --- substantive answer, but the VERDICT line is missing -------------
        # This is the class that produced the 2026-07-18/19 false BLOCKs. Log
        # the raw response FIRST (so it is diagnosable even if the retry also
        # fails), then try once to repair the format.
        _panel_log("lens_unparseable", reason="answer present but no VERDICT line",
                   repair_attempted=bool(allow_repair),
                   raw=_redact(_clip(out)), **log_common)

        if not allow_repair:
            return {**base, "verdict": "UNPARSEABLE", "infra_failure": True,
                    "failure_class": "no-parseable-verdict",
                    "reason": "no VERDICT line (format-repair retry already spent)",
                    "_repair_attempted": False}
        if deadline is not None and time.monotonic() > deadline:
            return {**base, "verdict": "UNPARSEABLE", "infra_failure": True,
                    "failure_class": "no-parseable-verdict",
                    "reason": "no VERDICT line (no budget left for a repair retry)",
                    "_repair_attempted": False}

        rstart = time.monotonic()
        rrc, rout, rusage, rerr = _hermes_oneshot(
            _REPAIR_PROMPT.format(prior=_clip(out, 6000)), model, provider,
            REPAIR_TIMEOUT)
        rverdict, rreason = _parse_verdict(rout)
        _panel_log("lens_repair_retry", recovered=rverdict is not None,
                   repaired_verdict=rverdict, rc=rrc,
                   elapsed_s=round(time.monotonic() - rstart, 1),
                   err=rerr, raw=_redact(_clip(rout, 600)),
                   lens=focus_name, model=model, provider=provider)
        if rverdict is not None:
            return {**base, "verdict": rverdict, "reason": rreason,
                    "repaired": True, "_repair_attempted": True}
        return {**base, "verdict": "UNPARSEABLE", "infra_failure": True,
                "failure_class": "no-parseable-verdict",
                "reason": "no VERDICT line, and the format-repair retry also "
                          "returned no VERDICT line",
                "_repair_attempted": True}

    _panel_log("lens_verdict", verdict=verdict,
               reason=_redact((reason or "")[:400]), **log_common)
    return {**base, "verdict": verdict, "reason": reason}


def run(diff_text, tier, tripwires=None):
    tripwires_text = ""
    if tripwires:
        tripwires_text = "\n".join(
            f"- {f.get('check')} [{f.get('severity')}] {f.get('file')}: {f.get('detail')}"
            for f in tripwires)
    if tier == "low":
        return {"verdict": "PASS", "tier": tier, "model_used": "(none)",
                "panel_ran": False, "infra_failure": False, "lenses": []}

    chain = validator_model.resolve_chain(tier)
    if not chain:
        # no model resolvable at all
        if tier == "high":
            return {"verdict": "BLOCK", "tier": tier, "model_used": "NO-CHAIN",
                    "panel_ran": False, "lenses": [], "infra_failure": True,
                    "failure_class": "no-model-chain",
                    "note": f"{INFRA_FAIL_PREFIX}: no model chain could be resolved, so "
                            "no lens ran. Holding as BLOCK (needs human). This is NOT a "
                            "review finding — re-run once the model catalog/providers "
                            "are healthy."}
        return {"verdict": "PASS", "tier": tier, "model_used": "NO-CHAIN",
                "panel_ran": False, "lenses": [], "infra_failure": True,
                "failure_class": "no-model-chain",
                "note": f"{INFRA_FAIL_PREFIX}: no model chain resolved -> deferring to "
                        "clean tripwires (medium-risk PASS-with-warning). The diff was "
                        "not adversarially reviewed."}

    deadline = time.monotonic() + PANEL_BUDGET_S
    lenses = []
    for name, desc in LENSES.get(tier, []):
        lenses.append(_run_lens_chain(name, desc, diff_text, tier, tripwires_text,
                                      chain, deadline=deadline))

    errored = [l for l in lenses if l["verdict"] == "ERROR"]
    blocked = [l for l in lenses if l["verdict"] == "BLOCK"]
    infra_blocked = [l for l in blocked if l.get("infra_failure")]
    models_used = ",".join(sorted({l.get("model", "?") for l in lenses}))

    # Conservative fallback when EVERY lens errored across the WHOLE chain.
    if errored and len(errored) == len(lenses):
        if tier == "high":
            return {"verdict": "BLOCK", "tier": tier,
                    "model_used": f"{models_used}=ALL-UNAVAILABLE", "panel_ran": False,
                    "lenses": lenses, "infra_failure": True,
                    "failure_class": "model-chain-unavailable",
                    "note": f"{INFRA_FAIL_PREFIX}: every model in the chain was "
                            "unavailable, so no lens reviewed this diff. Holding as "
                            "BLOCK (needs human). This is NOT a denial on the merits "
                            f"— see {PANEL_LOG} and re-run once the panel is healthy."}
        return {"verdict": "PASS", "tier": tier,
                "model_used": f"{models_used}=ALL-UNAVAILABLE", "panel_ran": False,
                "lenses": lenses, "infra_failure": True,
                "failure_class": "model-chain-unavailable",
                "note": f"{INFRA_FAIL_PREFIX}: medium-risk chain unavailable -> "
                        "deferring to clean tripwires (PASS-with-warning). The diff "
                        "was not adversarially reviewed."}

    verdict = "BLOCK" if blocked else "PASS"
    out = {"verdict": verdict, "tier": tier, "model_used": models_used,
           "panel_ran": True, "infra_failure": bool(infra_blocked or errored),
           "lenses": lenses}
    if infra_blocked:
        # Default-deny stands, but say WHY so nobody reads it as a code review.
        out["model_used"] = f"{models_used}=INFRA-NO-VERDICT"
        out["failure_class"] = "no-parseable-verdict"
        out["note"] = (
            f"{INFRA_FAIL_PREFIX}: "
            + ", ".join(sorted(l["lens"] for l in infra_blocked))
            + " produced no parseable verdict on any model in the chain, including "
              "after a format-repair retry. Holding the merge fail-closed, but this "
              "is NOT a denial on the merits — nothing reviewed the diff for "
              f"{'that lens' if len(infra_blocked) == 1 else 'those lenses'}. "
              f"Raw responses: {PANEL_LOG}. Re-run validation once the panel is healthy.")
    elif errored:
        out["failure_class"] = "partial-lens-error"
        out["note"] = (f"{INFRA_FAIL_PREFIX} on "
                       + ", ".join(sorted(l["lens"] for l in errored))
                       + " (the other lenses reviewed normally).")
    _panel_log("panel_result", tier=tier, verdict=out["verdict"],
               model_used=out["model_used"], infra_failure=out["infra_failure"],
               failure_class=out.get("failure_class"),
               lenses=[{"lens": l.get("lens"), "verdict": l.get("verdict"),
                        "model": l.get("model"), "infra": bool(l.get("infra_failure")),
                        "repaired": bool(l.get("repaired"))} for l in lenses])
    return out


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
