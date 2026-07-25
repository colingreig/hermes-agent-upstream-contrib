#!/usr/bin/env python3
"""
opencode_exec.py — Hermes executor's code-writing delegate.

Hermes (the gpt-5-mini orchestrator) does NOT write code itself. After it has
claimed a ClickUp task, created the per-task worktree, and assembled the task
context into a prompt file, it calls THIS helper to delegate the actual
code-writing to OpenCode on `openai/gpt-5`. The helper then returns a small
result JSON; the orchestrator inspects it and proceeds to commit/push/open-PR
(on success) or leaves the task claimed + posts a diagnostic (on failure).

Design notes:
  * Auth is 1Password-sourced (migrated off Doppler 2026-07-04) but NOT injected
    wholesale. We fetch ONLY the three provider keys (OPENAI_API_KEY_HERMES,
    ANTHROPIC_API_KEY_HERMES, GH_API_KEY_HERMES) via `op read <ref>` and pass an
    explicit allowlist child_env to OpenCode. NO plaintext key is ever written to
    ~/.config/opencode, and OpenCode never sees DATABASE_URL, CLOUDFLARE_API_TOKEN,
    or any other non-LLM credential.
  * The prompt is passed to the child via the OC_PROMPT env var (NOT argv) so a
    large task body with newlines / quotes / `$` can never break shell escaping.
  * `--dangerously-skip-permissions` is REQUIRED: this is a non-interactive cron;
    OpenCode must auto-approve its own file writes or it will hang waiting for input.
  * We stream OpenCode's JSON event log to ~/.hermes/logs/opencode/<task>-<ts>.jsonl
    for post-mortem, parse the final step_finish for cost/tokens/stop-reason, and
    check `git status --porcelain` afterward so the orchestrator knows whether any
    code was actually written (don't open an empty PR).

Exit codes: 0 = OpenCode finished AND the worktree is dirty (code written).
            2 = OpenCode finished but worktree is clean (no changes — soft fail).
            3 = OpenCode errored / timed out / credential failure (hard fail).
            4 = usage / setup error.

Usage:
  op-agnostic — the script invokes `op` itself. Call it directly:
  python3 opencode_exec.py --workdir ~/.hermes/worktrees/ignite-<taskId> \
      --prompt-file /tmp/oc_prompt_<taskId>.txt --task-id <taskId>
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


# GLM/reasoning models leak <think>...</think> tags into OpenCode's --format json
# text events (OpenCode issue #16903, open as of 2026-06). Strip them from any
# captured text so they never reach a PR body / result summary. Same regex OpenCode
# itself uses internally (prompt.ts title-gen).
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>\s*", re.IGNORECASE)


def _strip_think(s):
    return _THINK_RE.sub("", s or "")

# Writer model selection cascades (Colin, 2026-06-24, reordered 2026-07-05 / 86e260vnu).
#
# CODE cascade (cheapest-fallback-first; gemini-3.1-pro REMOVED from generic fallback
# — reserved for kanban decomposer only, see KANBAN_DECOMPOSER_CASCADE):
#   gpt-5.5(codex OAuth, ARMED) > gpt-5.4-mini(codex OAuth) > glm-4.7 > m3 > gemini-3.5-flash > sonnet/opus(gated)
# Rationale: when glm quota is exhausted (resets 2026-07-08 21:37), every miss must
# land on the CHEAPEST viable tier, not the most expensive one. MiniMax-M3 is the
# cheap consumption floor; gemini-3.5-flash is cheaper than 3.1-pro and is now the
# generic Google tail. The dead gpt-5 (consumption) tier is removed — gpt-5.4-mini
# (still Codex OAuth, subscription-flat) is the tier-2 fallback ahead of glm now.
#
# CONTENT cascade (prose/file-based content): sonnet ONLY, hard fail — no fallback
# (2026-07-12). Colin's content pick; glm-4.7/gemini-3.5-flash tail removed —
# content quality regressions on fallback tiers were worse than a hard failure.
#
# Both are config-PRIORITY cascades (first enabled tier), matching the prior writer
# design — flip a tier off via its flag to fall back. An explicit `--model X`
# (X != "auto") bypasses both cascades (escape hatch / per-call override).
WRITER_CASCADE = [
    ("openai/gpt-5.5",              "openai-codex"),  # PRIMARY when HERMES_WRITER_CODEX=1 (subscription-flat) — Colin's pick; documented hang risk → runner timeout+failover covers it
    ("openai/gpt-5.4-mini",         "openai-codex"),  # tier-2 Codex OAuth fallback (subscription-flat)
    ("zai-coding/glm-4.7",          "zai"),           # failover (1M ctx, unlimited)
    ("minimax/MiniMax-M3",          "minimax"),       # cheap consumption floor
    ("google/gemini-3.5-flash",     "google-flash"),  # cheap Google tier (was 3.1-pro)
    ("anthropic/claude-opus-4-8",   "anthropic"),     # gated, pricey
    ("anthropic/claude-sonnet-5",   "anthropic"),     # gated, pricey
]

# Content writer cascade — prose/file-based content. Sonnet only, hard fail (2026-07-12).
# glm-4.7 and gemini-3.5-flash removed from the tail — no fallback for content anymore.
CONTENT_CASCADE = [
    ("anthropic/claude-sonnet-5",   "content-anthropic"),
]

# Kanban decomposer — the ONLY tier where gemini-3.1-pro stays a generic fallback
# (86e260vnu, 2026-07-05). Per Colin's brief, 3.1-pro is reserved for the decomposer
# only; everywhere else it is intentionally absent from the cascade.
#
# Derived (2026-07-12) from auxiliary.kanban_decomposer in ~/.hermes/config.yaml —
# config.yaml is the single source of truth for PROVIDER ORDER; this module maps
# each provider to its opencode-namespace model slug via
# _OPENCODE_MODEL_BY_PROVIDER (config.yaml's own `model:` strings are NOT
# opencode-namespaced, so they can't be used directly). Falls back to the old
# literal cascade below on any load failure.
_KANBAN_DECOMPOSER_CASCADE_FALLBACK = [
    ("google/gemini-3.1-pro-preview", "google-decomposer"),
    ("google/gemini-3.5-flash",     "google-flash"),
    ("minimax/MiniMax-M3",          "minimax"),
]

# provider (as used in config.yaml's auxiliary.* blocks) -> (opencode model slug,
# _provider_enabled() gate tag). Only providers actually seen in auxiliary configs
# need an entry here; unknown providers are skipped with a stderr warning.
_OPENCODE_MODEL_BY_PROVIDER = {
    "openai-codex": ("openai/gpt-5.4-mini",       "openai-codex"),
    "zai":          ("zai-coding/glm-4.7",        "zai"),
    "anthropic":    ("anthropic/claude-haiku-4-5", "anthropic"),
    "gemini":       ("google/gemini-3.5-flash",   "google-flash"),
}


def _load_decomposer_cascade_from_config():
    """Derive KANBAN_DECOMPOSER_CASCADE from auxiliary.kanban_decomposer in
    ~/.hermes/config.yaml (primary + fallback_chain, in order). config.yaml owns
    provider order; _OPENCODE_MODEL_BY_PROVIDER owns the opencode model slug for
    each provider. Returns _KANBAN_DECOMPOSER_CASCADE_FALLBACK on any failure or
    empty result."""
    try:
        import yaml
    except ImportError:
        print("opencode_exec: pyyaml not available, using KANBAN_DECOMPOSER_CASCADE fallback", file=sys.stderr)
        return _KANBAN_DECOMPOSER_CASCADE_FALLBACK
    try:
        config_path = os.path.expanduser("~/.hermes/config.yaml")
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        decomposer = ((config.get("auxiliary") or {}).get("kanban_decomposer")) or {}
        entries = []
        primary_provider = decomposer.get("provider")
        if primary_provider:
            entries.append(primary_provider)
        for tier in decomposer.get("fallback_chain") or []:
            provider = tier.get("provider")
            if provider:
                entries.append(provider)
        cascade = []
        for provider in entries:
            mapped = _OPENCODE_MODEL_BY_PROVIDER.get(provider)
            if mapped is None:
                print("opencode_exec: unknown kanban_decomposer provider %r, skipping" % provider, file=sys.stderr)
                continue
            cascade.append(mapped)
        return cascade or _KANBAN_DECOMPOSER_CASCADE_FALLBACK
    except Exception as exc:
        print("opencode_exec: failed to load kanban_decomposer cascade from config.yaml (%s), using fallback" % exc, file=sys.stderr)
        return _KANBAN_DECOMPOSER_CASCADE_FALLBACK


KANBAN_DECOMPOSER_CASCADE = _load_decomposer_cascade_from_config()  # single source of truth: auxiliary.kanban_decomposer in config.yaml


def _truthy(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _provider_enabled(provider):
    if provider == "zai":
        # The new primary code-writer. ON by default; HERMES_WRITER_GLM=0 disables.
        return _truthy(os.environ.get("HERMES_WRITER_GLM"), default=True)
    if provider == "openai-codex":
        # Codex OAuth backend via gpt-5.4. OFF by default (opt-in). Arm with
        # HERMES_WRITER_CODEX=1.  Requires the strip-shim (:8647) and codex
        # proxy (:8646) to be running, and opencode.jsonc "openai" override in
        # place. The openai.baseURL override routes through the strip-shim.
        return _truthy(os.environ.get("HERMES_WRITER_CODEX"), default=False)
    if provider == "anthropic":
        # Code cascade only: OFF by default (Opus/Sonnet are pricey for bulk code).
        return _truthy(os.environ.get("HERMES_WRITER_ANTHROPIC"), default=False)
    if provider == "content-anthropic":
        # Content cascade: Sonnet ON by default (Colin's content pick).
        return _truthy(os.environ.get("HERMES_CONTENT_SONNET"), default=True)
    if provider == "google":
        # Legacy gemini-3.1-pro tail (now used only by the kanban decomposer cascade).
        # Kept for backwards compatibility; the writer cascade no longer includes 3.1-pro.
        return _truthy(os.environ.get("HERMES_CONTENT_GEMINI"), default=True)
    if provider == "google-flash":
        # Cheap Google tail for the generic writer/content cascades (86e260vnu).
        # ON by default; HERMES_GOOGLE_FLASH=0 disables it.
        return _truthy(os.environ.get("HERMES_GOOGLE_FLASH"), default=True)
    if provider == "google-decomposer":
        # Kanban-decomposer-only gate; the ONLY cascade that uses gemini-3.1-pro.
        return _truthy(os.environ.get("HERMES_DECOMPOSER_GEMINI_PRO"), default=True)
    if provider == "openai":
        return _truthy(os.environ.get("HERMES_WRITER_OPENAI"), default=True)
    if provider == "minimax":
        return True  # always-on floor
    return False


def resolve_writer_cascade(content=False):
    """Ordered list of ENABLED models (for runtime failover). Returns (models, cascade_str)."""
    table = CONTENT_CASCADE if content else WRITER_CASCADE
    models = [m for (m, p) in table if _provider_enabled(p)]
    if not models:
        models = ["openai/gpt-5"]  # belt-and-suspenders floor
    return models, ("content:" if content else "code:") + " > ".join(models)


def resolve_writer_model(content=False):
    """First enabled tier in the chosen cascade. Returns (model, cascade_str)."""
    models, cascade_str = resolve_writer_cascade(content=content)
    return models[0], cascade_str


def _variant_for(model, explicit_variant):
    """GLM (zai-coding/*) defaults to bounded 'high' effort; else honor explicit."""
    if explicit_variant:
        return explicit_variant
    if model.startswith("zai-coding/"):
        return "high"
    return ""


def _run_once(model, variant, child_env, workdir, opencode_bin, timeout, log_path, task_id, cascade_label):
    """Run ONE OpenCode delegation and capture results. Returns a dict with
    rc/texts/final/saw_error/timed_out/stderr_tail/elapsed, or {"launch_error": ...}."""
    inner = (
        f'exec "{opencode_bin}" run "$OC_PROMPT" '
        f'--model "{model}" --dangerously-skip-permissions --pure --format json '
        f'--dir "{workdir}"'
    )
    if variant:
        inner += f' --variant "{variant}"'
    cmd = ["sh", "-c", inner]
    eprint(f"[opencode_exec] task={task_id} model={model} variant={variant or '-'} workdir={workdir} "
           f"cascade=[{cascade_label}]")
    eprint(f"[opencode_exec] streaming events → {log_path}")
    eprint(f"[opencode_exec] child_env keys (allowlist): {sorted(k for k in child_env if k != 'OC_PROMPT')}")

    start = time.time()
    texts = []
    final = None
    saw_error = None
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd, env=child_env, cwd=workdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        return {"launch_error": str(e)}

    _watchdog_fired = threading.Event()

    def _watchdog():
        if not proc.poll() and not _watchdog_fired.wait(timeout=timeout):
            pass
        if not _watchdog_fired.is_set():
            try:
                proc.kill()
            except Exception:
                pass

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "text":
                    txt = ev.get("part", {}).get("text", "")
                    if txt:
                        texts.append(txt)
                elif etype == "step_finish":
                    final = ev.get("part", {})
                elif etype in ("error", "step_error"):
                    saw_error = ev
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            _watchdog_fired.set()
            watchdog_thread.join(timeout=5)
        elapsed_now = time.time() - start
        if elapsed_now >= timeout and proc.returncode not in (0,):
            timed_out = True
        stderr_tail = ""
        try:
            stderr_tail = proc.stderr.read()[-2000:]
        except Exception:
            pass

    return {"rc": proc.returncode, "texts": texts, "final": final, "saw_error": saw_error,
            "timed_out": timed_out, "stderr_tail": stderr_tail, "elapsed": round(time.time() - start, 1),
            "log_path": log_path}


DEFAULT_MODEL = "auto"  # "auto" => resolve_writer_model() (the cascade above)
DEFAULT_TIMEOUT = 1800  # 30 min wall-clock per delegation
LOG_DIR = os.path.expanduser("~/.hermes/logs/opencode")


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _changed_content_paths(workdir, committed_files, deliverables):
    """Resolve the files eligible for outcome measurement after a content run."""
    paths = set(committed_files or [])
    paths.update(deliverables or [])
    try:
        diff = subprocess.run(
            ["git", "-C", workdir, "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if diff.returncode == 0:
            paths.update(line.strip() for line in diff.stdout.splitlines() if line.strip())
        untracked = subprocess.run(
            ["git", "-C", workdir, "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if untracked.returncode == 0:
            paths.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    except Exception as exc:
        eprint(f"[opencode_exec] content outcome path scan failed (fail-open): {exc!r}")
    return sorted(paths)


def _record_content_outcome(task_id, workdir, committed_files, deliverables):
    """Append citation counts for an eligible content run. Never block writing."""
    try:
        import importlib.util

        module_path = os.path.join(os.path.dirname(__file__), "research_outcome_metrics.py")
        spec = importlib.util.spec_from_file_location("research_outcome_metrics", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        paths = _changed_content_paths(workdir, committed_files, deliverables)
        record = module.append_content_outcome(
            task_id=task_id,
            workdir=Path(workdir),
            relative_files=paths,
        )
        eprint(
            "[opencode_exec] content-outcome: "
            f"pieces={record['content_pieces']} "
            f"citation_links={record['citation_links']} "
            f"links_per_piece={record['citation_link_coverage_per_piece']} "
            f"(task={task_id})"
        )
        return record
    except Exception as exc:
        eprint(f"[opencode_exec] content outcome record failed (fail-open): {exc!r}")
        return None


# WRITER-LIVENESS (2026-06-25): persist served-tier record every run so a silent
# Codex→GLM degrade is captured even when the run itself also failed. Appends one
# JSON line to ~/.hermes/logs/writer-served.jsonl. Fail-open: a logging error NEVER
# breaks delegation — if the append fails we just eprint and continue.
_LIVENESS_LEDGER = os.path.expanduser("~/.hermes/logs/writer-served.jsonl")
_EXPECTED_PRIMARY = WRITER_CASCADE[0][0]  # derive from cascade head — a hardcoded literal here drifted (gpt-5.4) after the 7/12 gpt-5.5 bump, logging false degraded=true on every healthy run

def _record_served(result, armed):
    """Append a liveness record for the current run to the writer-served ledger.

    Also stamps result["served_by"] (== served_model, named explicitly so
    downstream closeout-comment / executor-brief builders don't have to know
    that "model" secretly means "the model that actually served, post-
    failover" — see 86e260vnn: pinned-vs-actual drift went unnoticed for
    days because nothing surfaced which tier really served.
    """
    try:
        served_model  = result.get("model", "unknown")
        served_provider = next((p for (m, p) in CONTENT_CASCADE + WRITER_CASCADE + KANBAN_DECOMPOSER_CASCADE if m == served_model), None)
        result["served_by"] = served_model
        degraded      = bool(armed and served_model != _EXPECTED_PRIMARY)
        record = {
            "ts":                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_id":              result.get("task_id", "unknown"),
            "expected_primary_model": _EXPECTED_PRIMARY,
            "served_model":         served_model,
            "served_provider":      served_provider,
            "armed":                bool(armed),
            "degraded":             degraded,
            "writer_cascade":       result.get("writer_cascade"),
            "failover_tried":       result.get("failover_tried", []),
            "cost_usd":             result.get("cost_usd"),
            "ok":                   result.get("ok", False),
        }
        os.makedirs(os.path.dirname(_LIVENESS_LEDGER), exist_ok=True)
        with open(_LIVENESS_LEDGER, "a", encoding="utf-8") as _lf:
            _lf.write(json.dumps(record) + "\n")
            _lf.flush()
        # Always visible (not just on DEGRADED) — the whole point is that
        # pinned-vs-actual drift should be impossible to miss in the log,
        # the executor brief, and the ClickUp closeout comment that quotes it.
        eprint(f"[opencode_exec] served-by: {served_model} (task={result.get('task_id', 'unknown')}, "
               f"cost_usd={result.get('cost_usd')})")
        if degraded:
            eprint(f"[opencode_exec] WRITER-LIVENESS: DEGRADED — armed={armed} "
                   f"expected={_EXPECTED_PRIMARY} served={served_model}")
    except Exception as _lv_err:
        eprint(f"[opencode_exec] WRITER-LIVENESS: ledger write failed (fail-open): {_lv_err!r}")


# ── FALLBACK RECEIPTS (86e260vnu, 2026-07-05) ─────────────────────────────────
# When the cascade advances past a tier because of a quota / rate-limit failure,
# emit a single-line JSON record to ~/.hermes/logs/fallback-receipts.jsonl. This
# is the visible signal the digest email + per-provider spend meter read to surface
# "fallback: <primary> quota exhausted, serving via <model>" in real time, so the
# ~$50/day glm-exhaustion → Gemini burn is impossible to miss. Fail-open: any
# write error is eprint-logged and swallowed — a logging bug NEVER breaks delegation.
_FALLBACK_RECEIPTS = os.path.expanduser("~/.hermes/logs/fallback-receipts.jsonl")


def _emit_fallback_receipt(task_id, primary, next_model, reason, rc, stderr_tail=""):
    """Append one JSON line to the fallback receipts ledger. Fail-open."""
    try:
        record = {
            "ts":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_id":      task_id,
            "primary":      primary,
            "next":         next_model,
            "reason":       reason,            # "quota exhausted" | "hard failure"
            "rc":           rc,
            "stderr_tail":  (stderr_tail or "")[-400:],
        }
        os.makedirs(os.path.dirname(_FALLBACK_RECEIPTS), exist_ok=True)
        with open(_FALLBACK_RECEIPTS, "a", encoding="utf-8") as _rf:
            _rf.write(json.dumps(record) + "\n")
            _rf.flush()
        eprint(f"[opencode_exec] FALLBACK-RECEIPT: {primary} {reason} → serving via {next_model} "
               f"(task={task_id})")
    except Exception as _fr_err:
        eprint(f"[opencode_exec] FALLBACK-RECEIPT: ledger write failed (fail-open): {_fr_err!r}")


# ── Worked-By stamp (86e2d6vjj item 2) ──────────────────────
# clickup_poll_gate stamps Worked-By=Hermes only on the continuation/wake path;
# an UNCLAIMED pick that lands straight in opencode_exec never got stamped, so the
# review-SLA staleness sweep's _worked_by_hermes() safety net could not see Hermes
# had worked it. Stamp once at the choke point below where the executor commits to
# running the task. Mirrors the poll-gate's best-effort contract: never raises,
# never blocks the run; a missing token or API error just logs and continues.
_WORKED_BY_FIELD_ID = "2bf5c958-ca2a-4f6b-bab5-25693b98b1f1"
_WORKED_BY_HERMES_OPTION_ID = (
    os.environ.get("CLICKUP_WORKED_BY_HERMES_OPTION_ID", "").strip()
    or "36c0d22d-3128-42b3-94d2-0d6072d2c0ea"
)


# ── Attempt history (ClickUp 86e2ddcpb, 2026-07-24) ─────────────────────────
# Session-end hook for claim_next.py's retry cap: record every real delegation
# outcome (success/fail/crash) to claim_store's per-task history so the next
# claim_next.py scan can see how many times this task has been attempted in
# the trailing 24h and refuse to reclaim it past HERMES_CLAIM_MAX_ATTEMPTS.
# Fail-open: any error here is eprint-logged and swallowed — history logging
# must never affect delegation success/failure or its return code.
def _record_attempt(task_id, outcome, note=""):
    try:
        import importlib.util as _ilu
        _cs_path = os.path.join(os.path.dirname(__file__), "claim_store.py")
        _cs_spec = _ilu.spec_from_file_location("claim_store", _cs_path)
        _cs = _ilu.module_from_spec(_cs_spec)
        _cs_spec.loader.exec_module(_cs)
        _cs.record_attempt(task_id, outcome, note=note)
    except Exception as e:
        eprint(f"[opencode_exec] record_attempt failed (fail-open): {e!r}")


def _stamp_worked_by_hermes(task_id):
    """Best-effort: set the ClickUp Worked By field to Hermes for an unclaimed pick."""
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token or not task_id or task_id == "unknown":
        _state = "set" if token else "unset"
        eprint("[opencode_exec] worked-by stamp skipped "
               "(token=" + _state + ", task=" + str(task_id) + ")")
        return
    try:
        body = json.dumps({"value": _WORKED_BY_HERMES_OPTION_ID}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.clickup.com/api/v2/task/" + task_id + "/field/" + _WORKED_BY_FIELD_ID,
            data=body, method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            eprint("[opencode_exec] worked-by stamp on " + task_id + ": rc=" + str(resp.status))
    except Exception as e:
        eprint("[opencode_exec] worked-by stamp failed for " + task_id + ": " + repr(e))


def main():
    ap = argparse.ArgumentParser(description="Delegate code-writing to OpenCode (openai/gpt-5).")
    ap.add_argument("--workdir", required=True, help="The per-task worktree OpenCode should edit (e.g. ~/.hermes/worktrees/ignite-<taskId>).")
    ap.add_argument("--prompt-file", required=True, help="File holding the assembled task prompt/context.")
    ap.add_argument("--task-id", default="adhoc", help="ClickUp task id (for log naming).")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--content", action="store_true",
                    default=_truthy(os.environ.get("OPENCODE_CONTENT"), default=False),
                    help="file-based content task (.md/.mdx/.astro, blog) -> Sonnet>GLM>Gemini cascade.")
    ap.add_argument("--variant", default=os.environ.get("OPENCODE_VARIANT", ""), help="reasoning effort: high|max|minimal (optional).")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("OPENCODE_TIMEOUT", DEFAULT_TIMEOUT)))
    ap.add_argument("--opencode-bin", default=os.environ.get("OPENCODE_BIN", os.path.expanduser("~/.hermes/bin/opencode")))
    args = ap.parse_args()

    # HERMES-PATCH 27 (generalized 2026-06-25; migrated off Doppler 2026-07-04): the
    # cascade-control flags (HERMES_WRITER_*, HERMES_CONTENT_*) live in 1Password
    # ("Dev Toolbox/dev"), but the agent's subprocess sanitizer (tools/environments/
    # local.py _sanitize_subprocess_env) scrubs bare HERMES_* vars from this child's
    # env — so os.environ never carries them and _provider_enabled() always saw the
    # hardcoded default. Net effect: gpt-5.4 was silently skipped even when armed (the
    # original CODEX-only bug), AND a flip of any OTHER tier had no effect at all —
    # e.g. HERMES_CONTENT_SONNET=0, set to drop the Anthropic-exhausted Sonnet content
    # primary until it regains access 2026-07-01 (verified dead via live APIError 400
    # on tasks 86e1z6adt/86e1z6adr), was silently ignored, so every content task still
    # burned a failed Sonnet call + ~8s before failing over to GLM. Re-resolve the
    # WHOLE set from 1Password BEFORE resolve_writer_cascade() reads the gates. An
    # EXPLICIT env value (incl. "0" to disable) always wins — we only fill a flag when
    # it is absent/empty. Item-unset → flag stays absent → hardcoded default.
    #
    # (Doppler org-wide auth was decommissioned 2026-07-03; the prior "doppler secrets
    # get" self-heal here had been silently failing every call since then — see
    # ClickUp 86e25xwwb — which is exactly why HERMES_CONTENT_SONNET stayed stuck at 0
    # for 9 days despite the 1Password field having been corrected. Migrated off the
    # `op` CLI entirely 2026-07-05 (86e260vnn / 86e2625xg): `op read` was found to hang
    # indefinitely under OP_SERVICE_ACCOUNT_TOKEN — the same hang class that separately
    # took the Hermes gateway down for hours the same day. Standing directive: never
    # shell out to `op`; use the 1Password service-account SDK via op_sdk_resolve.py.)
    _flag_names = ("HERMES_WRITER_CODEX", "HERMES_WRITER_GLM", "HERMES_WRITER_ANTHROPIC",
                   "HERMES_WRITER_OPENAI", "HERMES_CONTENT_SONNET", "HERMES_CONTENT_GEMINI")
    _missing_flags = [f for f in _flag_names if not os.environ.get(f)]
    if _missing_flags:
        try:
            import importlib.util as _ilu
            _osr_path = os.path.join(os.path.dirname(__file__), "op_sdk_resolve.py")
            _osr_spec = _ilu.spec_from_file_location("op_sdk_resolve", _osr_path)
            _osr = _ilu.module_from_spec(_osr_spec)
            _osr_spec.loader.exec_module(_osr)
            _flag_refs = {f: f"op://Dev Toolbox/dev/{f}" for f in _missing_flags}
            _flag_values = _osr.resolve_refs(list(_flag_refs.values()))
            for _flag, _ref in _flag_refs.items():
                if _ref in _flag_values and _flag_values[_ref]:
                    os.environ[_flag] = _flag_values[_ref]
        except Exception as _osr_err:
            eprint(f"[opencode_exec] cascade-flag self-heal via SDK failed (fail-open): {_osr_err!r}")

    # WRITER-LIVENESS (2026-06-25): snapshot the armed flag NOW, after the Doppler
    # self-heal above (patch 27) has had a chance to populate HERMES_WRITER_CODEX.
    # This is the canonical "is Codex expected?" signal for the liveness record.
    _writer_armed = _truthy(os.environ.get("HERMES_WRITER_CODEX"), default=False)

    # Resolve the ordered writer CASCADE (for runtime failover) unless an explicit
    # --model was passed. --content selects the prose cascade (Sonnet>GLM>Gemini).
    explicit_model = bool(args.model and args.model != "auto")
    explicit_variant = bool(args.variant)
    if explicit_model:
        cascade_models = [args.model]
        writer_cascade = f"explicit:{args.model}"
    else:
        cascade_models, writer_cascade = resolve_writer_cascade(content=args.content)
        args.model = cascade_models[0]

    workdir = os.path.abspath(os.path.expanduser(args.workdir))
    prompt_file = os.path.abspath(os.path.expanduser(args.prompt_file))

    if not os.path.isdir(workdir):
        print(json.dumps({"ok": False, "error": f"workdir not found: {workdir}"}))
        return 4
    if not os.path.isfile(prompt_file):
        print(json.dumps({"ok": False, "error": f"prompt-file not found: {prompt_file}"}))
        return 4
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt = f.read()
    if not prompt.strip():
        print(json.dumps({"ok": False, "error": "prompt-file is empty"}))
        return 4

    # PRE-FLIGHT: daily spend cap (S5 — hard cap against runaway loops).
    # FAIL-OPEN: if spend_guard itself errors, we proceed normally (never halt on a bug).
    # Cap is configurable via HERMES_DAILY_SPEND_CAP_USD (default $50).
    _cap_usd = float(os.environ.get("HERMES_DAILY_SPEND_CAP_USD", "50.0"))
    try:
        import importlib.util as _ilu, sys as _sys
        _sg_path = os.path.join(os.path.dirname(__file__), "spend_guard.py")
        _sg_spec = _ilu.spec_from_file_location("spend_guard", _sg_path)
        _sg = _ilu.module_from_spec(_sg_spec)
        _sg_spec.loader.exec_module(_sg)
        if _sg.is_over_cap(cap_usd=_cap_usd):
            _spend = _sg.daily_spend_usd()
            _msg = (
                f"daily spend cap ${_cap_usd:.2f} reached (today: ${_spend:.2f}) — "
                "delegation blocked; set HERMES_DAILY_SPEND_CAP_USD to a higher value "
                "or HERMES_SPEND_GUARD_DISABLE=1 to override"
            )
            eprint(f"[opencode_exec] SPEND CAP: {_msg}")
            print(json.dumps({"ok": False, "error": _msg, "spend_cap_usd": _cap_usd,
                               "spend_today_usd": _spend}))
            return 3
    except Exception as _sg_err:
        eprint(f"[opencode_exec] spend_guard failed (fail-open, proceeding): {_sg_err!r}")

    os.makedirs(LOG_DIR, exist_ok=True)
    # Date.now() is fine here — this is a live script, not a replayable workflow.
    ts = time.strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOG_DIR, f"{args.task_id}-{ts}.jsonl")

    # Build the inner opencode invocation. The prompt is passed via $OC_PROMPT (env),
    # never argv, so newlines/quotes/`$` in a task body can't break shell escaping.
    #
    # OPENCODE_DISABLE_CLAUDE_CODE=1 stops OpenCode from auto-crawling the operator's
    # ~/.claude/skills + ~/.agents/skills trees (167 skills on this host). That crawl
    # both (a) intermittently HANGS init for minutes and (b) bloats every request by
    # ~35k tokens. The cron worker writes code in a fresh repo and needs none of them.
    # `--pure` additionally skips external OpenCode plugins for deterministic startup.
    #
    # SECURITY FIX (S1-D1): Do NOT pass `op run` with the full env file (which injects
    # all ~135 secrets). Instead, fetch ONLY the provider keys OpenCode actually needs
    # via `op read` and pass an explicit allowlist child_env. OpenCode needs:
    #   OPENAI_API_KEY    — writer model (gpt-5) auth
    #   ANTHROPIC_API_KEY — cascade fallback when HERMES_WRITER_ANTHROPIC=1
    #   GH_TOKEN          — git/PR operations inside the worktree
    # The inner sh -c exports OPENAI_API_KEY from OPENAI_API_KEY_HERMES (the 1Password
    # item name); we now pre-fetch those values and expose them via the child env
    # directly, so the child never sees DATABASE_URL, CLOUDFLARE_API_TOKEN, Meta/MS
    # tokens, etc.
    #
    # Migrated off Doppler 2026-07-04, then off the `op` CLI entirely 2026-07-05
    # (86e260vnn / 86e2625xg): `op read` was found to hang indefinitely under
    # OP_SERVICE_ACCOUNT_TOKEN — the same hang class that separately took the
    # Hermes gateway down for hours the same day. Standing directive: never shell
    # out to `op`; use the 1Password service-account SDK via op_sdk_resolve.py,
    # batch-resolved once up front rather than one `op` subprocess per key.
    # GEMINI_API_KEY lives in the "hermes-agent" vault, everything else in
    # "Dev Toolbox/dev" (transitional — see brain conventions doc).
    _OP_REF = {"GEMINI_API_KEY": "op://hermes-agent/Gemini API Credentials/credential"}
    _OP_SECRET_KEYS = ("OPENAI_API_KEY_HERMES", "ANTHROPIC_API_KEY_HERMES",
                       "GH_API_KEY_HERMES", "ZAI_API_KEY_HERMES", "GEMINI_API_KEY")
    _op_secret_refs = {k: _OP_REF.get(k, f"op://Dev Toolbox/dev/{k}") for k in _OP_SECRET_KEYS}
    try:
        import importlib.util as _ilu
        _osr_path = os.path.join(os.path.dirname(__file__), "op_sdk_resolve.py")
        _osr_spec = _ilu.spec_from_file_location("op_sdk_resolve", _osr_path)
        _osr = _ilu.module_from_spec(_osr_spec)
        _osr_spec.loader.exec_module(_osr)
        _op_secret_values_by_ref = _osr.resolve_refs(list(_op_secret_refs.values()))
    except Exception as _osr_err:
        eprint(f"[opencode_exec] provider-key resolve via SDK failed (fail-open): {_osr_err!r}")
        _op_secret_values_by_ref = {}

    def _op_secret(key):
        """Look up one pre-resolved secret; empty string if resolution failed."""
        ref = _op_secret_refs.get(key, f"op://Dev Toolbox/dev/{key}")
        return _op_secret_values_by_ref.get(ref, "") or os.environ.get(key, "")

    openai_key    = _op_secret("OPENAI_API_KEY_HERMES")    or os.environ.get("OPENAI_API_KEY_HERMES", "")
    anthropic_key = _op_secret("ANTHROPIC_API_KEY_HERMES") or os.environ.get("ANTHROPIC_API_KEY_HERMES", "")
    # Mint a fresh GitHub App installation token (1h TTL) instead of using the
    # static PAT. Falls back to GH_API_KEY_HERMES only if minting fails (safety net
    # during the transition period; can be removed once the PAT is decommissioned).
    gh_token = ""
    try:
        r = subprocess.run(
            ["python3", os.path.expanduser("~/.hermes/scripts/github_app_token.py")],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            gh_token = r.stdout.strip()
    except Exception:
        pass
    if not gh_token:
        gh_token = os.environ.get("GH_TOKEN", "") or _op_secret("GH_API_KEY_HERMES") or os.environ.get("GH_API_KEY_HERMES", "")
    # GLM-4.7 code-writer (zai-coding provider) + Gemini content fallback.
    zai_key       = (_op_secret("ZAI_API_KEY_HERMES") or os.environ.get("ZAI_API_KEY_HERMES", "")
                     or os.environ.get("ZAI_API_KEY", ""))
    gemini_key    = _op_secret("GEMINI_API_KEY")          or os.environ.get("GEMINI_API_KEY", "")

    # GLM key guard (H2): if ZAI_API_KEY is empty, drop zai-coding tiers from the
    # cascade so failover skips straight to gpt-5 (a 401-after-launch would burn the
    # whole timeout). An EXPLICIT --model zai-coding/* with no key is a hard error.
    if not zai_key:
        if explicit_model and args.model.startswith("zai-coding/"):
            print(json.dumps({"ok": False, "task_id": args.task_id, "model": args.model,
                              "writer_cascade": writer_cascade,
                              "error": "ZAI_API_KEY empty (ZAI_API_KEY_HERMES missing) — GLM writer "
                                       "unavailable. Set the key, or pick another --model."}))
            return 3
        filtered = [m for m in cascade_models if not m.startswith("zai-coding/")]
        if filtered and filtered != cascade_models:
            eprint(f"[opencode_exec] ZAI_API_KEY empty → dropping GLM tier(s); cascade now {filtered}")
            cascade_models = filtered
            args.model = cascade_models[0]

    # Explicit minimal allowlist — OpenCode gets ONLY these variables.
    # No DATABASE_URL, no CLOUDFLARE_API_TOKEN, no Meta/MS/Gmail tokens.
    child_env = {
        # System essentials
        "PATH":    os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME":    os.environ.get("HOME", os.path.expanduser("~")),
        "TMPDIR":  os.environ.get("TMPDIR", "/tmp"),
        "LANG":    os.environ.get("LANG", "en_US.UTF-8"),
        "TERM":    os.environ.get("TERM", "xterm-256color"),
        # Provider keys OpenCode needs (mapped to canonical names)
        "OPENAI_API_KEY":    openai_key,
        "ANTHROPIC_API_KEY": anthropic_key,
        "ZAI_API_KEY":       zai_key,      # zai-coding provider (opencode.jsonc {env:ZAI_API_KEY})
        "GEMINI_API_KEY":    gemini_key,   # google provider (content fallback)
        "GH_TOKEN":          gh_token,
        # Writer model cascade control (non-sensitive flags from parent env)
        "HERMES_WRITER_OPENAI":     os.environ.get("HERMES_WRITER_OPENAI", "1"),
        "HERMES_WRITER_ANTHROPIC":  os.environ.get("HERMES_WRITER_ANTHROPIC", ""),
        "HERMES_WRITER_GLM":        os.environ.get("HERMES_WRITER_GLM", ""),
        "HERMES_WRITER_CODEX":      os.environ.get("HERMES_WRITER_CODEX", ""),  # Codex OAuth (default OFF)
        "HERMES_CONTENT_SONNET":    os.environ.get("HERMES_CONTENT_SONNET", ""),
        "HERMES_CONTENT_GEMINI":    os.environ.get("HERMES_CONTENT_GEMINI", ""),
        # OpenCode config (non-sensitive)
        "OPENCODE_DISABLE_CLAUDE_CODE":        "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_VARIANT":                    args.variant or "",
        # The untrusted task prompt — passed as env, NOT argv
        "OC_PROMPT": prompt,
    }

    # ── Run with runtime FAILOVER ───────────────────────────────────────────
    # Try each enabled cascade tier in order. A HARD failure (timeout, error event,
    # or non-zero rc — e.g. GLM looping on a tool-call error) auto-falls to the NEXT
    # tier instead of returning a failure the executor re-loops on forever. A clean
    # run that simply made no changes is NOT a hard failure (retrying won't help).
    # On every failover, emit a 'fallback: <primary> quota exhausted, serving via <next>'
    # receipt to ~/.hermes/logs/fallback-receipts.jsonl so quota-driven spend is
    # visible in real time (per 86e260vnu, 2026-07-05).
    run = None
    failover_tried = []
    used_model = cascade_models[0]
    base_log = log_path
    # 86e2d6vjj item 2: stamp Worked-By=Hermes once, at the choke point where the
    # executor commits to running this (possibly unclaimed) task. Best-effort.
    _stamp_worked_by_hermes(args.task_id)
    for i, model in enumerate(cascade_models):
        attempt_log = base_log if i == 0 else base_log.replace(".jsonl", f".try{i + 1}.jsonl")
        variant = _variant_for(model, args.variant if explicit_variant else "")
        run = _run_once(model, variant, child_env, workdir, args.opencode_bin,
                        args.timeout, attempt_log, args.task_id, writer_cascade)
        if run.get("launch_error"):
            print(json.dumps({"ok": False, "error": f"failed to launch sh/opencode: {run['launch_error']}"}))
            return 3
        used_model = model
        hard_fail = (run["timed_out"] or run["saw_error"] is not None or run["rc"] not in (0, None))
        # Quota-fallback detection (86e260vnu): if the failed tier's stderr tail
        # looks like a 429 / rate-limit / quota-exhaustion signal AND a next tier
        # exists, mark this as a quota-driven fallback (not a generic failure).
        quota_exhausted = False
        if hard_fail and i + 1 < len(cascade_models):
            tail = (run.get("stderr_tail") or "") + " " + json.dumps(run.get("saw_error") or {})
            tlow = tail.lower()
            quota_exhausted = any(s in tlow for s in (
                "429", "rate limit", "rate_limit", "rate-limit",
                "quota exhausted", "quota_exhausted", "quota-exhausted",
                "usage limit", "insufficient_quota", "billing", "overloaded",
            ))
            reason = "quota exhausted" if quota_exhausted else "hard failure"
            _emit_fallback_receipt(task_id=args.task_id, primary=model,
                                   next_model=cascade_models[i + 1],
                                   reason=reason, rc=run["rc"],
                                   stderr_tail=run.get("stderr_tail", ""))
            eprint(f"[opencode_exec] {model} {reason.upper()} "
                   f"(timeout={run['timed_out']} err={bool(run['saw_error'])} rc={run['rc']}) "
                   f"→ FAILOVER to {cascade_models[i + 1]} [receipt emitted]")
        failover_tried.append({"model": model, "rc": run["rc"], "timed_out": run["timed_out"],
                               "error": bool(run["saw_error"]), "hard_fail": hard_fail,
                               "quota_exhausted": quota_exhausted})
        if not hard_fail:
            break
        if i + 1 < len(cascade_models) and not quota_exhausted:
            # Generic hard-fail message — quota-failures already logged a richer
            # 'reason.upper() ... FAILOVER ... [receipt emitted]' line above.
            eprint(f"[opencode_exec] {model} HARD-FAILED "
                   f"(timeout={run['timed_out']} err={bool(run['saw_error'])} rc={run['rc']}) "
                   f"→ FAILOVER to {cascade_models[i + 1]}")

    args.model = used_model
    texts = run["texts"]
    final = run["final"]
    saw_error = run["saw_error"]
    timed_out = run["timed_out"]
    stderr_tail = run["stderr_tail"]
    elapsed = run["elapsed"]
    log_path = run["log_path"]
    rc = run["rc"]

    # Did OpenCode actually change anything? It can land work in TWO ways:
    #   (a) UNCOMMITTED edits  -> `git status --porcelain` is non-empty (dirty worktree)
    #   (b) COMMITTED on the agent branch -> worktree is CLEAN but the branch is
    #       ahead of the base. (OpenCode now sometimes commits its own work; the
    #       original check only saw (a), so committed work was reported as
    #       "no changes", the push/PR was skipped, and real commits were stranded
    #       on the local branch — verified 2026-06-23 on 86e1z31j5.)
    dirty = False
    changed = []
    try:
        st = subprocess.run(["git", "-C", workdir, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
        changed = [l for l in st.stdout.splitlines() if l.strip()]
        dirty = bool(changed)
    except Exception as e:
        eprint(f"[opencode_exec] git status failed: {e}")

    # (b) commits ahead of the base branch (committed-but-not-pushed work)
    commits_ahead = 0
    committed_files = []
    base = None
    try:
        r = subprocess.run(["git", "-C", workdir, "symbolic-ref", "-q", "--short",
                            "refs/remotes/origin/HEAD"], capture_output=True, text=True, timeout=15)
        base = (r.stdout or "").strip() or None  # e.g. "origin/main"
    except Exception:
        pass
    for cand in ([base] if base else []) + ["origin/main", "origin/master", "main", "master"]:
        if not cand:
            continue
        try:
            r = subprocess.run(["git", "-C", workdir, "rev-list", "--count", f"{cand}..HEAD"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                commits_ahead = int(r.stdout.strip())
                if commits_ahead:
                    df = subprocess.run(["git", "-C", workdir, "diff", "--name-only", f"{cand}..HEAD"],
                                        capture_output=True, text=True, timeout=15)
                    committed_files = [l for l in df.stdout.splitlines() if l.strip()]
                base = cand
                break
        except Exception:
            continue

    # "Wrote changes" = uncommitted edits OR commits ahead of base.
    wrote_changes = dirty or commits_ahead > 0
    if not changed and committed_files:
        changed = committed_files

    # NON-GIT (DB-publish) success path (Agent A2 2026-06-24).
    # DB-backed content tasks (dynamics365group.com blog rewrites) run OpenCode in a
    # plain, NON-git workdir (e.g. /tmp/ignite-<id>) and the deliverable is a written
    # file — `fields.json` — that is then fed to db_apply.py; there is no repo, no
    # commit, no PR. The git-diff success check above ALWAYS sees "no changes" for
    # such a workdir (git status/rev-list fail → dirty=False, commits_ahead=0), so a
    # PERFECTLY GOOD run was reported as ok:false and the executor aborted BEFORE
    # db_apply — the [Blog rewrite] backlog conversion killer (verified 2026-06-24:
    # task 86e1z6ada wrote a valid 10.2KB fields.json, reason=stop, yet opencode_exec
    # returned "made no file changes"). FIX: if the workdir is NOT a git repo, detect
    # success by the presence of a non-empty deliverable file instead of git diff.
    is_git_workdir = os.path.isdir(os.path.join(workdir, ".git"))
    if not is_git_workdir:
        try:
            r = subprocess.run(["git", "-C", workdir, "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=15)
            is_git_workdir = (r.returncode == 0 and r.stdout.strip() == "true")
        except Exception:
            is_git_workdir = False
    deliverables = []
    if not is_git_workdir:
        # Prefer the canonical DB-publish deliverable; fall back to any non-empty
        # output file OpenCode wrote (sources.txt/summary.txt are sidecars).
        for cand in ("fields.json", "fields_clean.json"):
            p = os.path.join(workdir, cand)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                deliverables.append(cand)
        if not deliverables:
            try:
                for fn in sorted(os.listdir(workdir)):
                    fp = os.path.join(workdir, fn)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        deliverables.append(fn)
            except Exception:
                pass
        if deliverables:
            wrote_changes = True
            if not changed:
                changed = deliverables

    tokens = (final or {}).get("tokens") if isinstance(final, dict) else None
    cost = (final or {}).get("cost") if isinstance(final, dict) else None
    reason = (final or {}).get("reason") if isinstance(final, dict) else None

    result = {
        "ok": False,
        "task_id": args.task_id,
        "model": args.model,
        "writer_cascade": writer_cascade,
        "failover_tried": failover_tried,
        # 86e260vnu: surface the FIRST quota-driven failover receipt for the
        # closeout comment + per-provider spend meter. None if no quota failovers.
        "quota_fallback_primary": next(
            (t["model"] for t in failover_tried if t.get("quota_exhausted")),
            None,
        ),
        "quota_fallback_serving": next(
            (cascade_models[i + 1] for i, t in enumerate(failover_tried)
             if t.get("quota_exhausted") and i + 1 < len(cascade_models)),
            None,
        ),
        "elapsed_s": elapsed,
        "returncode": rc,
        "stop_reason": reason,
        "cost_usd": cost,
        "tokens": tokens,
        "dirty": dirty,
        "commits_ahead": commits_ahead,
        "already_committed": commits_ahead > 0,
        "base_branch": base,
        "workdir_is_git": is_git_workdir,
        "deliverables": deliverables,
        # DB-publish signal: a non-git workdir whose deliverable is fields.json means
        # the executor should run db_apply.py (NO commit/PR) — see the SKILL's
        # DB-backed publish lane. mode is "git-pr" for repo workdirs.
        "mode": ("db-publish" if (not is_git_workdir and "fields.json" in deliverables) else "git-pr"),
        "db_publish": (not is_git_workdir and "fields.json" in deliverables),
        "fields_file": (os.path.join(workdir, "fields.json")
                        if (not is_git_workdir and "fields.json" in deliverables) else None),
        "files_changed": len(changed),
        "changed_sample": changed[:20],
        "log": log_path,
        "final_text_tail": _strip_think("".join(texts))[-1200:],
    }

    if timed_out:
        result["error"] = f"timeout after {args.timeout}s"
        _record_served(result, _writer_armed)  # WRITER-LIVENESS (2026-06-25)
        _record_attempt(args.task_id, "crash", result["error"])  # 86e2ddcpb retry cap
        print(json.dumps(result))
        return 3
    if saw_error is not None:
        # Strip any leaked <think> tags before this becomes a ClickUp diagnostic (H1).
        result["error"] = _strip_think(json.dumps(saw_error))[:1500]
        _record_served(result, _writer_armed)  # WRITER-LIVENESS (2026-06-25)
        _record_attempt(args.task_id, "crash", result["error"])  # 86e2ddcpb retry cap
        print(json.dumps(result))
        return 3
    if rc not in (0, None):
        result["error"] = f"opencode exited rc={rc}; stderr_tail={stderr_tail[-800:]}"
        _record_served(result, _writer_armed)  # WRITER-LIVENESS (2026-06-25)
        _record_attempt(args.task_id, "crash", result["error"])  # 86e2ddcpb retry cap
        print(json.dumps(result))
        return 3
    if not wrote_changes:
        if is_git_workdir:
            result["error"] = "opencode finished but made no file changes (clean worktree AND no commits ahead of base) — do NOT open an empty PR"
        else:
            result["error"] = ("opencode finished but wrote no deliverable file in the non-git workdir "
                               "(expected fields.json or another non-empty output) — nothing to publish")
        _record_served(result, _writer_armed)  # WRITER-LIVENESS (2026-06-25)
        _record_attempt(args.task_id, "fail", result["error"])  # 86e2ddcpb retry cap
        print(json.dumps(result))
        return 2

    # Success. If OpenCode already COMMITTED (commits_ahead>0, clean worktree), the
    # executor should just PUSH the branch — it does NOT need to re-commit. If the
    # worktree is dirty, the executor commits then pushes as before.
    result["ok"] = True
    if args.content:
        # Citation-link coverage is observed at the writer choke point while the
        # changed piece still exists locally.  The content-free receipt is later
        # joined to research severity + validator verdict by ClickUp task id.
        result["content_outcome"] = _record_content_outcome(
            args.task_id, workdir, committed_files, deliverables
        )
    _record_served(result, _writer_armed)  # WRITER-LIVENESS (2026-06-25)
    _record_attempt(args.task_id, "success")  # 86e2ddcpb retry cap
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
