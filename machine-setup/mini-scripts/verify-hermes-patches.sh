#!/usr/bin/env bash
# verify-hermes-patches.sh
# ---------------------------------------------------------------------------
# Idempotent guard for Hermes local hand-patches that `hermes update` reverts.
#
# WHY THIS EXISTS
#   Originally: a set of hand-patches lived as UNCOMMITTED working-tree edits to
#   files upstream NousResearch/hermes-agent changed frequently, at a mutable
#   $HOME/.hermes/hermes-agent git checkout. `hermes update` auto-stashed local
#   changes, pulled, then `git stash apply`; a conflict (common, hot files) ran
#   `git reset --hard HEAD` and left the edits only in a dangling stash — so
#   patches silently vanished from the working tree. This script re-applied
#   them idempotently and verified the running services actually loaded them.
#
# 2026-07-19 DEPLOY MODEL CHANGE (ClickUp 86e2e7z2h)
#   The mini migrated to immutable $HOME/.hermes/releases/vX.Y.Z-<sha>/ dirs +
#   a runtime-current symlink swap — there is no mutable git checkout to run
#   `hermes update`/git-stash-apply against any more, so the original failure
#   mode this script guards against no longer exists in that form. The same
#   day's home-directory data-loss incident (86e2ddcpb) also permanently lost
#   the original *.patch diff files in $HOME/.hermes/local-patches (never
#   committed anywhere — they were working-tree-only by design — and never in
#   restic's backup scope; unrecoverable, confirmed during 86e2a99q9/86e2e7z2h).
#   Every one of the 12 EXPECTED_PATCHES below has since been reconciled into a
#   real, formally merged commit on main (git log confirms each), so the fix
#   ships with every release cut regardless of the lost .patch files. Section 1
#   below therefore verifies each patch by SENTINEL — a load-bearing string
#   grepped from the live release checkout — not by the now-impossible
#   "does the lost .patch file still apply" check. If a raw .patch file does
#   turn up in PATCH_DIR (e.g. a future hand-patch before its PR lands), the
#   git-apply re-application path is still exercised for it.
#
# WHEN TO RUN
#   - After every mini release re-cut (manually, or wire as a post-cut step)
#   - Any time the dashboard/executor behaves like a patch reverted
#
# SAFETY
#   - Lives OUTSIDE the repo (~/.hermes/scripts) so it survives updates.
#   - Patch sources (when present) live in ~/.hermes/local-patches (also
#     outside the repo/release tree).
#   - Re-application (when a .patch file exists) uses `git apply --3way`; on
#     conflict it STOPS LOUDLY and leaves a .rej trail rather than silently
#     resetting. Nothing is forced.
#   - Read-only by default; pass --apply to actually re-apply missing patches,
#     and --restart to kickstart services if anything changed.
# ---------------------------------------------------------------------------
set -uo pipefail

# REPO resolves to the mini's current immutable release checkout (a real git
# repo per commit — confirmed during 86e2a99q9), NOT the old mutable
# $HOME/.hermes/hermes-agent checkout, which no longer exists post-2026-07-19.
# HERMES_AGENT_DIR still wins if set, for a dev box that predates the migration.
REPO="${HERMES_AGENT_DIR:-$(readlink -f "$HOME/.hermes/runtime-current" 2>/dev/null || true)}"
if [ -z "$REPO" ] || [ ! -d "$REPO" ]; then
  REPO="$HOME/.hermes/hermes-agent"  # last-resort legacy fallback; will fail loudly below if also absent
fi
PATCH_DIR="${HERMES_PATCH_DIR:-$HOME/.hermes/local-patches}"
JOBS_JSON="$HOME/.hermes/cron/jobs.json"
# 5am daily clickup-executor; expects max_turns=200. Job IDs can regenerate on
# edit — override with HERMES_EXECUTOR_JOB_ID if the check starts reporting MISSING.
EXECUTOR_JOB_ID="${HERMES_EXECUTOR_JOB_ID:-62714b869845}"
UID_NUM="$(id -u)"

APPLY=0; RESTART=0
for a in "$@"; do
  case "$a" in
    --apply)   APPLY=1 ;;
    --restart) RESTART=1 ;;
    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
  esac
done

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()   { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

FAIL=0; CHANGED=0

cd "$REPO" || { red "Repo not found: $REPO"; exit 2; }

# --- 1. Patch application state -------------------------------------------
hdr "1. Patch application state"

# P3 — expected-patch manifest: a deleted/renamed .patch file is silent.
# If ANY name from this list is absent, fail loudly before iterating.
EXPECTED_PATCHES=(
  "01-error_classifier-billing-400.patch"
  "02-run_agent-credpool-clobberfix.patch"
  "03-scheduler-partial-and-jobmaxturns.patch"
  "04-skill-manage-stub-guard.patch"
  "05-slack-decision-thread-hook.patch"
  # 06-anthropic-oauth-login-token-endpoint RETIRED 2026-06-24: subsumed by
  # upstream v0.17.0 (anthropic_adapter.py now natively iterates
  # _OAUTH_TOKEN_URLS = platform.claude.com → console.anthropic.com). The stale
  # patch file lives in local-patches/retired-pre-v0.17.0/.
  "20-background-review-size-discipline.patch"
  "21-tirith-nul-byte-block.patch"
  "22-slack-watchdog-selfheal.patch"
  # 23 (2026-06-24): scheduler preflight — when a cron job PINS a provider but the
  # resolved runtime has NO api key, fail LOUD instead of sliding into agent_init's
  # #17929 fallback (which builds anthropic as an OpenAI-compat client → 404-loops
  # silently). Root cause of the 2026-06-24 clickup-executor outage (keyless boot
  # before doppler exported OPENAI_API_KEY). See learnings/2026-06-24 …404 outage.
  "23-scheduler-pinned-provider-keyguard.patch"
  # 24 (2026-06-25): gemini restore_primary key refresh — re-resolve GEMINI_API_KEY
  # from live env in create_openai_client's gemini branch so a stale _primary_runtime
  # snapshot key can't persist into restore_primary. Root cause of the 2026-06-25
  # "API key not valid" HTTP 400 on the fallback/restore path.
  "24-gemini-restore-primary-key-refresh.patch"
  # 25 RESERVED-BUT-NEVER-LANDED (per brain) — number skipped intentionally.
  # 26 (2026-06-25): codex-proxy-writer — proxy upstream adapter `openai-codex`
  # exposing the ChatGPT Codex OAuth backend (gpt-5.4) as a local OpenAI-compat
  # endpoint (:8646) so OpenCode can WRITE code through the subscription-flat
  # backend (default-OFF behind HERMES_WRITER_CODEX). Touches 4 proxy files incl.
  # the NEW hermes_cli/proxy/adapters/openai_codex.py.
  "26-codex-proxy-writer.patch"
  # 27 (2026-06-25): opencode_exec resolves HERMES_WRITER_CODEX from Doppler — lives in
  # ~/.hermes/scripts/ (NOT the hermes-agent repo), so it is verified by the §18b-codex
  # SENTINEL (grep 'HERMES-PATCH 27'), not this git-apply manifest. The .patch file
  # local-patches/27-opencode-writer-codex-doppler-resolve.patch is the manual-reapply
  # artifact. Root cause: the subprocess sanitizer scrubs bare HERMES_WRITER_* vars, so
  # the executor-spawned opencode_exec never saw the armed flag → gpt-5.4 silently
  # skipped on every real write (all writes fell to glm-5.2). See learnings/2026-06-25.
  # 28 (2026-06-25): reload_env boot-env protection — _PROCESS_BOOT_ENV_KEYS snapshot
  # at import time; deletion loop skips keys present at boot. Root cause of the
  # 2026-06-25 gateway keyless-resolution stall (ZAI_API_KEY/GLM_API_KEY wiped by
  # /reload RPC). Verified by §28 sentinel grep.
  "28-reload-env-boot-env-protection.patch"
)
# Sentinel-first verification (see 2026-07-19 deploy-model note in the script
# header): each EXPECTED_PATCHES entry is checked against a load-bearing string
# in the LIVE release checkout first, regardless of whether its original .patch
# diff file still exists in PATCH_DIR (all 12 were lost + confirmed
# unrecoverable in 86e2e7z2h). A .patch file, if one does exist for an entry
# (e.g. a future hand-patch before its PR lands), still gets the git-apply
# re-application path on sentinel-absent.
patch_sentinel_present() {
  case "$1" in
    01-error_classifier-billing-400.patch)
      grep -Fq '"out of extra usage" in error_msg' "$REPO/agent/error_classifier.py" 2>/dev/null ;;
    02-run_agent-credpool-clobberfix.patch)
      grep -Fq 'getattr(_current, "auth_type", None) != AUTH_TYPE_OAUTH' "$REPO/run_agent.py" 2>/dev/null ;;
    03-scheduler-partial-and-jobmaxturns.patch)
      grep -q 'PARTIAL' "$REPO/cron/scheduler.py" 2>/dev/null \
        && grep -q 'max_turns' "$REPO/cron/scheduler.py" 2>/dev/null ;;
    04-skill-manage-stub-guard.patch)
      grep -Fq 'skill_manage stub-guard' "$REPO/tools/skill_manager_tool.py" 2>/dev/null ;;
    05-slack-decision-thread-hook.patch)
      grep -Fq '# HERMES-PATCH 05: slack-decision-thread-hook' "$REPO/plugins/platforms/slack/adapter.py" 2>/dev/null ;;
    20-background-review-size-discipline.patch)
      grep -Fq '⚠️ SIZE DISCIPLINE (hard rule)' "$REPO/agent/background_review.py" 2>/dev/null ;;
    21-tirith-nul-byte-block.patch)
      grep -Fq '# HERMES-PATCH: tirith-nul-byte-block' "$REPO/tools/tirith_security.py" 2>/dev/null ;;
    22-slack-watchdog-selfheal.patch)
      grep -Fq '_ensure_socket_watchdog' "$REPO/plugins/platforms/slack/adapter.py" 2>/dev/null ;;
    23-scheduler-pinned-provider-keyguard.patch)
      grep -Fq '# HERMES-PATCH 06 — pinned-provider credential preflight' "$REPO/cron/scheduler.py" 2>/dev/null ;;
    24-gemini-restore-primary-key-refresh.patch)
      grep -Fq '# HERMES-PATCH 24:' "$REPO/agent/agent_runtime_helpers.py" 2>/dev/null ;;
    26-codex-proxy-writer.patch)
      grep -Fq '# HERMES-PATCH 26: codex-proxy-writer' "$REPO/hermes_cli/proxy/adapters/openai_codex.py" 2>/dev/null \
        && grep -Fq 'HERMES-PATCH 26' "$REPO/hermes_cli/proxy/server.py" 2>/dev/null ;;
    28-reload-env-boot-env-protection.patch)
      grep -Fq 'HERMES-PATCH: reload_env boot-env protection' "$REPO/hermes_cli/config.py" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

missing_patches=0
for ep in "${EXPECTED_PATCHES[@]}"; do
  patchfile_present=0
  [ -f "$PATCH_DIR/$ep" ] && patchfile_present=1

  if patch_sentinel_present "$ep"; then
    if [ "$patchfile_present" -eq 1 ]; then
      grn "APPLIED    $ep (sentinel-verified; source .patch also present in $PATCH_DIR)"
    else
      grn "APPLIED    $ep (sentinel-verified against the live release; source .patch file lost 2026-07-19 + unrecoverable, see script header)"
    fi
    continue
  fi

  if [ "$patchfile_present" -eq 0 ]; then
    red "MISSING    $ep — sentinel absent AND no .patch file in $PATCH_DIR to reapply from. Needs a manual fix on main + a release re-cut, not --apply."
    FAIL=1; missing_patches=1
    continue
  fi

  # A .patch file exists for this entry but its sentinel is absent — genuinely
  # reverted (or a not-yet-landed hand-patch). `patch_failed` is per-patch so a
  # successful --apply clears it (must not leave the script exiting non-zero).
  p="$PATCH_DIR/$ep"
  patch_failed=1
  if git apply --check "$p" >/dev/null 2>&1; then
    ylw "MISSING    $ep (applies cleanly)"; mode=clean
  else
    ylw "MISSING    $ep (context drift — will try 3-way merge)"; mode=drift
  fi
  if [ "$APPLY" -eq 1 ]; then
    if [ "$mode" = clean ] && git apply "$p" >/dev/null 2>&1; then
      grn "  -> re-applied (clean)"; CHANGED=1; patch_failed=0
    elif git apply --3way "$p" 2>/dev/null && patch_sentinel_present "$ep"; then
      grn "  -> re-applied (3-way merge, sentinel verified)"; CHANGED=1; patch_failed=0
    else
      red "  -> RE-APPLY FAILED, or --3way claimed success but the sentinel is still missing post-apply. Resolve by hand:"
      red "     cd $REPO && git apply --3way --reject $p   # then fix any *.rej"
    fi
  fi
  [ "$patch_failed" -eq 1 ] && FAIL=1
done
if [ "$missing_patches" -eq 0 ]; then
  grn "manifest   all ${#EXPECTED_PATCHES[@]} expected patches verified live in $REPO (sentinel and/or file-backed)"
fi

# Orphan check: a .patch file present in PATCH_DIR that ISN'T one of
# EXPECTED_PATCHES (a new hand-patch that was never added to the manifest
# above, or a stale leftover) — informational only, never fails the run.
if [ -d "$PATCH_DIR" ]; then
  for p in "$PATCH_DIR"/*.patch; do
    [ -e "$p" ] || break
    name="$(basename "$p")"
    known=0
    for ep in "${EXPECTED_PATCHES[@]}"; do [ "$ep" = "$name" ] && known=1 && break; done
    [ "$known" -eq 0 ] && ylw "ORPHAN     $name present in $PATCH_DIR but not in EXPECTED_PATCHES — add it to the manifest or remove the stale file."
  done
fi

# --- 2. Syntax/import sanity of patched files ------------------------------
hdr "2. Python parse check (patched files)"
for f in agent/error_classifier.py run_agent.py cron/scheduler.py tools/skill_manager_tool.py plugins/platforms/slack/adapter.py agent/agent_runtime_helpers.py hermes_cli/proxy/adapters/openai_codex.py hermes_cli/proxy/adapters/base.py hermes_cli/proxy/server.py hermes_cli/config.py; do
  if "$REPO/venv/bin/python" -c "import ast,sys; ast.parse(open('$REPO/$f').read())" 2>/dev/null; then
    grn "parse-ok   $f"
  else
    red "PARSE FAIL $f — patch left the file unparseable"; FAIL=1
  fi
done

# --- 2a. Behavioral sentinel: billing-400 credential-rotation patches --------
# P1: patches 01 + 02 are the most load-bearing (they fix Anthropic sub-exhaust
# failover). git apply + ast.parse alone can't catch an upstream refactor that
# lands the hunk in the wrong function, inert. Test the ACTUAL BEHAVIOR:
#   patch 01: a 400 + "out of extra usage" MUST classify as billing+rotate
#   patch 02: the auth_type guard must be structurally present BEFORE
#             resolve_anthropic_token in _try_refresh_anthropic_client_credentials
hdr "2a. Behavioral sentinel (billing-400 rotation — patches 01+02)"
HOOK_PY_EARLY="$REPO/venv/bin/python"
# Sentinel A: error_classifier classify 400+"out of extra usage" → billing, should_rotate_credential=True.
# classify_api_error() takes an Exception (not keyword args); build a minimal fake HTTP error.
ec_result=$(cd "$REPO" && "$HOOK_PY_EARLY" - "$REPO" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1])
from agent.error_classifier import classify_api_error, FailoverReason
class _E(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.status_code = code; self.body = msg
r = classify_api_error(
    _E(400, "You're out of extra usage. Add more at claude.ai/settings/usage and keep going."),
    provider="anthropic",
)
ok = (r.reason == FailoverReason.billing and r.should_rotate_credential is True)
print("OK" if ok else f"BROKEN(reason={r.reason},rotate={r.should_rotate_credential})")
PY
)
if [ "$ec_result" = "OK" ]; then
  grn "sentinel   error_classifier: 400+'out of extra usage' → billing+rotate=True (patch 01 functional)"
else
  red "sentinel   error_classifier BROKEN for billing-400 path ($ec_result) — patch 01 is inert or mislocated!"
  red "           Anthropic sub-exhaust will NOT rotate to the api-key fallback. Re-run: --apply --restart"; FAIL=1
fi

# Sentinel B: run_agent _try_refresh_anthropic_client_credentials contains the
# auth_type guard (compare node) AT A LOWER LINE NUMBER than the
# resolve_anthropic_token import — proving the hunk is in the right function and
# the right order (guard short-circuits before the refresh clobbers the pool key).
ra_result=$(cd "$REPO" && "$HOOK_PY_EARLY" - "$REPO/run_agent.py" <<'PY' 2>/dev/null
import ast, sys
src = open(sys.argv[1]).read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
       and node.name == '_try_refresh_anthropic_client_credentials':
        guard_line = None; resolver_line = None
        for s in ast.walk(node):
            if isinstance(s, ast.Compare) and hasattr(s, 'lineno'):
                txt = ast.unparse(s)
                if 'auth_type' in txt and 'AUTH_TYPE_OAUTH' in txt:
                    guard_line = s.lineno
            if isinstance(s, ast.ImportFrom) and hasattr(s, 'lineno'):
                txt = ast.unparse(s)
                if 'resolve_anthropic_token' in txt:
                    resolver_line = s.lineno
        ok = guard_line is not None and resolver_line is not None and guard_line < resolver_line
        print("OK" if ok else f"BROKEN(guard_line={guard_line},resolver_line={resolver_line})")
        sys.exit(0)
print("BROKEN(function _try_refresh_anthropic_client_credentials not found)")
PY
)
if [ "$ra_result" = "OK" ]; then
  grn "sentinel   run_agent: auth_type guard precedes resolve_anthropic_token in _try_refresh_anthropic_client_credentials (patch 02 structurally in-place)"
else
  red "sentinel   run_agent patch 02 BROKEN or mislocated ($ra_result) — subscription→api-key rotation will be clobbered on per-create refresh!"; FAIL=1
fi

# --- 2b. Sentinel check: slack-decision-thread-hook present ----------------
# The patch loop above re-applies 05-slack-decision-thread-hook.patch by content;
# this is a fast behavioral tripwire that the unique marker is actually in the
# running file (catches a partial/failed re-apply that left the diff out).
# v0.17.0 moved the Slack adapter from gateway/platforms/slack.py to
# plugins/platforms/slack/adapter.py (platforms→plugins refactor, 2026-06-24).
SLACK_ADAPTER="plugins/platforms/slack/adapter.py"
SLACK_HOOK_SENTINEL="# HERMES-PATCH 05: slack-decision-thread-hook"
if grep -Fq "$SLACK_HOOK_SENTINEL" "$REPO/$SLACK_ADAPTER" 2>/dev/null; then
  grn "sentinel   slack-decision-thread-hook present in $SLACK_ADAPTER"
else
  red "sentinel   slack-decision-thread-hook MISSING in $SLACK_ADAPTER — the Slack→ClickUp"
  red "           decision round-trip hook reverted; re-run with --apply (05-*.patch)"; FAIL=1
fi

# --- 3. Data wiring: executor job carries max_turns ------------------------
hdr "3. Job-record wiring (per-job max_turns)"
mt="$("$REPO/venv/bin/python" - "$JOBS_JSON" "$EXECUTOR_JOB_ID" <<'PY' 2>/dev/null
import json,sys
jobs=json.load(open(sys.argv[1])); jl=jobs if isinstance(jobs,list) else jobs.get("jobs",[])
print(next((j.get("max_turns") for j in jl if isinstance(j,dict) and j.get("id")==sys.argv[2]),"MISSING"))
PY
)"
if [ "$mt" = "200" ]; then grn "executor job $EXECUTOR_JOB_ID max_turns=200"
else red "executor job $EXECUTOR_JOB_ID max_turns=$mt (expected 200) — scheduler Change A is inert"; FAIL=1; fi

# --- 4. Liveness: is the running process newer than the patched files? -----
# The cron tick imports cron.scheduler in-process; a file mtime AFTER the
# process start time means the running process holds stale code.
hdr "4. Liveness (running process vs patched-file mtime)"
newest_mtime=0
for f in agent/error_classifier.py run_agent.py cron/scheduler.py tools/skill_manager_tool.py; do
  m=$(stat -f %m "$REPO/$f"); [ "$m" -gt "$newest_mtime" ] && newest_mtime=$m
done
stale=0
for svc in ai.hermes.gateway com.colingreig.hermes-dashboard; do
  pid=$(launchctl list 2>/dev/null | awk -v s="$svc" '$3==s{print $1}')
  if [ -z "$pid" ] || [ "$pid" = "-" ]; then ylw "$svc not running"; continue; fi
  # Process start epoch. `ps -o lstart=` pads/spaces the field, so strip it.
  # macOS `date -j -f` needs the exact field order, which is LOCALE-dependent:
  # day-first ("Fri 12 Jun ...") on en_CA/most non-US, month-first on en_US.
  # Try both; if neither parses, report honestly rather than defaulting green.
  _raw=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//')
  pstart=$(date -j -f "%a %d %b %T %Y" "$_raw" +%s 2>/dev/null \
        || date -j -f "%a %b %d %T %Y" "$_raw" +%s 2>/dev/null)
  if [ -z "$pstart" ]; then
    ylw "$svc (pid $pid) — could not parse start time ('$_raw'); liveness UNKNOWN"
    continue
  fi
  if [ "$pstart" -lt "$newest_mtime" ]; then
    # Don't fold into FAIL here — section 5 may resolve it with a restart.
    # Only an UNremediated stale state (no/failed restart) is a real failure.
    red "$svc (pid $pid) started BEFORE newest patch — running STALE code (restart needed)"; stale=1
  else
    grn "$svc (pid $pid) newer than patches — live"
  fi
done

# --- 5. Optional restart if we changed code or services are stale ----------
if { [ "$CHANGED" -eq 1 ] || [ "$stale" -eq 1 ]; } && [ "$RESTART" -eq 1 ]; then
  hdr "5. Restarting services (loads patched code; drops active web TUI sessions)"
  for svc in ai.hermes.gateway com.colingreig.hermes-dashboard; do
    if launchctl kickstart -k "gui/$UID_NUM/$svc"; then grn "kickstart $svc"
    else red "kickstart FAILED for $svc — check: launchctl list | grep $svc"; FAIL=1; fi
  done
  # A successful kickstart replaces the process (new start time > file mtime),
  # so the stale condition is resolved. Confirm the dashboard actually serves
  # before we call it remediated — kickstart returning 0 doesn't prove a clean
  # import (a broken patch could crash on load).
  sleep 3
  _code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "http://127.0.0.1:9119/" 2>/dev/null)
  if [ "$_code" = "200" ]; then grn "dashboard healthy after restart (HTTP 200)"
  else red "dashboard NOT healthy after restart (HTTP ${_code:-none}) — check logs"; FAIL=1; fi
elif [ "$CHANGED" -eq 1 ] || [ "$stale" -eq 1 ]; then
  hdr "5. Restart needed"
  ylw "Patched code is on disk but the running process is stale."
  ylw "Re-run with --restart, or: launchctl kickstart -k gui/$UID_NUM/{ai.hermes.gateway,com.colingreig.hermes-dashboard}"
  ylw "(restart drops active dashboard web-TUI sessions)"
  FAIL=1   # unremediated stale/changed code is a real, actionable failure
fi

# --- 6. GitHub App token minting (replaces static PAT remap) -----------------
# The static fine-grained PAT (GH_API_KEY_HERMES) has been DECOMMISSIONED.
# Both launchd services now mint a fresh GitHub App installation token via
# github_app_token.py on each start. The App credentials live in Doppler:
#   GH_APP_PRIVATE_KEY, GH_APP_ID, GH_APP_INSTALLATION_ID
# The plist sh -c wrapper exports GH_TOKEN as the output of the token script.
# If a Hermes service reinstall regenerates the plists, the App-token call
# vanishes and gh fails OPEN to the full-access keyring token. Detect loudly.
# No auto-fix: a regenerated plist changes structure, so re-wrapping must be
# done by hand. Backups: ~/Library/LaunchAgents/*.bak-pre-ghtoken
# Design ref: shared brain decisions/2026-06-12 Hermes scoped GitHub PAT;
#             task 86e1vuzwb (GitHub App credential layer migration).
hdr "6. GitHub App token minting (github_app_token.py -> GH_TOKEN)"
for svc in ai.hermes.gateway com.colingreig.hermes-dashboard; do
  plist="$HOME/Library/LaunchAgents/$svc.plist"
  if [ ! -f "$plist" ]; then red "MISSING plist: $plist"; FAIL=1; continue; fi
  if grep -Fq 'github_app_token.py' "$plist"; then
    grn "App-token mint present  $svc.plist"
  else
    red "APP-TOKEN MINT MISSING in $svc.plist — gh will FAIL OPEN to the full-access keyring token!"
    red "  Fix: re-add  export GH_TOKEN=\"\$(python3 \"\$HOME/.hermes/scripts/github_app_token.py\" 2>/dev/null || echo '')\";  to the service's sh -c wrapper,"
    red "       then:  launchctl bootout gui/$UID_NUM/$svc ; launchctl bootstrap gui/$UID_NUM $plist"
    FAIL=1
  fi
  # Verify GH_API_KEY_HERMES is NOT referenced (decommissioned)
  if grep -Fq 'GH_API_KEY_HERMES' "$plist"; then
    red "STALE PAT REF in $svc.plist — GH_API_KEY_HERMES has been decommissioned!"
    red "  Fix: replace export GH_TOKEN=\"\$GH_API_KEY_HERMES\" with the App-token mint call above."
    FAIL=1
  fi
  # Live-env check: GH_TOKEN actually present in the RUNNING process env?
  pid=$(launchctl list 2>/dev/null | awk -v s="$svc" '$3==s{print $1}')
  if [ -z "$pid" ] || [ "$pid" = "-" ]; then ylw "$svc not running — live-env check skipped"; continue; fi
  if ps eww -p "$pid" 2>/dev/null | tr ' ' '\n' | grep -q '^GH_TOKEN='; then
    grn "GH_TOKEN live   $svc (pid $pid)"
  else
    red "GH_TOKEN NOT in live env of $svc (pid $pid) — reload: bootout + bootstrap $plist"; FAIL=1
  fi
done

# --- 6a. GitHub App credentials present in 1Password -------------------------
# The App token script reads GH_APP_PRIVATE_KEY, GH_APP_ID, GH_APP_INSTALLATION_ID
# from the environment (1Password injects them at gateway boot via op-secrets.env;
# Doppler decommissioned 2026-07-03). Verify they're set.
hdr "6a. GitHub App credentials in 1Password (GH_APP_*)"
_op_read() {
  local val
  val="$("$REPO/venv/bin/python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from op_sdk_resolve import resolve_refs; sys.stdout.write(resolve_refs([sys.argv[2]]).get(sys.argv[2], ""))' \
    "$HOME/.hermes/scripts" "op://Dev Toolbox/dev/$1" 2>/dev/null)"
  printf '%s' "$val"
}
APP_KEY=$(_op_read GH_APP_PRIVATE_KEY)
APP_ID=$(_op_read GH_APP_ID)
APP_INST=$(_op_read GH_APP_INSTALLATION_ID)
if [ -n "$APP_KEY" ] && [ -n "$APP_ID" ] && [ -n "$APP_INST" ]; then
  grn "1Password GH_APP_* secrets present (id=$APP_ID, inst=$APP_INST)"
else
  red "MISSING 1Password GH_APP_* secrets — App token minting will fail!"
  red "  Set: GH_APP_PRIVATE_KEY (PEM), GH_APP_ID, GH_APP_INSTALLATION_ID in op://Dev Toolbox/dev"
  FAIL=1
fi
# Also verify the old PAT is GONE (decommissioned) — Doppler itself is gone, so the
# durable signal now is absence from the 1Password item.
STALE_PAT=$(_op_read GH_API_KEY_HERMES)
if [ -z "$STALE_PAT" ]; then
  grn "GH_API_KEY_HERMES decommissioned (absent from 1Password)"
else
  ylw "GH_API_KEY_HERMES still present in 1Password — safe to delete now that App token minting is live"
fi

# --- 6b. ignite-marketplace sync cron (launchd plist loaded) -----------------
# The private mirror repo (colingreig/ignite-marketplace) keeps AMH skills
# current via an hourly launchd sync. If the plist is missing or the job is
# not loaded, Hermes loses the upstream-SHA safety net and will keep serving
# stale vendored content. Detect loudly; no auto-fix (a missing plist means
# someone re-installed the Hermes service bundle and dropped the marketplace
# cron). Design ref: shared brain 2026-06-20 AMH Skills 2/3 (ClickUp 86e1z37j4).
hdr "6b. ignite-marketplace sync cron (launchd plist loaded)"
MKT_PLIST="$HOME/Library/LaunchAgents/com.colingreig.ignite-marketplace-sync.plist"
MKT_WRAPPER="$HOME/.hermes/scripts/ignite-marketplace-sync.sh"
MKT_REPO="$HOME/Projects/ignite-marketplace"
if [ ! -f "$MKT_PLIST" ]; then
  red "MISSING plist: $MKT_PLIST"; FAIL=1
else
  if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "com.colingreig.ignite-marketplace-sync"; then
    grn "launchd loaded  com.colingreig.ignite-marketplace-sync"
  else
    red "launchd NOT loaded: com.colingreig.ignite-marketplace-sync — Hermes won't auto-sync AMH"
    red "  Fix: launchctl bootstrap gui/$UID_NUM $MKT_PLIST"
    FAIL=1
  fi
fi
[ -x "$MKT_WRAPPER" ] || { red "wrapper not executable: $MKT_WRAPPER"; FAIL=1; }
[ -d "$MKT_REPO" ]   || { red "repo missing: $MKT_REPO — re-clone from colingreig/ignite-marketplace"; FAIL=1; }

# --- 6c. Validator chain (openai-codex primary) + executor --------------------
# 2026-06-22: executor + validator migrated to OpenAI gpt-5-mini.
# 2026-06-25: validator chain updated to openai-codex:gpt-5.4 as primary tier
#   (flat-fee OAuth, silent-hang-safe). Full chain:
#     openai-codex:gpt-5.4  (ChatGPT OAuth, auto-refresh, ~/.hermes/auth.json)
#     openai-api:gpt-5.5    (api key path, 1.05M ctx)
#     minimax:MiniMax-M3    (unlimited fallback)
#     gemini:gemini-3.5-flash (last-resort)
# The wiring lives in TWO update-fragile places:
#   (a) gateway plist — the OpenAI key remap (mirrors GH_TOKEN §6) plus the
#       three VALIDATOR_*_CHAIN env literals. A plist regen drops the remap
#       (openai-api gets NO key) AND the chains (validator silently falls back).
#   (b) cron/jobs.json — the clickup-executor job's model/provider.
# This is a PLIST + JSON patch, NOT a git patch — `git apply` can't restore it.
# Detect LOUDLY, no auto-fix (a regenerated plist changes structure; re-wrapping
# by hand is safer than a blind PlistBuddy poke at secret/model routing).
# Backups: ~/Library/LaunchAgents/ai.hermes.gateway.plist.bak-*
# DURABILITY NOTE (2026-06-25): openai-codex uses OAuth (auth.json), NOT Doppler;
#   the chain can be set in Doppler claude-code/dev to survive plist regeneration.
# Design refs: brain operations/2026-06-22 "Hermes executor + validator migrated
#   to OpenAI gpt-5-mini"; brain 2026-06-25 openai-codex validator chain.
hdr "6c. Validator chain (openai-codex primary) + OPENAI_API_KEY remap + executor"
GW_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
EXPECT_CHAIN="openai-codex:gpt-5.4,minimax:MiniMax-M3,gemini:gemini-3.5-flash"
if [ ! -f "$GW_PLIST" ]; then
  red "MISSING plist: $GW_PLIST"; FAIL=1
else
  # (a1) OpenAI key remap in the sh -c wrapper
  if grep -Fq 'export OPENAI_API_KEY="$OPENAI_API_KEY_HERMES"' "$GW_PLIST"; then
    grn "OpenAI remap present  ai.hermes.gateway.plist"
  else
    red "OPENAI_API_KEY remap MISSING in ai.hermes.gateway.plist — openai-api provider gets NO key!"
    red "  Fix: re-add  export OPENAI_API_KEY=\"\$OPENAI_API_KEY_HERMES\";  to the sh -c wrapper,"
    red "       then:  launchctl bootout gui/$UID_NUM/ai.hermes.gateway ; launchctl bootstrap gui/$UID_NUM $GW_PLIST"
    FAIL=1
  fi
  # (a2) the three VALIDATOR_*_CHAIN env literals (2026-06-25: openai-codex:gpt-5.4 primary).
  # The canonical chain lives in EXPECT_CHAIN above and is AUTO-RESTORED under --apply:
  # PlistBuddy writes the stable EnvironmentVariables dict (structure-independent of any
  # ProgramArguments revert). A plist-env edit needs bootout+bootstrap (NOT kickstart)
  # to load, so 6c does its own reload under --restart. Backup: ai.hermes.gateway.plist.bak-6c-*
  PB=/usr/libexec/PlistBuddy
  chain_fixed=0; backed_up=0
  for tier in LOW MEDIUM HIGH; do
    cur=$("$PB" -c "Print :EnvironmentVariables:VALIDATOR_${tier}_CHAIN" "$GW_PLIST" 2>/dev/null)
    if [ "$cur" = "$EXPECT_CHAIN" ]; then
      grn "VALIDATOR_${tier}_CHAIN ok    ($EXPECT_CHAIN)"
    elif [ "$APPLY" -eq 1 ]; then
      if [ "$backed_up" -eq 0 ]; then
        cp "$GW_PLIST" "$GW_PLIST.bak-6c-$(date +%Y%m%d-%H%M%S)" && backed_up=1 && ylw "backed up gateway plist before chain restore"
      fi
      if "$PB" -c "Set :EnvironmentVariables:VALIDATOR_${tier}_CHAIN $EXPECT_CHAIN" "$GW_PLIST" 2>/dev/null \
         || "$PB" -c "Add :EnvironmentVariables:VALIDATOR_${tier}_CHAIN string $EXPECT_CHAIN" "$GW_PLIST" 2>/dev/null; then
        grn "VALIDATOR_${tier}_CHAIN -> restored ($EXPECT_CHAIN)"; chain_fixed=1; CHANGED=1
      else
        red "VALIDATOR_${tier}_CHAIN restore FAILED via PlistBuddy — set it by hand"; FAIL=1
      fi
    else
      red "VALIDATOR_${tier}_CHAIN MISSING/WRONG in plist (got '${cur:-<none>}') — validator not on openai-codex:gpt-5.4!"
      red "  Fix: re-run with --apply --restart (auto-restores), or set by hand + bootout/bootstrap"
      FAIL=1
    fi
  done
  # A plist EnvironmentVariables change only loads via bootout+bootstrap (the
  # epilogue's kickstart -k reuses the in-memory job spec). Do that reload here.
  if [ "$chain_fixed" -eq 1 ] && [ "$RESTART" -eq 1 ]; then
    if launchctl bootout "gui/$UID_NUM/ai.hermes.gateway" 2>/dev/null; launchctl bootstrap "gui/$UID_NUM" "$GW_PLIST" 2>/dev/null; then
      grn "reloaded gateway (bootout+bootstrap) to load restored chains"
    else
      red "gateway bootout+bootstrap FAILED after chain restore — reload by hand: launchctl bootstrap gui/$UID_NUM $GW_PLIST"; FAIL=1
    fi
  elif [ "$chain_fixed" -eq 1 ]; then
    ylw "chains restored on disk but NOT loaded — re-run with --restart (bootout+bootstrap), kickstart won't suffice"
  fi
  # live-env check: chains actually present (with the right value) in the RUNNING gateway
  gwpid=$(launchctl list 2>/dev/null | awk '$3=="ai.hermes.gateway"{print $1}')
  if [ -z "$gwpid" ] || [ "$gwpid" = "-" ]; then
    ylw "ai.hermes.gateway not running — live-env check skipped"
  elif ps eww -p "$gwpid" 2>/dev/null | tr ' ' '\n' | grep -Fxq "VALIDATOR_HIGH_CHAIN=$EXPECT_CHAIN"; then
    grn "VALIDATOR chains live  ai.hermes.gateway (pid $gwpid, value matches)"
  else
    red "VALIDATOR_*_CHAIN missing/stale in live env of ai.hermes.gateway (pid $gwpid) — reload: --apply --restart"; FAIL=1
  fi
fi
# (b) executor job model/provider in jobs.json
# 2026-06-29: Colin switched the main brain glm-5.2 → glm-4.7 (perf + to cut the
# 429 storm — glm-5.2's heavy reasoning-burn was overloading the z.ai Coding Plan;
# see brain operations/2026-06-29 GLM 429 root cause). config.yaml model.default is
# now glm-4.7 / zai. The executor (62714b869845) is healthy EITHER pinned glm-4.7|zai
# OR unpinned (null|null) inheriting that default — see scheduler.py (model =
# job.get("model") ... else _model_cfg.get("default")).
# NOTE: clickup-executor-2 (baa3251e033d) is DELIBERATELY split to gpt-5-mini|openai-api
# so the two parallel executors don't both hammer the single z.ai rate ceiling — that
# is the correct state for executor-2, NOT a regression. (This §6b only asserts the
# primary executor.) Do NOT "fix" either back to glm-5.2.
if [ -f "$JOBS_JSON" ]; then
  exline=$("$REPO/venv/bin/python3.11" - "$JOBS_JSON" "$EXECUTOR_JOB_ID" <<'PY' 2>/dev/null
import json,sys
d=json.load(open(sys.argv[1])); jid=sys.argv[2]
jobs=d if isinstance(d,list) else d.get("jobs",list(d.values()) if isinstance(d,dict) else d)
for j in jobs:
    if j.get("id")==jid:
        print(f"{j.get('model')}|{j.get('provider')}"); break
PY
)
  # config.yaml default model/provider the unpinned job inherits.
  cfgline=$("$REPO/venv/bin/python" - "$HOME/.hermes/config.yaml" <<'PY' 2>/dev/null
import yaml,sys
cfg=yaml.safe_load(open(sys.argv[1])) or {}
m=cfg.get("model") or {}
if isinstance(m,str):
    print(f"{m}|")
else:
    print(f"{m.get('default') or m.get('model')}|{m.get('provider')}")
PY
)
  if [ "$exline" = "None|None" ] && [ "$cfgline" = "glm-4.7|zai" ]; then
    grn "executor model ok     UNPINNED job inherits config.yaml default glm-4.7 / zai (job $EXECUTOR_JOB_ID)"
  elif [ "$exline" = "glm-4.7|zai" ]; then
    grn "executor model ok     pinned glm-4.7 / zai (job $EXECUTOR_JOB_ID)"
  else
    red "executor model WRONG in jobs.json/config: job='${exline:-<none>}' config.default='${cfgline:-<none>}' (job $EXECUTOR_JOB_ID)"
    red "  Expected: job null|null inheriting config.yaml model.default=glm-4.7 (provider zai), OR job pinned glm-4.7|zai."
    red "  Do NOT re-pin to glm-5.2 (the 06-29 429-storm cause); set config.yaml model.default=glm-4.7 / provider=zai."
    FAIL=1
  fi
else
  red "MISSING jobs.json: $JOBS_JSON"; FAIL=1
fi


# --- 6d. Skills pull freshness check ----------------------------------------
# Each monitored source gets its own freshness budget. Do NOT let one busy
# source or one shared audit file mask a stale sibling.
#
# NOTE: This is a passive CHECK, not an auto-pull. The actual pulls are handled by:
#   - com.colingreig.ignite-skills-pull (launchd) for ignite-skills-live (3-hourly)
#   - com.colingreig.pull_anthropic_skills (launchd) for anthropic-agent-skills (daily)
FRESHNESS_STATUS_AND_REPORT=$(python3 - <<'PY'
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

sources = [
    {
        "label": "ignite-skills-live",
        "log_path": Path.home() / ".hermes/logs/ignite-skills-pull.log",
        "cadence_hours": 3,
        "markers": (" OK:",),
    },
    {
        "label": "anthropic-agent-skills",
        "log_path": Path.home() / ".hermes/logs/anthropic-skills-pull.log",
        "cadence_hours": 24,
        "markers": ("pull OK",),
    },
]

iso_ts = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
local_ts = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [A-Z]{3}\b")


def parse_timestamp(line: str) -> datetime | None:
    match = local_ts.search(line)
    if match:
        try:
            parsed = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)

    match = iso_ts.search(line)
    if match:
        raw = match.group("ts").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    return None


stale_lines = []
missing_lines = []
now = datetime.now(timezone.utc)
for source in sources:
    log_path = source["log_path"]
    cadence_hours = source["cadence_hours"]
    markers = source["markers"]
    last_success = None

    if not log_path.is_file():
        missing_lines.append(f"{source['label']}: missing log {log_path}")
        continue

    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not any(marker in raw for marker in markers):
                continue
            ts = parse_timestamp(raw)
            if ts is None:
                continue
            if last_success is None or ts > last_success:
                last_success = ts

    threshold_hours = cadence_hours * 3
    if last_success is None:
        missing_lines.append(f"{source['label']}: no parseable success lines in {log_path}")
        continue

    hours_since = (now - last_success).total_seconds() / 3600.0
    if hours_since >= threshold_hours:
        stale_lines.append(
            f"{source['label']}: last success {hours_since:.1f} hours ago in {log_path} (threshold: {threshold_hours}h)"
        )

if stale_lines or missing_lines:
    print("STALE")
    for line in missing_lines + stale_lines:
        print(line)
else:
    print("OK")
PY
)
  FRESHNESS_STATUS=${FRESHNESS_STATUS_AND_REPORT%%$'\n'*}
  FRESHNESS_REPORT=${FRESHNESS_STATUS_AND_REPORT#*$'\n'}
  if [ "$FRESHNESS_STATUS" = "OK" ]; then
    grn "Skills pull freshness check passed for all monitored sources"
  else
    red "Skills pull freshness check failed"
    if [ -n "$FRESHNESS_REPORT" ]; then
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        red "  -> $line"
      done <<EOF
$FRESHNESS_REPORT
EOF
    fi
    FAIL=1
  fi

# --- 7. Skills bridge: external_dirs wired to the claude-skills library --------
# Hermes' own skill set (~/.hermes/skills) does NOT include the document skills
# (ignite-docx/pptx/xlsx + native docx/pdf/pptx/xlsx) or the rest of the Claude
# Code library. They reach Hermes via skills.external_dirs in ~/.hermes/config.yaml.
# This is a CONFIG patch (not a git patch / not a plist) — `hermes update` stashes
# REPO edits, so config.yaml should survive, but a config reinstall or a manual
# `hermes config` rewrite can drop it. Detect loudly; no auto-fix (YAML structure
# edits are done by hand). Design ref: shared brain architecture note
# "Hermes vs Claude Code skill libraries are disjoint — bridge via skills.external_dirs".
#
# FIX (2026-07-04, ClickUp 86e25qd33): this section previously hardcoded only 4 of
# the real (now 11) external_dirs as EXPECTED_DIRS, so it silently stopped covering
# 7 dirs added since (brain packs, ads/seo/blog marketplace plugins) — a config
# regression on any of those 7 would never have been caught. It also had NO check
# that the config's external_dirs actually make it into the runtime's skill
# snapshot — which is the real root cause of the 217->29 skill regression going
# unnoticed for days: config.yaml can list the right dirs while the snapshot
# builder still silently drops most of them. Two independent checks now run:
# (a) every dir currently configured resolves + exists on disk, faulting a
# hardcoded EXPECTED_DIRS floor if it shrinks; (b) the live snapshot JSON is
# parsed directly and asserted against a sentinel skill set + a count floor.
hdr "7. Skills bridge (skills.external_dirs)"
CONFIG_YAML="$HOME/.hermes/config.yaml"
SNAPSHOT_JSON="$HOME/.hermes/.skills_prompt_snapshot.json"
# Floor: known-good external dirs as of 2026-07-04. Shrinking below this floor is
# always a regression signal even if some other dir was added to compensate.
EXPECTED_DIRS=(
  "/Users/colingreig/dev/ignite-skills-live/skills"
  "/Users/colingreig/dev/ignite-skills-live/ignite-code/skills"
  "/Users/colingreig/dev/ignite-skills-live/ignite-content/skills"
  "/Users/colingreig/.claude/plugins/marketplaces/anthropic-agent-skills/skills"
  "/Users/colingreig/brain/packs/social-hub"
  "/Users/colingreig/brain/packs/local-seo-brain"
  "/Users/colingreig/brain/packs/marketing-brain"
  "/Users/colingreig/brain/packs/website-brain"
  "/Users/colingreig/.claude/plugins/marketplaces/ignite-marketplace/plugins/claude-ads"
  "/Users/colingreig/.claude/plugins/marketplaces/ignite-marketplace/plugins/claude-seo"
  "/Users/colingreig/.claude/plugins/marketplaces/ignite-marketplace/plugins/claude-blog"
)
SKILLS_BRIDGE_ALERT=""
if [ ! -f "$CONFIG_YAML" ]; then
  red "MISSING config: $CONFIG_YAML"; FAIL=1
  SKILLS_BRIDGE_ALERT="${SKILLS_BRIDGE_ALERT}- config.yaml missing entirely: $CONFIG_YAML\n"
else
  # 7a. Every floor dir must still be a member of the config's real external_dirs
  # list AND exist on disk. Parsing handles both a proper YAML list and (defensively,
  # since a manual `hermes config` rewrite has produced this before) a JSON-encoded
  # string form of the same list.
  for d in "${EXPECTED_DIRS[@]}"; do
    if "$REPO/venv/bin/python" - "$CONFIG_YAML" "$d" <<'PY' 2>/dev/null
import json, sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
raw = (cfg.get("skills") or {}).get("external_dirs") or []
if isinstance(raw, str):
    try:
        raw = json.loads(raw)
    except Exception:
        raw = [raw]
sys.exit(0 if sys.argv[2] in raw else 1)
PY
    then
      grn "external_dirs has  $d"
      if [ ! -d "$d" ]; then
        red "  -> but the directory does NOT exist on disk: $d"
        FAIL=1
        SKILLS_BRIDGE_ALERT="${SKILLS_BRIDGE_ALERT}- configured but missing on disk: $d\n"
      fi
    else
      red "external_dirs MISSING  $d — Hermes can't see the document/ignite skills"
      red "  Fix: add it under skills.external_dirs in $CONFIG_YAML, then restart:"
      red "       launchctl kickstart -k gui/$UID_NUM/{ai.hermes.gateway,com.colingreig.hermes-dashboard}"
      FAIL=1
      SKILLS_BRIDGE_ALERT="${SKILLS_BRIDGE_ALERT}- dropped from skills.external_dirs: $d\n"
    fi
  done

  # 7b. Live skill index sentinel (NEW, 86e25qd33 item 3): config listing the right
  # dirs is necessary but not sufficient — assert the actual rendered skill index
  # (what prompt_builder.build_skills_system_prompt() serves into prompts, LIVE —
  # external skill dirs are scanned at prompt-build time and are deliberately never
  # written to the snapshot JSON, so the snapshot file alone can't verify this)
  # still contains representative skills from each external category and hasn't
  # silently shrunk below a sane floor.
  SNAP_RESULT=$("$REPO/venv/bin/python" - "$SNAPSHOT_JSON" "$REPO" <<'PY' 2>&1
import sys, re
sys.path.insert(0, sys.argv[2])
SENTINELS = ["blog-write", "docx", "seo-audit", "ignite-blog"]
FLOOR = 150
try:
    from agent import prompt_builder as pb
    txt = pb.build_skills_system_prompt(available_tools=None, available_toolsets=None)
except Exception as e:
    print(f"UNREADABLE {e!r}")
    sys.exit(2)
names = set(re.findall(r'(?m)^\s{4}-\s+([a-z0-9][a-z0-9:_-]+)', txt))
missing = [s for s in SENTINELS if s.lower() not in txt.lower()]
print(f"COUNT {len(names)}")
print(f"MISSING {','.join(missing)}")
sys.exit(1 if (missing or len(names) < FLOOR) else 0)
PY
  )
  snap_rc=$?
  snap_count=$(printf '%s\n' "$SNAP_RESULT" | awk '/^COUNT /{print $2}')
  snap_missing=$(printf '%s\n' "$SNAP_RESULT" | awk -F' ' '/^MISSING /{print $2}')
  if printf '%s\n' "$SNAP_RESULT" | grep -q '^UNREADABLE'; then
    red "snapshot   $SNAPSHOT_JSON unreadable/missing — cannot verify skill count: $SNAP_RESULT"
    FAIL=1
    SKILLS_BRIDGE_ALERT="${SKILLS_BRIDGE_ALERT}- skills snapshot unreadable: $SNAPSHOT_JSON\n"
  elif [ "$snap_rc" -eq 0 ]; then
    grn "live skill index   $snap_count skill(s) live, all sentinels present (floor 150)"
  else
    red "live skill index   REGRESSED — $snap_count skill(s) live (floor 150), missing sentinel(s): ${snap_missing:-none}"
    red "  This is the exact failure mode that let 217->29 go unnoticed for days."
    FAIL=1
    SKILLS_BRIDGE_ALERT="${SKILLS_BRIDGE_ALERT}- live skill index regressed: ${snap_count:-0} skills live (floor 150), missing sentinel(s): ${snap_missing:-none}\n"
  fi
fi

# 7c. Alert loudly on any skills-bridge failure above (item 4, 86e25qd33) — a
# non-zero script exit alone is easy to miss; babysit-hermes reads this script's
# output but a ClickUp comment guarantees the failure surfaces even between
# babysit-hermes passes. Same task-comment convention as the ignite-marketplace
# sync escalation path (POST /api/v2/task/{id}/comment, CLICKUP_API_TOKEN only,
# no destructive list-write). De-duped to once per calendar day via a marker file
# so repeated `hermes update` runs don't spam the thread.
if [ -n "$SKILLS_BRIDGE_ALERT" ]; then
  ALERT_MARKER="$HOME/.hermes/state/skills-bridge-alert-marker"
  ALERT_TASK_ID="${SKILLS_BRIDGE_ALERT_TASK_ID:-86e25qd33}"
  TODAY="$(date +%F)"
  mkdir -p "$(dirname "$ALERT_MARKER")"
  if [ "$(cat "$ALERT_MARKER" 2>/dev/null)" != "$TODAY" ]; then
    # This script runs standalone (post `hermes update`, not necessarily as a child
    # of the op-run-wrapped gateway process), so it can't assume OP_SERVICE_ACCOUNT_TOKEN
    # is already inherited — fall back to the read-only runtime token file directly.
    CU_TOKEN="$(_op_read CLICKUP_API_TOKEN)"
    if [ -n "$CU_TOKEN" ]; then
      ALERT_BODY=$(printf '🤖 **Escalation from verify-hermes-patches.sh §7 (skills bridge)**\n\nThe external skills bridge (skills.external_dirs -> live prompt snapshot) has a problem:\n\n%b\n**Why this matters:** this is the exact class of failure that let the skill count silently regress 217->29 for days.\n\n**Next action:** re-run `~/.hermes/scripts/verify-hermes-patches.sh` after investigating, restart the gateway if config.yaml was corrected (`launchctl kickstart -k gui/%s/{ai.hermes.gateway,com.colingreig.hermes-dashboard}`), and confirm §7 goes green.\n' "$SKILLS_BRIDGE_ALERT" "$UID_NUM")
      if curl -sf -X POST "https://api.clickup.com/api/v2/task/$ALERT_TASK_ID/comment" \
          -H "Authorization: $CU_TOKEN" -H "Content-Type: application/json" \
          -d "$(python3 -c 'import json,sys; print(json.dumps({"comment_text": sys.argv[1], "notify_all": False}))' "$ALERT_BODY")" \
          >/dev/null 2>&1; then
        echo "$TODAY" > "$ALERT_MARKER"
        ylw "alert      posted skills-bridge escalation comment to ClickUp task $ALERT_TASK_ID"
      else
        ylw "alert      FAILED to post ClickUp escalation comment (network/auth) — see console output above for the actual failure"
      fi
    else
      ylw "alert      CLICKUP_API_TOKEN unavailable (op read failed) — skipping ClickUp escalation, console output above is authoritative"
    fi
  fi
fi

# --- 8. GitHub App identity wiring (Hermes Dev Assistant) ------------------
# Hermes commits/PRs as the "Hermes Dev Assistant" GitHub App (App 4053083,
# install 140297518) instead of Colin's PAT. Wiring spans: App credentials in
# Doppler (GH_APP_PRIVATE_KEY, GH_APP_ID, GH_APP_INSTALLATION_ID), an isolated
# gitconfig, 2 helper scripts, and the App-token mint call in both plists.
# As of 2026-06-30 (task 86e1vuzwb): the PEM is stored in Doppler as a multiline
# secret (GH_APP_PRIVATE_KEY), NOT just as a local file. The token script reads
# the env var first, falling back to the local PEM file. The static PAT
# (GH_API_KEY_HERMES) has been decommissioned. Design ref: brain note "Hermes
# GitHub App identity"; task 86e1vuzwb.
hdr "8. GitHub App identity wiring"
APP_KEY="$HOME/.hermes/hermes-dev-assistant.private-key.pem"
APP_ENVF="$HOME/.hermes/.env"
APP_GITCFG="$HOME/.hermes/gitconfig"
TOK_SCRIPT="$HOME/.hermes/scripts/github_app_token.py"
CRED_SCRIPT="$HOME/.hermes/scripts/github_app_cred.sh"
# 8a. files present (local PEM is now optional — 1Password is the primary source)
for f in "$APP_GITCFG" "$TOK_SCRIPT" "$CRED_SCRIPT"; do
  [ -f "$f" ] && grn "present    ${f##*/}" || { red "MISSING    $f"; FAIL=1; }
done
if [ -f "$APP_KEY" ]; then
  grn "present    ${APP_KEY##*/} (local fallback)"
  perm=$(stat -f %A "$APP_KEY" 2>/dev/null)
  [ "$perm" = "600" ] && grn "key perms  600" || { red "key perms  $perm (expected 600) — run: chmod 600 $APP_KEY"; FAIL=1; }
else
  ylw "optional    ${APP_KEY##*/} absent — 1Password GH_APP_PRIVATE_KEY is the primary source"
fi
# 8b. 1Password credentials present (primary credential source)
APP_KEY_DPL=$(_op_read GH_APP_PRIVATE_KEY)
APP_ID_DPL=$(_op_read GH_APP_ID)
APP_INST_DPL=$(_op_read GH_APP_INSTALLATION_ID)
if [ -n "$APP_KEY_DPL" ] && [ -n "$APP_ID_DPL" ] && [ -n "$APP_INST_DPL" ]; then
  grn "1password   GH_APP_* secrets present (id=$APP_ID_DPL, inst=$APP_INST_DPL)"
else
  red "1password   MISSING GH_APP_* secrets — token minting will fail without local PEM fallback"; FAIL=1
fi
# 8c. static PAT decommissioned (must be absent from 1Password)
STALE_PAT=$(_op_read GH_API_KEY_HERMES)
if [ -z "$STALE_PAT" ]; then
  grn "decommiss   GH_API_KEY_HERMES absent from 1Password (PAT decommissioned)"
else
  red "decommiss   GH_API_KEY_HERMES still in op://Dev Toolbox/dev — delete it via the 1Password app/CLI"; FAIL=1
fi
# 8d. env lines still present (for local dev / fallback)
for v in GITHUB_APP_ID GITHUB_APP_INSTALLATION_ID GITHUB_APP_PRIVATE_KEY_PATH GIT_CONFIG_GLOBAL; do
  grep -q "^$v=" "$APP_ENVF" 2>/dev/null && grn "env        $v set" || ylw "env        $v not in .env (1Password is primary)"
done
# 8e. live end-to-end: mint a token AND prove git auths as the bot to an installed repo
if [ -f "$TOK_SCRIPT" ]; then
  if "$REPO/venv/bin/python" "$TOK_SCRIPT" >/dev/null 2>&1 || python3 "$TOK_SCRIPT" >/dev/null 2>&1; then
    grn "token      mints OK (App auth chain live)"
  else
    red "token      MINT FAILED — check Doppler GH_APP_* secrets / local PEM"; FAIL=1
  fi
  bot=$(GIT_CONFIG_GLOBAL="$APP_GITCFG" git config user.email 2>/dev/null)
  case "$bot" in
    *hermes-dev-assistant\[bot\]*) grn "git author $bot" ;;
    *) red "git author resolved to '$bot' (expected the bot noreply email)"; FAIL=1 ;;
  esac
fi

# --- 9. Review-daemon crons present (Universal AI Review Daemon) -----------
# The review daemon is two cron jobs in ~/.hermes/cron/jobs.json: the no-agent
# gate `review-poll-gate` (*/15) and the validator agent `hermes-validate`.
# jobs.json normally survives `hermes update` (it's outside the repo), but a
# cron reinstall / jobs.json rewrite can drop them, silently killing the daemon
# (work in `ready for review` + `needs-validation` would pile up unvalidated).
# Detection-only; re-create by hand (cron IDs regenerate, so no auto-fix):
#   hermes cron create "0 4 * * *"  --name hermes-validate  --skill hermes-validate --deliver local
#   hermes cron create "*/15 * * * *" --name review-poll-gate --script review_poll_gate.py --no-agent --deliver local
# Design ref: brain note "Universal AI Review Daemon — built + shadow-deployed (2026-06-15)".
hdr "9. Review-daemon crons (review-poll-gate + hermes-validate)"
for jobname in review-poll-gate hermes-validate; do
  present="$("$REPO/venv/bin/python" - "$JOBS_JSON" "$jobname" <<'PY' 2>/dev/null
import json,sys
jobs=json.load(open(sys.argv[1])); jl=jobs if isinstance(jobs,list) else jobs.get("jobs",[])
print("YES" if any(isinstance(j,dict) and j.get("name")==sys.argv[2] for j in jl) else "NO")
PY
)"
  if [ "$present" = "YES" ]; then grn "cron present   $jobname"
  else red "cron MISSING   $jobname — review daemon is down; re-create (see header comment)"; FAIL=1; fi
done

# --- 10. Merge-guard wiring (executor/validator self-merge gate) -----------
# Mechanical gate that stops out-of-band self-merges during shadow (the PR #35
# failure). Layers: (A) pre_tool_call hook in config.yaml -> merge_guard.py
# [PRIMARY], (B) VALIDATE_SHADOW refuse in hermes_validate_ops.cmd_merge_pr,
# (C) defense-in-depth refuse in ~/.hermes/bin/gh.
# DURABILITY: all of merge_guard.py, hermes_validate_ops.py, the gh shim, and
# config.yaml live OUTSIDE the hermes-agent repo, so a normal `hermes update`
# (which only stashes/reset-hard's REPO edits) does NOT revert them. The real
# risk is a `hermes config` rewrite / setup-wizard / config migration touching
# config.yaml — this section is the post-rewrite tripwire, and all checks are
# BEHAVIORAL (run the guard), not substring greps that pass on a dead matcher.
hdr "10. Merge-guard wiring (self-merge gate)"
MG="$HOME/.hermes/scripts/merge_guard.py"
GH_SHIM="$HOME/.hermes/bin/gh"
VAL_OPS="$HOME/.hermes/scripts/hermes_validate_ops.py"
HOOK_PY="$REPO/venv/bin/python"
# 10a. guard present + parseable + pinned interpreter exists
if [ -x "$HOOK_PY" ] && [ -f "$MG" ] && "$HOOK_PY" -c "import ast; ast.parse(open('$MG').read())" 2>/dev/null; then
  grn "present    merge_guard.py parse-ok; hook interpreter exists"
else
  red "MISSING/UNPARSEABLE merge_guard.py OR hook interpreter $HOOK_PY absent — gate DOWN (hook fails OPEN)"; FAIL=1
fi
# 10b. config.yaml has BOTH pre_tool_call entries pointing at the guard
if "$HOOK_PY" - "$CONFIG_YAML" <<'PY' 2>/dev/null
import yaml,sys
cfg=yaml.safe_load(open(sys.argv[1])) or {}
entries=((cfg.get("hooks") or {}).get("pre_tool_call") or [])
want={"^terminal$","^mcp__plugin_github_github__merge_pull_request$"}
have={e.get("matcher") for e in entries if isinstance(e,dict) and "merge_guard.py" in (e.get("command") or "")}
sys.exit(0 if want.issubset(have) else 1)
PY
then grn "config     pre_tool_call hooks wired (terminal + MCP merge)"
else red "config     pre_tool_call merge-guard hooks MISSING in config.yaml — re-add both matchers, then: launchctl kickstart -k gui/$UID_NUM/ai.hermes.gateway"; FAIL=1; fi
# 10c. BEHAVIORAL: guard BLOCKS a merge and ALLOWS a merge-themed pr create (shadow)
mg_block=$(printf '%s' '{"tool_name":"terminal","tool_input":{"command":"gh pr merge 35 --squash"}}' | VALIDATE_SHADOW=true HERMES_AUTONOMOUS_MERGE= "$HOOK_PY" "$MG" 2>/dev/null)
mg_allow=$(printf '%s' '{"tool_name":"terminal","tool_input":{"command":"gh pr create --title \"fix merge conflict\""}}' | VALIDATE_SHADOW=true HERMES_AUTONOMOUS_MERGE= "$HOOK_PY" "$MG" 2>/dev/null)
if echo "$mg_block" | grep -q '"decision": *"block"' && ! echo "$mg_allow" | grep -q block; then
  grn "behavior   guard BLOCKS gh pr merge, ALLOWS gh pr create (no false-positive)"
else
  red "behavior   guard matcher BROKEN (block=$([ -n "$mg_block" ] && echo y || echo n), create-blocked=$(echo "$mg_allow"|grep -q block && echo y || echo n))"; FAIL=1
fi
# 10d. BEHAVIORAL: validator cmd_merge_pr REFUSES a live merge under shadow.
# Capture-then-grep (NOT a pipe): the script intentionally exits non-zero on
# refusal, which `set -o pipefail` would otherwise turn into a false red.
# The safety property under test is that a live merge is REFUSED — NOT which
# specific gate fires. cmd_merge_pr is defense-in-depth: the verdict gate runs
# FIRST (refuses "no validator verdict on record, fail-closed" when PR #35 has
# no verdict), and only a PR that passes the verdict gate reaches the
# VALIDATE_SHADOW shadow-refusal branch. PR #35's verdict-store state is
# incidental and time-varying, so asserting ONLY the shadow substring made 10d
# intermittently false-RED whenever #35 had no verdict (the common case) — a
# stale test, not a real hole (task 86e1yjkzf). Accept refusal via EITHER gate;
# go red ONLY if the merge is genuinely NOT refused.
val_out=$(VALIDATE_SHADOW=true "$HOOK_PY" "$VAL_OPS" merge-pr colingreig/ignite-digital-engine 35 --squash 2>&1 || true)
if echo "$val_out" | grep -qiE 'VALIDATE_SHADOW|verdict gate refuses|refuses merge|fail-closed'; then
  grn "validator  cmd_merge_pr refuses live merge in shadow (verdict-gate or shadow branch)"
else
  red "validator  cmd_merge_pr did NOT refuse live merge — live merge possible while writeback muzzled"; FAIL=1
fi
# 10e. BEHAVIORAL: gh shim refuses pr merge under shadow (refuses before exec)
if [ -x "$GH_SHIM" ] && VALIDATE_SHADOW=true HERMES_AUTONOMOUS_MERGE= "$GH_SHIM" pr merge 999999 --squash >/dev/null 2>&1; [ "$?" = "13" ]; then
  grn "shim       ~/.hermes/bin/gh refuses pr merge (defense-in-depth, exit 13)"
else
  ylw "shim       ~/.hermes/bin/gh merge refuse not firing (secondary layer; hook is primary)"
fi

# --- 11. Git commit identity guard (blocks -c user.email=<non-bot>) ----------
# Mechanical gate that stops `git commit -c user.email=hermes@ignitemarketing.com`
# (and any other non-bot email) — enforces clean GitHub-App bot attribution on
# Hermes commits. NOTE (corrected 2026-06-18): this is commit HYGIENE, not a
# Vercel fix. Vercel builds regardless of author; the stuck elevatoruptime.com
# PRs failed on a separate Supabase-at-prerender build error, fixed by disabling
# Vercel preview deployments fleet-wide. Fix applied 2026-06-18.
# DURABILITY: git_commit_identity_guard.py lives in ~/.hermes/scripts (outside
# the hermes-agent repo) so hermes update does NOT revert it. The risk is a
# `hermes config` rewrite clobbering the config.yaml hook entry — this section
# is the tripwire. The guard is FAIL-OPEN (errors allow the commit through) so
# the PRIMARY fix is the skill-level rule + skill reference correction.
hdr "11. Git commit identity guard (clean bot attribution)"
CIG="$HOME/.hermes/scripts/git_commit_identity_guard.py"
HOOK_PY="$REPO/venv/bin/python"
# 11a. guard present + parseable
if [ -x "$HOOK_PY" ] && [ -f "$CIG" ] && "$HOOK_PY" -c "import ast; ast.parse(open('$CIG').read())" 2>/dev/null; then
  grn "present    git_commit_identity_guard.py parse-ok"
else
  # P4: always set FAIL=1 for a missing required guard, regardless of --apply mode.
  # --apply has no auto-fix for a missing guard file (it's not a git patch); printing
  # red without setting FAIL=1 allowed the script to exit 0 with a dead guard.
  red "MISSING or UNPARSEABLE git_commit_identity_guard.py — guard is DOWN (fail-open)"
  red "  Recreate from the canonical source at ~/.hermes/scripts/git_commit_identity_guard.py"
  red "  (keep it fail-open: any exception in the guard must exit 0, not block)"
  FAIL=1
fi
# 11b. config.yaml has the terminal pre_tool_call entry for the identity guard
if "$HOOK_PY" - "$CONFIG_YAML" <<'PY' 2>/dev/null
import yaml,sys
cfg=yaml.safe_load(open(sys.argv[1])) or {}
entries=((cfg.get("hooks") or {}).get("pre_tool_call") or [])
have=any(isinstance(e,dict) and "git_commit_identity_guard.py" in (e.get("command") or "") for e in entries)
sys.exit(0 if have else 1)
PY
then grn "config     pre_tool_call hook wired for git_commit_identity_guard.py"
else
  red "config     git_commit_identity_guard pre_tool_call hook MISSING in config.yaml"
  if [ "$APPLY" -eq 1 ]; then
    red "  Add to config.yaml hooks.pre_tool_call:"
    red "  - matcher: ^terminal\$"
    red "    command: $HOOK_PY $CIG"
    red "    timeout: 5"
    red "  Then restart: launchctl kickstart -k gui/$UID_NUM/ai.hermes.gateway"
  fi
  FAIL=1
fi
# 11c. BEHAVIORAL: guard blocks bad email, allows clean commit
cig_block=$(printf '%s' '{"tool_name":"terminal","tool_input":{"command":"git commit -c user.email=hermes@ignitemarketing.com -m \"test\""}}' | "$HOOK_PY" "$CIG" 2>/dev/null)
cig_allow=$(printf '%s' '{"tool_name":"terminal","tool_input":{"command":"git commit -m \"fix: something\""}}' | "$HOOK_PY" "$CIG" 2>/dev/null)
if echo "$cig_block" | grep -q '"decision": *"block"' && ! echo "$cig_allow" | grep -q block; then
  grn "behavior   guard BLOCKS bad-email commit, ALLOWS clean commit"
else
  red "behavior   identity guard BROKEN (block=$([ -n "$cig_block" ] && echo y || echo n), clean-blocked=$(echo "$cig_allow"|grep -q block && echo y || echo n))"; FAIL=1
fi

# --- 12. Autonomous-merge fail-closed CI gate ------------------------------
# autonomous_merge.py (the merge ACTOR) must refuse to merge a PR that has NO
# green gating CI check — otherwise a repo whose PRs run only non-gating checks
# (Vercel/preview/smoke) auto-merges UNVERIFIED, violating the "CI green"
# guardrail (the topdynamicspartners gap, found 2026-06-21). Like section 10
# this file lives OUTSIDE the hermes-agent repo so `hermes update` does NOT
# revert it; the real risk is a hand-edit/refactor dropping the gate. BEHAVIORAL
# tripwire: drive evaluate() with a stubbed PR-state and assert block-vs-merge.
hdr "12. Autonomous-merge fail-closed CI gate (require green gating check)"
AM="$HOME/.hermes/scripts/autonomous_merge.py"
HOOK_PY="$REPO/venv/bin/python"
if [ -f "$AM" ] && "$HOOK_PY" -c "import ast; ast.parse(open('$AM').read())" 2>/dev/null; then
  am_verdict=$(HERMES_AUTONOMOUS_MERGE=1 VALIDATE_SHADOW=false "$HOOK_PY" - "$AM" <<'PY' 2>/dev/null
import sys,importlib.util
spec=importlib.util.spec_from_file_location("autonomous_merge",sys.argv[1])
am=importlib.util.module_from_spec(spec); spec.loader.exec_module(am)
sys.modules["autonomous_merge"]=am  # so cmd_merge_pr's `import autonomous_merge` gets THIS (monkeypatched) instance
am.validator_verdict.is_pass_fresh=lambda repo,pr:(True,"fresh")
base=dict(state="OPEN",head="abc123",mergeable="MERGEABLE",merge_state="CLEAN",
          failing=[],pending=[],ignored=["Vercel"])
v={"tier":"low","head_sha":"abc123"}; al={"r/r"}
# Point 1: the sweep ACTOR (autonomous_merge.evaluate).
am._pr_state=lambda repo,pr:(dict(base,gating_green=[]),None)
no_gate=am.evaluate("r/r",1,v,al)[0]
am._pr_state=lambda repo,pr:(dict(base,gating_green=["Tests"]),None)
has_gate=am.evaluate("r/r",1,v,al)[0]
# Point 2: the merge CHOKEPOINT (hermes_validate_ops.cmd_merge_pr) — a direct
# `merge-pr` call must also be gated. Stub the upstream gates, then drive it.
import types
import hermes_validate_ops as ops
ops.VALIDATE_SHADOW=False; ops.DRY_RUN=True
ops.load_allowlist=lambda:{"r/r"}; ops.repo_allowed=lambda r,a:True
ops.validator_verdict=types.SimpleNamespace(is_pass_fresh=lambda r,p:(True,"fresh"))
arg=types.SimpleNamespace(repo="r/r",pr_number=1,squash=True)
import io,contextlib
_sink=io.StringIO()
with contextlib.redirect_stdout(_sink),contextlib.redirect_stderr(_sink):
    am._pr_state=lambda repo,pr:(dict(base,gating_green=[]),None)
    cp_no=ops.cmd_merge_pr(arg)        # expect 1 (blocked)
    am._pr_state=lambda repo,pr:(dict(base,gating_green=["Tests"]),None)
    cp_yes=ops.cmd_merge_pr(arg)       # expect 0 (reaches dry-run merge)
ok=(no_gate=="blocked" and has_gate=="merge" and cp_no==1 and cp_yes==0)
print("BLOCK" if ok else f"BROKEN(actor:no={no_gate},yes={has_gate};choke:no={cp_no},yes={cp_yes})")
PY
)
  if [ "$am_verdict" = "BLOCK" ]; then
    grn "behavior   fail-closed CI gate live at BOTH actor (evaluate) + chokepoint (cmd_merge_pr)"
  else
    red "behavior   autonomous-merge fail-closed gate BROKEN ($am_verdict) — a no-CI PR could auto-merge unverified"; FAIL=1
  fi
else
  red "MISSING/UNPARSEABLE autonomous_merge.py — merge actor down or unguarded"; FAIL=1
fi

# --- 13. Verdict-store fail-closed (sticky BLOCK + HIGH-tripwire override) ---
# Caretaker fix 2026-06-21 (jdmbuysell-v4#390): the verdict store was
# last-write-wins, so a PASS overwrote a same-head BLOCK and a broken PR
# auto-merged. These behaviors live in ~/.hermes/scripts (OUTSIDE the
# hermes-agent repo, so `hermes update` does NOT revert them); the real risk is
# a hand-edit/refactor dropping the guard. BEHAVIORAL tripwire:
#   (1) record_verdict keeps a same-head BLOCK over a later PASS
#   (2) validate_pr downgrades PASS->BLOCK when a HIGH tripwire finding exists
hdr "13. Verdict-store fail-closed (sticky BLOCK + HIGH-tripwire override)"
VV="$HOME/.hermes/scripts/validator_verdict.py"
VP="$HOME/.hermes/scripts/validate_pr.py"
if [ -f "$VV" ] && [ -f "$VP" ] \
   && "$HOOK_PY" -c "import ast; ast.parse(open('$VV').read())" 2>/dev/null \
   && "$HOOK_PY" -c "import ast; ast.parse(open('$VP').read())" 2>/dev/null; then
  vv_verdict=$("$HOOK_PY" - "$VV" <<'PY' 2>/dev/null
import sys,importlib.util,tempfile,os
spec=importlib.util.spec_from_file_location("validator_verdict",sys.argv[1])
vv=importlib.util.module_from_spec(spec); spec.loader.exec_module(vv)
tmp=tempfile.mktemp(suffix=".json")
try:
    # write a BLOCK for head=sha1, then a PASS for the SAME head -> must stay BLOCK
    vv.record_verdict("r/r",1,{"verdict":"BLOCK","head_sha":"sha1","tier":"high"},path=tmp)
    kept,sticky=vv.record_verdict("r/r",1,{"verdict":"PASS","head_sha":"sha1","tier":"high"},path=tmp)
    same_head_held = (kept["verdict"]=="BLOCK" and sticky is True)
    # a NEW head PASS resets (sticky must NOT fire)
    kept2,sticky2=vv.record_verdict("r/r",1,{"verdict":"PASS","head_sha":"sha2","tier":"high"},path=tmp)
    new_head_resets = (kept2["verdict"]=="PASS" and sticky2 is False)
    print("OK" if (same_head_held and new_head_resets) else f"BROKEN(held={same_head_held},reset={new_head_resets})")
finally:
    try: os.remove(tmp)
    except OSError: pass
PY
)
  # Static check: validate_pr has the fail-closed PASS->BLOCK override on HIGH findings.
  if grep -q 'FAIL-CLOSED OVERRIDE' "$VP" && grep -q 'verdict = "BLOCK"' "$VP"; then vp_ok=y; else vp_ok=n; fi
  if [ "$vv_verdict" = "OK" ] && [ "$vp_ok" = "y" ]; then
    grn "behavior   sticky BLOCK holds same-head PASS; new-head resets; validate_pr HIGH-tripwire override present"
  else
    red "verdict-store fail-closed BROKEN (sticky=$vv_verdict, validate_pr_override=$vp_ok)"; FAIL=1
  fi
else
  red "MISSING/UNPARSEABLE validator_verdict.py or validate_pr.py — verdict-store guard down"; FAIL=1
fi

# --- 14. Image-gen venv dependency (google-genai) --------------------------
# Caretaker fix 2026-06-22: the claude-blog hero-image ladder
# (scripts/generate_hero.py, ladder step 2 "direct Gemini API") imports
# `google.genai`. If that SDK is absent from the hermes-agent venv, the Gemini
# rung silently prints "google-genai not installed; skipping" and the ladder
# falls through to stock/Openverse — which is how jdmbuysell PR #391/#392
# shipped with a wrong/stolen hero instead of a generated one. The credential
# itself already flows correctly: .env resolves GOOGLE_AI_API_KEY=${GEMINI_API_KEY}
# via python-dotenv interpolation, and GOOGLE_AI_API_KEY is NOT in the sandbox
# _HERMES_PROVIDER_ENV_BLOCKLIST, so it survives the subprocess scrub (whereas
# GEMINI_API_KEY / GOOGLE_API_KEY are scrubbed). `hermes update` can rebuild the
# venv and drop this package, so we re-ensure it here.
hdr "14. Image-gen venv dependency (google-genai)"
VENV_PY="$REPO/venv/bin/python"
if "$VENV_PY" -c 'import google.genai' 2>/dev/null; then
  gg_ver="$("$VENV_PY" -c 'from importlib.metadata import version; print(version("google-genai"))' 2>/dev/null)"
  grn "present    google-genai $gg_ver importable in hermes-agent venv (blog hero Gemini rung live)"
elif [ "$APPLY" -eq 1 ]; then
  ylw "missing    google-genai not in venv — installing (blog hero Gemini rung is dead without it)"
  if "$VENV_PY" -m pip install --quiet google-genai >/dev/null 2>&1 \
     && "$VENV_PY" -c 'import google.genai' 2>/dev/null; then
    grn "installed  google-genai now importable in hermes-agent venv"
  else
    red "FAILED to install google-genai into venv — blog hero AI rung stays dead"; FAIL=1
  fi
else
  red "MISSING google-genai in venv — blog hero Gemini rung dead (falls to stock/Openverse). Re-run with --apply"; FAIL=1
fi

# --- 15. Driven imagery-count tripwire (>3 distinct owned images) -----------
# Caretaker fix 2026-06-22: operator policy — jdmbuysell "Driven" review posts
# must reference MORE THAN 3 (>=4) distinct generated/owned images under
# /images/driven/ (PR #391 shipped a Driven post with too few real in-body
# images). The gate is check_blog_imagery_count() in scripts/validate_tripwires.py.
# That file lives OUTSIDE the hermes-agent repo so `hermes update` does NOT
# revert it; the real risk is a hand-edit/refactor dropping the check from run().
# BEHAVIORAL tripwire: a NEW Driven post (category "Driven") carrying only 1
# owned image MUST produce a high blog_imagery_count finding.
hdr "15. Driven imagery-count tripwire (>3 distinct owned images)"
VT="$HOME/.hermes/scripts/validate_tripwires.py"
if [ -f "$VT" ] && "$VENV_PY" -c "import ast; ast.parse(open('$VT').read())" 2>/dev/null; then
  dv_verdict=$(cd "$HOME/.hermes/scripts" && "$VENV_PY" - <<'PY' 2>/dev/null
import validate_tripwires as vt
diff = (
  'diff --git a/apps/web/src/content/blog/d.mdx b/apps/web/src/content/blog/d.mdx\n'
  'new file mode 100644\n--- /dev/null\n+++ b/apps/web/src/content/blog/d.mdx\n'
  '@@ -0,0 +1,6 @@\n+---\n+categories:\n+  - "Driven"\n+---\n'
  '+![a](/images/driven/a.jpg)\n'
)
res = vt.run(diff, repo="")
hits = [f for f in res["findings"]
        if f["check"] == "blog_imagery_count" and f["severity"] == "high"]
print("OK" if (hits and not res["pass"]) else "BROKEN")
PY
)
  if [ "$dv_verdict" = "OK" ]; then
    grn "behavior   blog_imagery_count BLOCKS a Driven post with <4 distinct owned images (>3 policy live)"
  else
    red "Driven imagery-count tripwire BROKEN ($dv_verdict) — check_blog_imagery_count missing/unwired in validate_tripwires.py"; FAIL=1
  fi
else
  red "MISSING/UNPARSEABLE validate_tripwires.py — Driven imagery-count gate down"; FAIL=1
fi

# --- 16. Vehicle image photorealism self-QC skill --------------------------
# Caretaker fix 2026-06-22: the Evo IV-VI "Driven" set shipped obviously-fake
# generated car images (empty cabins on moving cars, garbled RALLIART banners,
# smeared plates, missing Evo widebody flares) because the executor QC checked
# SUBJECT IDENTITY ("right car, distinct scenes") but never PHOTOREALISM. The
# fix is an executor-side self-QC reject-gate skill the image-gen agent runs on
# its OWN output BEFORE opening the PR. The skill lives in ~/.hermes/skills/
# (NOT a git repo — survives `hermes update`); the real risk is accidental
# deletion or the reject-gate markers being gutted. Presence + marker check.
# Companion knowledge: brain learnings 2026-06-22 vehicle image-gen adversarial
# QC playbook. Reversibility: delete the skill dir + this section.
hdr "16. Vehicle image photorealism self-QC skill"
VQC="$HOME/.hermes/skills/vehicle-image-qc/SKILL.md"
if [ -f "$VQC" ] \
   && grep -q "self-QC reject-gate" "$VQC" \
   && grep -q "no visible driver" "$VQC" \
   && grep -q "widebody fender flares" "$VQC"; then
  grn "present    vehicle-image-qc skill installed with reject-gate markers (executor self-QC live)"
else
  red "MISSING/GUTTED vehicle-image-qc skill (~/.hermes/skills/vehicle-image-qc/SKILL.md) — vehicle image photorealism self-QC gone"; FAIL=1
fi

# --- 17. Vehicle image authenticity validator backstop ---------------------
# Caretaker fix 2026-06-22: companion HARD backstop to section 16's executor
# self-QC. validator_image_authenticity.py runs a vision lens (reusing the panel's
# `hermes -z` one-shot + credential pool) over added /images/driven/ photos in a
# PR; a confident AI-fake -> HIGH finding -> validate_pr.py's fail-closed override
# BLOCKs the merge. It is biased to REAL and fails OPEN (medium/warn) on any vision
# error so it never bricks the gate. Lives OUTSIDE the hermes-agent repo (survives
# `hermes update`); risk is the module being deleted or unwired from validate_pr.py.
# BEHAVIORAL (no network): module imports, validate_pr wires it in, and the
# /images/driven/ scoping picks a driven image out of a synthetic diff.
hdr "17. Vehicle image authenticity validator backstop"
VIA="$HOME/.hermes/scripts/validator_image_authenticity.py"
if [ -f "$VIA" ] && "$VENV_PY" -c "import ast; ast.parse(open('$VIA').read())" 2>/dev/null \
   && grep -q "import validator_image_authenticity as via" "$HOME/.hermes/scripts/validate_pr.py" \
   && grep -q "via.run(" "$HOME/.hermes/scripts/validate_pr.py"; then
  via_verdict=$(cd "$HOME/.hermes/scripts" && "$VENV_PY" - <<'PY' 2>/dev/null
import validator_image_authenticity as via
diff = ('diff --git a/apps/web/public/images/driven/x.jpg b/apps/web/public/images/driven/x.jpg\n'
        'new file mode 100644\nBinary files /dev/null and b/apps/web/public/images/driven/x.jpg differ\n')
files = via.vc.parse_unified_diff(diff)
scoped = via._driven_images_in_diff(files) == ['apps/web/public/images/driven/x.jpg']
noop = via.run('diff --git a/src/x.ts b/src/x.ts\n', repo='o/r', head='deadbeef')['findings'] == []
print("OK" if (scoped and noop) else "BROKEN")
PY
)
  if [ "$via_verdict" = "OK" ]; then
    grn "behavior   image-authenticity backstop wired into validate_pr + scopes to /images/driven/ (no-op elsewhere)"
  else
    red "image-authenticity backstop BROKEN ($via_verdict) — scoping/no-op assertion failed"; FAIL=1
  fi
else
  red "MISSING/UNWIRED validator_image_authenticity.py — vehicle image vision backstop down (check validate_pr.py import + via.run call)"; FAIL=1
fi

# --- 18. OpenCode delegation seam (orchestrator→OpenCode coding worker) ------
# Architecture 2026-06-23: Hermes (gpt-5-mini) no longer writes code — the
# clickup-executor delegates code-writing to OpenCode on openai/gpt-5 via
# ~/.hermes/scripts/opencode_exec.py (STEP 4 of clickup-queue-poller/SKILL.md).
# DURABILITY: the helper + skill live in ~/.hermes/{scripts,skills} (OUTSIDE the
# hermes-agent repo) so `hermes update` does NOT revert them. The real risks are:
#   (a) the opencode binary/symlink/PATH breaking (npm change, manual cleanup),
#   (b) the helper losing a load-bearing guard (OPENCODE_DISABLE_CLAUDE_CODE=1
#       prevents the ~/.claude/skills init-hang + 35k-token bloat;
#       --dangerously-skip-permissions prevents the headless permission-hang;
#       doppler run is the only auth path), or
#   (c) the executor skill's STEP 4 reverting to self-coding.
# This section is the structural tripwire. The live model smoke (network + ~$;
# ~15s) is OPT-IN via HERMES_VERIFY_OPENCODE_SMOKE=1. Refs: brain
# operations/2026-06-23 Hermes orchestrator→OpenCode switch (as-built);
# ~/.hermes/skills/clickup-queue-poller/references/opencode-delegation.md
hdr "18. OpenCode delegation seam (executor → OpenCode openai/gpt-5)"
OC_BIN="$HOME/.hermes/bin/opencode"
OC_EXEC="$HOME/.hermes/scripts/opencode_exec.py"
OC_SKILL="$HOME/.hermes/skills/clickup-queue-poller/SKILL.md"
# 18a. binary resolves on the cron PATH + runs
if [ -x "$OC_BIN" ] || [ -L "$OC_BIN" ]; then
  if oc_ver="$("$OC_BIN" --version 2>/dev/null)"; then
    grn "binary     opencode $oc_ver  (~/.hermes/bin/opencode, on cron PATH)"
  else
    red "binary     ~/.hermes/bin/opencode present but --version FAILED — symlink target broken?"; FAIL=1
    [ -L "$OC_BIN" ] && red "           -> $(readlink "$OC_BIN")"
  fi
else
  red "binary     ~/.hermes/bin/opencode MISSING — every code task hard-fails"
  red "           Fix: ln -sf ~/.npm-global/bin/opencode ~/.hermes/bin/opencode  (or re-install: npm i -g --prefix ~/.npm-global opencode-ai@latest)"; FAIL=1
fi
# 18b. helper present + parses + carries the load-bearing guards.
# P5: anchor greps to the actual runtime constructs, not substrings that could
# match comments. Each check verifies the FLAG is used in executable code.
if [ -f "$OC_EXEC" ] && "$HOOK_PY" -c "import ast; ast.parse(open('$OC_EXEC').read())" 2>/dev/null; then
  miss=""
  # OPENCODE_DISABLE_CLAUDE_CODE must be SET (=1) in the command string — not just mentioned
  grep -qE 'OPENCODE_DISABLE_CLAUDE_CODE=1' "$OC_EXEC" || miss="$miss OPENCODE_DISABLE_CLAUDE_CODE=1"
  # --dangerously-skip-permissions must appear in the CLI args (executable context)
  grep -qF -- '--dangerously-skip-permissions' "$OC_EXEC" || miss="$miss --dangerously-skip-permissions"
  # SCOPED secret injection (S1 2026-06-23 hardening): fetch only the needed keys via
  # `doppler secrets get`, NOT a blanket `doppler run` that injects all ~134 secrets into
  # the --dangerously-skip-permissions child (prompt-injection exfil path).
  grep -qE '"doppler", *"secrets", *"get"' "$OC_EXEC" || miss="$miss scoped-secret-fetch"
  # child_env MUST be an explicit allowlist; a blanket dict(os.environ) is a full-credential
  # leak into the skip-perms child — fail CLOSED if it ever reverts.
  if grep -qE 'child_env *= *dict\(os\.environ\)' "$OC_EXEC"; then miss="$miss env-allowlist(FULL-ENV-LEAK)"; fi
  grep -qE 'child_env *= *\{' "$OC_EXEC" || miss="$miss child_env-allowlist"
  # real wall-clock watchdog timeout (kills a silent hang that emits no stdout)
  grep -qE '_watchdog|threading\.(Thread|Timer)|signal\.alarm' "$OC_EXEC" || miss="$miss watchdog-timeout"
  # openai/gpt-5 must appear as the model string in the MODEL_FALLBACK_ORDER / args
  grep -qE '"openai/gpt-5"' "$OC_EXEC" || miss="$miss \"openai/gpt-5\""
  if [ -z "$miss" ]; then
    grn "helper     opencode_exec.py parse-ok + all guards present (OPENCODE_DISABLE=1, skip-perms, scoped-secret-fetch, env-allowlist, watchdog, gpt-5 literal)"
  else
    red "helper     opencode_exec.py MISSING guard(s):$miss — delegation will hang/bloat/mis-auth"; FAIL=1
  fi
else
  red "helper     MISSING/UNPARSEABLE $OC_EXEC — executor has no code-writing delegate"; FAIL=1
fi
# 18b-codex (2026-06-25): the opt-in Codex WRITER tier. opencode_exec.py must
# carry (a) the HERMES_WRITER_CODEX gate (default OFF) in _provider_enabled and
# the child_env passthrough, and (b) the ("openai/gpt-5.4","openai-codex") cascade
# tier. This is the writer counterpart to the validator chain's codex tier. The
# tier is BEHIND glm-5.2 so a missing/disabled flag is safe (falls to glm-5.2),
# but if the tier string or flag is dropped the opt-in path silently dies.
if [ -f "$OC_EXEC" ]; then
  cmiss=""
  grep -qE 'HERMES_WRITER_CODEX' "$OC_EXEC" || cmiss="$cmiss HERMES_WRITER_CODEX-flag"
  grep -qE '"openai/gpt-5\.4", *"openai-codex"' "$OC_EXEC" || cmiss="$cmiss codex-cascade-tier"
  # HERMES-PATCH 27: the flag must be re-resolved from Doppler (the subprocess
  # sanitizer scrubs the bare env var, so without this gpt-5.4 is silently skipped).
  grep -qE 'HERMES-PATCH 27' "$OC_EXEC" || cmiss="$cmiss patch27-doppler-resolve"
  if [ -z "$cmiss" ]; then
    grn "writer     opencode_exec.py carries opt-in Codex writer tier (HERMES_WRITER_CODEX gate + openai/gpt-5.4→openai-codex cascade + patch27 Doppler-resolve)"
  else
    red "writer     opencode_exec.py MISSING Codex writer wiring:$cmiss — the HERMES_WRITER_CODEX opt-in path is dead (re-apply patch 26/27 companion)"; FAIL=1
  fi
fi
# 18b-proxy (2026-06-25): the codex-proxy launchd service. opencode's Codex writer
# tier routes through a local OpenAI-compatible proxy (port 8646) that injects the
# OAuth bearer + Cloudflare headers (HERMES-PATCH 26). The plist lives OUTSIDE the
# hermes-agent repo so `hermes update` does NOT revert it; the risk is a service
# reinstall dropping the plist or the job unloading. Detect file-present + loaded.
CODEX_PROXY_PLIST="$HOME/Library/LaunchAgents/ai.hermes.codex-proxy.plist"
if [ ! -f "$CODEX_PROXY_PLIST" ]; then
  red "proxy      MISSING plist: $CODEX_PROXY_PLIST — Codex writer path has no upstream proxy"
  red "           Fix: restore the codex-proxy launchd plist (provider openai-codex, port 8646), then: launchctl bootstrap gui/$UID_NUM $CODEX_PROXY_PLIST"; FAIL=1
else
  if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "ai.hermes.codex-proxy"; then
    grn "proxy      ai.hermes.codex-proxy plist present + launchd loaded (Codex writer upstream :8646 live)"
  else
    red "proxy      ai.hermes.codex-proxy plist present but NOT loaded — Codex writer upstream down"
    red "           Fix: launchctl bootstrap gui/$UID_NUM $CODEX_PROXY_PLIST"; FAIL=1
  fi
fi
# 18b-jsonc (2026-06-25, cheap): OpenCode's built-in `openai` provider override must
# point at the local codex proxy (:8646) in Responses mode, else the writer tier
# would hit api.openai.com (no codex OAuth). Best-effort substring check.
OC_JSONC="$HOME/.config/opencode/opencode.jsonc"
if [ -f "$OC_JSONC" ] && grep -q '127.0.0.1:8646' "$OC_JSONC"; then
  grn "proxy      opencode.jsonc overrides openai provider → http://127.0.0.1:8646 (Codex writer routes to proxy)"
else
  ylw "proxy      opencode.jsonc openai→:8646 override not found (Codex writer only matters when HERMES_WRITER_CODEX=1; non-fatal)"
fi
# 18b2. Agent A2 2026-06-24: opencode_exec.py must detect NON-GIT (DB-publish) success
# by deliverable file (fields.json), not only git diff — else dynamics365group blog
# rewrites report ok:false and the tick spins forever.
if [ -f "$OC_EXEC" ] && grep -q 'mode.*db-publish' "$OC_EXEC" && grep -q "db_publish" "$OC_EXEC"; then
  grn "helper     opencode_exec.py detects non-git DB-publish success (fields.json → ok:true mode=db-publish)"
else
  red "helper     opencode_exec.py LACKS non-git DB-publish success detection — DB blog tasks will spin; re-apply Agent A2 fix"; FAIL=1
fi
# 18b3. executor SKILL must route DB-backed content sites to the DB-publish lane BEFORE clone.
if [ -f "$OC_SKILL" ] && grep -q "DB-BACKED CONTENT SITE GATE" "$OC_SKILL"; then
  grn "skill      clickup-queue-poller routes DB-backed sites (dynamics365group) to DB-publish lane before resolve_repo"
else
  red "skill      clickup-queue-poller MISSING DB-BACKED CONTENT SITE GATE — dynamics365group will take git lane and spin; re-apply Agent A2 steer"; FAIL=1
fi
# 18b4. Agent A3 2026-06-24: the DB-publish choreography is collapsed into ONE deterministic
# script (db_publish_task.py) — the gpt-5-mini orchestrator fumbled the hand-rolled multi-step
# recipe (mkdir/prompt-file/&-backgrounding/json-parse) every tick → 0 ships. SKILL must call
# it; the script must exist + be executable + parse.
DB_PUB="$HOME/.hermes/scripts/db_publish_task.py"
if [ -x "$DB_PUB" ] && python3 -c "import ast; ast.parse(open('$DB_PUB').read())" 2>/dev/null; then
  grn "helper     db_publish_task.py present + executable + parse-ok (one-call DB-publish lane)"
else
  red "helper     db_publish_task.py MISSING/not-executable/unparseable — DB-publish lane reverts to fumbled multi-step; re-apply Agent A3 script"; FAIL=1
fi
# 18b4b. 2026-06-27 site-config hardening: per-site values (live_url_base/table/whitelist/
# slug-pattern/db-env) live in db_site_config.py (the SITE_CONFIG registry + fail-safe-LOUD
# routing guard). It is imported by db_publish_task / db_apply / db_closeout_actor /
# db_publish_and_closeout — if it's missing, those imports fail and the whole DB-publish lane
# breaks; if its dynamics365group.com entry is gone, a real publish mis-routes/parks. Guard
# both presence/parse AND the seed entry.
DB_SITECFG="$HOME/.hermes/scripts/db_site_config.py"
if [ -f "$DB_SITECFG" ] && python3 -c "import ast; ast.parse(open('$DB_SITECFG').read())" 2>/dev/null \
   && grep -q "dynamics365group.com" "$DB_SITECFG"; then
  grn "helper     db_site_config.py present + parse-ok + has dynamics365group.com SITE_CONFIG entry (registry + fail-loud routing)"
else
  red "helper     db_site_config.py MISSING/unparseable/no-dynamics-entry — DB-publish routing breaks or mis-routes; restore the SITE_CONFIG registry"; FAIL=1
fi
if [ -f "$OC_SKILL" ] && grep -q "db_publish_task.py" "$OC_SKILL"; then
  grn "skill      clickup-queue-poller step 3c calls db_publish_task.py (single deterministic call)"
else
  red "skill      clickup-queue-poller step 3c does NOT call db_publish_task.py — orchestrator will improvise the choreography and spin; re-apply Agent A3 steer"; FAIL=1
fi
# 18b5. Agent A4 2026-06-24 (Colin): deliverables must be ATTACHED to the task so the
# Windows-PC reviewer + shared brain can see them (local workdir is ephemeral + cross-machine
# invisible). attach_deliverable.py must exist+exec+parse; db_publish_task.py must call it;
# SKILL must mandate attach on draft-to-disk/decision-park closeout BEFORE the review flip.
ATTACH="$HOME/.hermes/scripts/attach_deliverable.py"
if [ -x "$ATTACH" ] && python3 -c "import ast; ast.parse(open('$ATTACH').read())" 2>/dev/null; then
  grn "helper     attach_deliverable.py present + executable + parse-ok (stable-dir preserve + clickup attach)"
else
  red "helper     attach_deliverable.py MISSING/not-executable/unparseable — parked drafts stay unreviewable cross-machine; re-apply Agent A4 helper"; FAIL=1
fi
if [ -f "$DB_PUB" ] && grep -q "attach_deliverable.py" "$DB_PUB"; then
  grn "helper     db_publish_task.py attaches the published fields.json deterministically"
else
  red "helper     db_publish_task.py does NOT attach the deliverable — published content not visible cross-machine; re-apply Agent A4 wiring"; FAIL=1
fi
if [ -f "$OC_SKILL" ] && grep -q "DELIVERABLE MUST BE ATTACHED" "$OC_SKILL"; then
  grn "skill      clickup-queue-poller mandates deliverable-attach on draft-to-disk/decision-park closeout"
else
  red "skill      clickup-queue-poller MISSING the deliverable-attach hard rule — parked drafts will be lost again; re-apply Agent A4 steer"; FAIL=1
fi
# 18c. executor skill STEP 4 still delegates to OpenCode (not reverted to self-coding)
if [ -f "$OC_SKILL" ] && grep -q "opencode_exec.py" "$OC_SKILL"; then
  grn "skill      clickup-queue-poller STEP 4 delegates to opencode_exec.py (not self-coding)"
else
  red "skill      clickup-queue-poller SKILL.md does NOT reference opencode_exec.py — STEP 4 may have reverted to Hermes self-coding"; FAIL=1
fi
# 18d. OPT-IN live model smoke (network + cost). HERMES_VERIFY_OPENCODE_SMOKE=1 to enable.
if [ "${HERMES_VERIFY_OPENCODE_SMOKE:-0}" = "1" ] && [ -x "$OC_BIN" ]; then
  smoke_dir="$(mktemp -d)"; printf 'Create a file ok.txt containing exactly READYCHECK_OK and nothing else.' > "$smoke_dir/p.txt"
  smoke=$(python3 "$OC_EXEC" --workdir "$smoke_dir" --prompt-file "$smoke_dir/p.txt" --task-id verifysmoke 2>/dev/null)
  if echo "$smoke" | grep -q '"ok": *true'; then
    grn "smoke      live opencode delegation wrote code under doppler (cost: $(echo "$smoke" | sed -n 's/.*"cost_usd": *\([0-9.e-]*\).*/\1/p'))"
  else
    red "smoke      live opencode delegation FAILED: $(echo "$smoke" | head -c 200)"; FAIL=1
  fi
  rm -rf "$smoke_dir"
else
  ylw "smoke      live model smoke skipped (set HERMES_VERIFY_OPENCODE_SMOKE=1 to run; costs ~\$ + network)"
fi

# ──────────────────────────────────────────────────────────────────────────
# §19. DB-backed blog publish lane (dynamics365group.com Neon `posts`).
# Added 2026-06-23. Some sites publish a blog post by writing a DB row, not via
# git/PR. The lane: clickup-queue-poller STEP 4 (a0) → db_apply.py (slug-scoped,
# column-whitelisted, verify-after, reversible), DB URL from 1Password (2026-07-04:
# moved off Doppler — see db_site_config.py). Guard that the helper, its safety
# guards, the 1Password secret, and the skill wiring survive.
# ──────────────────────────────────────────────────────────────────────────
hdr "§19  DB-publish lane (dynamics365group Neon)"
DB_APPLY="$HOME/.hermes/scripts/db_apply.py"
if [ -f "$DB_APPLY" ] && python3 -c "import ast,sys; ast.parse(open('$DB_APPLY').read())" 2>/dev/null; then
  miss=""
  for g in "ALLOWED_COLS" "FOR UPDATE" "rowcount != 1" "conn.rollback" "expect_id" "backup"; do
    grep -q "$g" "$DB_APPLY" || miss="$miss $g"
  done
  if [ -z "$miss" ]; then
    grn "helper     db_apply.py parse-ok + all guards present (whitelist, FOR UPDATE, rowcount, rollback, expect-id, backup)"
  else
    red "helper     db_apply.py MISSING guard(s):$miss — DB publish is unsafe"; FAIL=1
  fi
  # Agent A 2026-06-24: unknown sidecar keys must be DROPPED (dropped_unknown), not
  # hard-fail the whole publish. A stray LLM key (e.g. solution_link) was aborting
  # every [Blog rewrite] DB publish → columns_updated:[] → 0 tasks shipped. If this
  # reverts to the old "refusing unknown/forbidden column(s)" RETURN, re-apply.
  if grep -q "dropped_unknown" "$DB_APPLY" && ! grep -q "refusing unknown/forbidden column" "$DB_APPLY"; then
    grn "helper     db_apply.py drops unknown sidecar keys (no longer aborts a valid publish)"
  else
    red "helper     db_apply.py may still HARD-FAIL on unknown keys — [Blog rewrite] backlog will not ship; re-apply Agent A drop-unknown fix"; FAIL=1
  fi
else
  red "helper     MISSING/UNPARSEABLE $DB_APPLY — DB-backed publish lane has no apply path"; FAIL=1
fi
# 19b. Neon D365GROUP_DATABASE_URL[_UNPOOLED] readable from 1Password (2026-07-04:
# the site's db_url_env, per db_site_config.py — moved off Doppler's generic
# DATABASE_URL[_UNPOOLED] name, since the 1Password item stores it site-prefixed).
D365_DSN=$(_op_read D365GROUP_DATABASE_URL_UNPOOLED)
[ -z "$D365_DSN" ] && D365_DSN=$(_op_read D365GROUP_DATABASE_URL)
if [ -n "$D365_DSN" ]; then
  grn "secret     D365GROUP_DATABASE_URL[_UNPOOLED] present in op://Dev Toolbox/dev"
else
  red "secret     D365GROUP_DATABASE_URL[_UNPOOLED] NOT in op://Dev Toolbox/dev — DB publish lane will fail closed"; FAIL=1
fi
# 19b2. psycopg2 importable by the EXECUTOR's interpreter. The gateway plist PATH
# puts the release venv/bin FIRST, so the executor's `python3` is the VENV
# python — NOT homebrew. db_apply.py needs psycopg2 THERE (this exact gap
# made the first autonomous DB-lane run fail "No module named psycopg2" 2026-06-23).
VENV_PY="$REPO/venv/bin/python3"
if [ -x "$VENV_PY" ] && "$VENV_PY" -c "import psycopg2" 2>/dev/null; then
  grn "deps       psycopg2 importable by the executor venv python ($("$VENV_PY" -c 'import psycopg2;print(psycopg2.__version__.split()[0])'))"
else
  red "deps       psycopg2 NOT in the hermes venv — DB lane fails for the cron executor"; \
  red "           Fix: $REPO/venv/bin/python -m pip install psycopg2-binary"; FAIL=1
fi
# 19c. executor skill STEP 4 routes DB-backed content to db_apply.py (not a repo PR / orphan deliverable).
if [ -f "$OC_SKILL" ] && grep -q "db_apply.py" "$OC_SKILL"; then
  grn "skill      clickup-queue-poller (a0) routes DB-backed content to db_apply.py lane"
else
  red "skill      clickup-queue-poller SKILL.md does NOT reference db_apply.py — DB-backed tasks may orphan again"; FAIL=1
fi

# --- 20. Skill-bloat tripwire + self-improve size-discipline -----------------
# 2026-06-23 root-cause: the self-improve background-review loop (background_review.py)
# was told to "be ACTIVE — most sessions produce a skill update, even if small" with
# ZERO size awareness. On 15-min crons it appended to SKILL.md every tick until the
# executor (clickup-queue-poller) hit 114KB and the validator (hermes-pr-validate) hit
# 100KB — past the 100,000-char HARD save limit. Saves were rejected, ticks burned in
# retry loops, and the giant skills drowned the model -> low-quality output. Fix:
# patch 20-background-review-size-discipline.patch (auto-applied in section 1) softens
# the prompt + adds a SIZE DISCIPLINE rule. This section is the behavioral tripwire.
hdr "20. Skill-bloat tripwire + self-improve size-discipline"
BGR="$REPO/agent/background_review.py"
# P5: strengthen from a bare substring grep (matches comments) to verifying that
# the SIZE DISCIPLINE rule is ACTUALLY IN THE PROMPT CONSTANTS — the only place
# that matters. The patch inserts it into _SKILL_REVIEW_PROMPT and _COMBINED_REVIEW_PROMPT
# as the hard-limit enforcement string. Also verify the 100,000-char limit constant
# is mentioned in a prompt string (not just a comment), so an upstream refactor
# that moves the prompt can't silently strand the rule.
bgr_ok=$("$REPO/venv/bin/python" - "$BGR" <<'PY' 2>/dev/null
import ast, sys
src = open(sys.argv[1]).read()
tree = ast.parse(src)
# Collect all string literals that are assigned to module-level names starting with _SKILL or _COMBINED
prompt_strings = []
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and (t.id.startswith("_SKILL_") or t.id.startswith("_COMBINED_")):
                # join all string parts (Constant, JoinedStr, or implicit concat via ast.Constant in body)
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        prompt_strings.append(sub.value)
combined = "\n".join(prompt_strings)
has_discipline = "SIZE DISCIPLINE" in combined
has_limit      = "100,000" in combined or "100000" in combined
has_no_append  = "50KB" in combined or "50 KB" in combined or "already large" in combined
print("OK" if (has_discipline and has_limit and has_no_append)
      else f"BROKEN(discipline={has_discipline},limit={has_limit},no_append={has_no_append})")
PY
)
if [ "$bgr_ok" = "OK" ]; then
  grn "patch      background_review.py prompt constants carry SIZE DISCIPLINE rule + 100k limit + no-append gate (self-improve won't re-bloat skills)"
else
  red "patch      background_review.py SIZE DISCIPLINE MISSING FROM PROMPT CONSTANTS ($bgr_ok)"
  red "           The rule must be in _SKILL_REVIEW_PROMPT/_COMBINED_REVIEW_PROMPT, not just a comment."
  red "           Re-run with --apply (20-*.patch)"; FAIL=1
fi
# any agent-created (non-bundled) SKILL.md over 90KB is on the road back to the limit
_big="$(find "$HOME/.hermes/skills" -name SKILL.md -size +90000c 2>/dev/null)"
if [ -z "$_big" ]; then
  grn "bloat      no agent SKILL.md over 90KB (all under the 100k save limit with headroom)"
else
  red "bloat      SKILL.md files approaching/over the 100k save limit — trim to references/:"; FAIL=1
  echo "$_big" | while read -r f; do [ -n "$f" ] && red "           $(wc -c < "$f" | tr -d ' ') chars  $f"; done
fi

# --- 28. reload_env boot-env protection (config.py) -------------------------
# 2026-06-25: reload_env() deleted Doppler-injected provider keys (ZAI_API_KEY,
# GLM_API_KEY, etc.) from os.environ on every /reload RPC because they are
# "known Hermes keys" absent from ~/.hermes/.env. The fix snapshots the process
# env at import time (_PROCESS_BOOT_ENV_KEYS) and skips deleting any key present
# there. Root cause of the 2026-06-25 gateway keyless-resolution multi-hour stall.
# This is a git patch (28-reload-env-boot-env-protection.patch) — verified by its
# sentinel string appearing in hermes_cli/config.py.
# 2026-06-29: patch 28 EXTENDED to also skip writing UNINTERPOLATED ${VAR}
# values. load_env() is a literal parser (no ${...} expansion) unlike the
# boot-time python-dotenv loader, so `GOOGLE_API_KEY=${GEMINI_API_KEY}` was
# re-clobbered to the literal string on every /reload, breaking gemini key
# resolution (email-triage PATCH-06 "NO API key" every tick). Both fixes ship
# in the one patch file; second sentinel below guards the extension.
hdr "28. reload_env boot-env protection + uninterpolated-ref guard (patch 28)"
RELOAD_SENTINEL="HERMES-PATCH: reload_env boot-env protection"
UNINTERP_SENTINEL="HERMES-PATCH: never write an UNINTERPOLATED"
if grep -Fq "$RELOAD_SENTINEL" "$REPO/hermes_cli/config.py" 2>/dev/null; then
  grn "sentinel   reload_env boot-env protection present in hermes_cli/config.py"
else
  ylw "sentinel   reload_env boot-env protection MISSING in hermes_cli/config.py (patch 28 reverted)"
  if [ "$APPLY" -eq 1 ]; then
    p28="$PATCH_DIR/28-reload-env-boot-env-protection.patch"
    if [ ! -f "$p28" ]; then
      red "  -> patch file missing: $p28 — cannot auto-apply; re-apply by hand"; FAIL=1
    elif git apply --check "$p28" >/dev/null 2>&1 && git apply "$p28" >/dev/null 2>&1; then
      grn "  -> re-applied (clean)"; CHANGED=1
    elif git apply --3way "$p28" 2>/dev/null && grep -Fq "$RELOAD_SENTINEL" "$REPO/hermes_cli/config.py" 2>/dev/null; then
      grn "  -> re-applied (3-way merge, sentinel verified)"; CHANGED=1
    else
      red "  -> RE-APPLY FAILED — resolve by hand: cd $REPO && git apply --3way --reject $p28"; FAIL=1
    fi
  else
    red "  -> Re-run with --apply (--restart to reload gateway)"; FAIL=1
  fi
fi
# Second sentinel: the uninterpolated-${VAR} guard (extension to patch 28).
# Re-apply is handled by the patch-28 block above (both hunks ship in one file);
# this just verifies the extension survived. A missing marker here with the
# boot-env sentinel present would mean a stale patch file — re-run with --apply.
if grep -Fq "$UNINTERP_SENTINEL" "$REPO/hermes_cli/config.py" 2>/dev/null; then
  grn "sentinel   uninterpolated-\${VAR} guard present in hermes_cli/config.py"
else
  red "sentinel   uninterpolated-\${VAR} guard MISSING — patch 28 file is stale; regenerate from a tree that has both hunks"; FAIL=1
fi
# Parse check: don't let a bad apply leave config.py unparseable
if ! "$REPO/venv/bin/python" -c "import ast,sys; ast.parse(open('$REPO/hermes_cli/config.py').read())" 2>/dev/null; then
  red "PARSE FAIL hermes_cli/config.py — patch left the file unparseable"; FAIL=1
else
  grn "parse-ok   hermes_cli/config.py"
fi

# --- 29. Writer-chain conformance (Codex-OAuth primary, GLM failover) --------
# 2026-06-25: The Hermes Codex-OAuth code-writer chain has 4 coupled points
# that must ALL be healthy for OpenCode to use openai/gpt-5.4 (via the codex
# proxy) rather than silently degrading to zai-coding/glm-5.2. §18 checks the
# STRUCTURAL wiring (on hermes update only): binary, script guards, plist file,
# jsonc substring. §29 adds the LIVE conformance check on every run:
#   - Doppler HERMES_WRITER_CODEX == "1" (the only durable home for the flag)
#   - codex-proxy launchd loaded + port 8646 listening (auto-repair if --apply --restart)
#   - opencode.jsonc openai.options.baseURL == http://127.0.0.1:8646/v1 (auto-repair)
#   - OAuth access_token JWT not expired/near-expiry (HARD-REFUSE, no auto-repair)
#   - WRITER_CASCADE[0] in opencode_exec.py matches writer-chain.json primary
# Source of truth: ~/.hermes/writer-chain.json (read-only, authored 2026-06-25).
# Verifier: ~/.hermes/scripts/verify-writer-chain.py (lives outside the repo).
hdr "29. Writer-chain conformance (Codex-OAuth writer — flag / proxy / jsonc / OAuth)"
WC_MANIFEST="$HOME/.hermes/writer-chain.json"
WC_VERIFIER="$HOME/.hermes/scripts/verify-writer-chain.py"
HOOK_PY="${HOOK_PY:-python3}"
# 29a. manifest file exists + parses as JSON
if [ ! -f "$WC_MANIFEST" ]; then
  red "manifest   MISSING $WC_MANIFEST — writer-chain source of truth not present"; FAIL=1
elif ! python3 -c "import json; json.load(open('$WC_MANIFEST'))" 2>/dev/null; then
  red "manifest   $WC_MANIFEST FAILS json.load — file corrupt?"; FAIL=1
else
  grn "manifest   $WC_MANIFEST present + json-valid"
fi
# 29b. verifier script exists + is executable + parses as Python
if [ ! -f "$WC_VERIFIER" ]; then
  red "verifier   MISSING $WC_VERIFIER — cannot run live conformance checks"; FAIL=1
elif [ ! -x "$WC_VERIFIER" ]; then
  red "verifier   $WC_VERIFIER not executable (chmod +x needed)"; FAIL=1
elif ! python3 -c "import ast; ast.parse(open('$WC_VERIFIER').read())" 2>/dev/null; then
  red "verifier   $WC_VERIFIER fails ast.parse — syntax error?"; FAIL=1
else
  grn "verifier   $WC_VERIFIER present + executable + parse-ok"
fi
# 29c. invoke the verifier, surface its pass/fail
if [ -f "$WC_VERIFIER" ] && [ -x "$WC_VERIFIER" ] && [ -f "$WC_MANIFEST" ]; then
  if [ "$APPLY" -eq 1 ] && [ "$RESTART" -eq 1 ]; then
    wc_out=$(python3 "$WC_VERIFIER" --apply --restart --alert 2>&1); wc_rc=$?
  elif [ "$APPLY" -eq 1 ]; then
    wc_out=$(python3 "$WC_VERIFIER" --apply --alert 2>&1); wc_rc=$?
  else
    wc_out=$(python3 "$WC_VERIFIER" 2>&1); wc_rc=$?
  fi
  # Print each verifier output line with an indent for readability
  while IFS= read -r line; do
    printf '           %s\n' "$line"
  done <<< "$wc_out"
  if [ "$wc_rc" -eq 0 ]; then
    grn "verifier   writer-chain conformance PASS (all coupled points healthy)"
  else
    red "verifier   writer-chain conformance FAIL (rc=$wc_rc) — see above; re-run with --apply --restart to auto-repair proxy/jsonc (OAuth needs manual refresh)"; FAIL=1
  fi
fi

# --- 30. PATCH-06 error-message safety (no kickstart command in scheduler.py) ---
# 2026-06-27: the HERMES-PATCH 06 keyguard block in cron/scheduler.py originally
# contained literal `launchctl kickstart -k gui/$UID/ai.hermes.gateway` in both
# the logger.error and RuntimeError messages. The glm-5.2 executor was treating
# these as actionable instructions, copy-pasting the command via execute_code and
# killing the gateway on every "no API key" preflight failure.
#
# Fix: soften the error text to operator-facing messages without a shell command.
#   logger.error: "kickstart ai.hermes.gateway." →
#                 "Contact the operator to restart the Hermes gateway service."
#   RuntimeError: f"`launchctl kickstart -k gui/$UID/ai.hermes.gateway`. " →
#                 "The gateway needs to be restarted by the operator — do not run "
#                 "launchctl commands directly."
#
# DURABILITY: 23-scheduler-pinned-provider-keyguard.patch is in EXPECTED_PATCHES
# and is re-applied by section 1 after `hermes update`. But the .patch file still
# carries the OLD kickstart text. This section runs AFTER section 1 and re-softens
# the two error messages via sed (idempotent — no-op if already applied).
#
# DETECTION: grep for "kickstart ai.hermes.gateway" — present = reverted.
# RE-APPLY: 5 sed commands (logger.error: 2 subs; RuntimeError: 3 subs).
hdr "30. PATCH-06 error-message safety (no kickstart command in scheduler.py)"
SCHED="$REPO/cron/scheduler.py"
KICK06_CHECK="kickstart ai.hermes.gateway"
if ! grep -Fq "$KICK06_CHECK" "$SCHED" 2>/dev/null; then
  grn "sentinel   PATCH-06 error messages safe — no kickstart command in scheduler.py"
else
  ylw "sentinel   PATCH-06 kickstart command still in scheduler.py — softening not applied (23-*.patch reverted it)"
  if [ "$APPLY" -eq 1 ]; then
    # logger.error block: semicolon → period on the "exported %s" line
    sed -i '' 's/exported %s; "/exported %s. "/' "$SCHED"
    # logger.error block: replace kickstart-command line with operator-safe message
    sed -i '' 's|"kickstart ai.hermes.gateway.",|"Contact the operator to restart the Hermes gateway service.",|' "$SCHED"
    # RuntimeError block: remove "Fix: " suffix from the "exported {_env_hint}" line
    sed -i '' 's|exported {_env_hint}. Fix: "|exported {_env_hint}. "|' "$SCHED"
    # RuntimeError block: replace backtick kickstart f-string with operator-safe first line.
    # Note: $UID here is in single quotes — shell does NOT expand it; matches the
    # literal string "$UID" that Python stores in the f-string (Python f-strings use
    # {name}, not $name, so $UID is inert text in the Python source).
    sed -i '' 's|f"`launchctl kickstart -k gui/$UID/ai.hermes.gateway`. "|f"The gateway needs to be restarted by the operator — do not run "|' "$SCHED"
    # RuntimeError block: prepend "launchctl commands directly. " to the final sentence
    sed -i '' 's|f"No inference call was made."|f"launchctl commands directly. No inference call was made."|' "$SCHED"
    # Verify the sentinel is gone
    if ! grep -Fq "$KICK06_CHECK" "$SCHED" 2>/dev/null; then
      grn "  -> re-applied (kickstart command removed from PATCH-06 error messages)"; CHANGED=1
    else
      red "  -> RE-APPLY FAILED — kickstart still present; fix by hand:"; FAIL=1
      red "     grep -n 'kickstart' $SCHED"
    fi
  else
    red "  -> Re-run with --apply to remove the dangerous kickstart command from error messages"; FAIL=1
  fi
fi
# Parse check: ensure the sed pass didn't break scheduler.py syntax
if ! "$REPO/venv/bin/python" -c "import ast,sys; ast.parse(open('$SCHED').read())" 2>/dev/null; then
  red "PARSE FAIL cron/scheduler.py — PATCH-06 message softening left an unparseable file"; FAIL=1
else
  grn "parse-ok   cron/scheduler.py (post PATCH-06 message check)"
fi

# --- 31. Merge-conflict marker check (dormant conflict markers) ------------
# 2026-06-28: the dashboard crashed because an unresolved `<<<<<<< ours` git
# merge-conflict marker sat dormant in agent/anthropic_adapter.py (a
# lazily-imported module) until that code path finally loaded. `hermes update`
# merges upstream over local patches via `git stash apply` / `--3way`, and that
# class of bug — a conflict marker left behind by a bad merge/apply — can
# recur silently on any tracked file, not just the 13 files this script
# already patches. This section is a tripwire, not a re-apply lane: it FAILS
# loudly so the operator resolves the conflict by hand; there is nothing to
# --apply.
#
# Scope: all git-tracked files in the repo (git ls-files respects .gitignore,
# so venv/, node_modules/, __pycache__/ are naturally excluded — this is
# broader than just agent/, hermes_cli/, cron/, which is where the Jun-28 bug
# happened to land but not the only place a bad merge could leave one).
#
# Precision: anchor on '^<<<<<<< ' (marker + space at column 0) so a string
# literal like `"<<<<<<< HEAD\n"` inside a test fixture (see
# tests/hermes_cli/test_update_post_pull_syntax_guard.py, which legitimately
# exercises this exact failure mode) does NOT false-positive — real conflict
# markers are always flush-left, indented literals never are. Any hit is then
# corroborated against '^=======$' and '^>>>>>>> ' in the same file before
# failing, to further rule out a one-off '<<<<<<< ' substring with no real
# conflict block around it.
hdr "31. Merge-conflict marker check (no dormant <<<<<<< markers in tracked files)"
CONFLICT_HITS=$(git ls-files -z | xargs -0 grep -l '^<<<<<<< ' 2>/dev/null)
if [ -z "$CONFLICT_HITS" ]; then
  grn "conflict   no unresolved '<<<<<<< ' markers in any git-tracked file"
else
  conflict_real=0
  while IFS= read -r cf; do
    [ -z "$cf" ] && continue
    if grep -q '^=======$' "$REPO/$cf" 2>/dev/null && grep -q '^>>>>>>> ' "$REPO/$cf" 2>/dev/null; then
      red "CONFLICT MARKER  $cf — live '<<<<<<<' / '=======' / '>>>>>>>' triple present (unresolved merge)"
      conflict_real=1
    else
      ylw "conflict   $cf has a '<<<<<<< ' line but no corroborating '=======' / '>>>>>>> ' in the same file — treating as false positive, not failing"
    fi
  done <<< "$CONFLICT_HITS"
  if [ "$conflict_real" -eq 1 ]; then
    red "  -> resolve by hand: grep -n '<<<<<<<\|=======\|>>>>>>>' <file>, fix the merge, commit or re-run --apply"
    FAIL=1
  fi
fi

# --- 32. Skills-symlink trust check (resolve-before-compare) ---------------
# ~/.hermes/skills is a symlink -> ~/brain/hermes/skills (2026-06-26 versioning
# migration, see brain operations/2026-06-26 Hermes-native skills now
# versioned). tools/skills_tool.py's skill_view() has a trust check that warns
# "skill file is outside the trusted skills directory" if a loaded skill's
# path doesn't sit under the trusted skills dir. If that comparison doesn't
# resolve symlinks on BOTH sides before checking containment, the top-level
# SKILLS_DIR symlink hop alone makes every migrated skill misread as
# untrusted — a noisy false positive on every skill load that trains
# log-readers to ignore real security warnings.
#
# CURRENT STATE (verified 2026-07-02): upstream already carries the fix —
# commit 184c10cf97 (benbarclay) added `.resolve()` on both SKILLS_DIR and
# skill_md before the relative_to() containment check (tools/skills_tool.py
# ~line 1138/1145). `git status` on this file is clean (no local diff), so
# there is no local .patch to re-apply here — this section exists purely as a
# regression tripwire in case a future `hermes update` reintroduces an
# unresolved comparison.
#
# NOTE: 'sentry-monitor' and 'grill-me' still legitimately warn in
# agent.log/gateway.error.log — their SKILL.md files are THEMSELVES symlinks
# to directories entirely outside ~/brain/hermes/skills
# (~/.hermes/repos/ignite-sentinel/hermes/, ~/.agents/skills/grill-me/). That
# is a correct, unrelated trust-boundary signal (those files really do live
# outside the trusted tree) and is intentionally NOT silenced by this check —
# only the top-level-symlink-hop false positive is in scope.
hdr "32. Skills-symlink trust check (resolve-before-compare, tools/skills_tool.py)"
SKT="$REPO/tools/skills_tool.py"
if grep -Fq 'SKILLS_DIR.resolve()' "$SKT" 2>/dev/null && grep -Fq 'skill_md.resolve().relative_to' "$SKT" 2>/dev/null; then
  grn "structural resolve() present on both sides of the trust-prefix comparison"
else
  red "STRUCTURAL REGRESSION — trust-prefix comparison no longer resolves symlinks on both sides"
  red "  Fix: in skill_view()'s trust check ($SKT), compare SKILLS_DIR.resolve() against skill_md.resolve() before relative_to()"
  FAIL=1
fi
# Behavioral: replicate the trust check against every real skill dir under the
# (possibly symlinked) SKILLS_DIR and confirm skills that live under the
# resolved trusted target are NOT flagged outside-trusted purely because of
# the top-level symlink hop. A skill whose SKILL.md is itself individually
# symlinked elsewhere (sentry-monitor, grill-me) is excluded from the
# false-positive count — that's a different, legitimate warning.
SK_RESULT=$("$REPO/venv/bin/python" - "$HOME/.hermes/skills" <<'PY' 2>/dev/null
import sys
from pathlib import Path
skills_dir = Path(sys.argv[1])
if not skills_dir.exists():
    print("SKIP")
    sys.exit(0)
trusted = skills_dir.resolve()
false_positives = []
checked = 0
for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
    smd = d / "SKILL.md"
    if not smd.exists():
        continue
    checked += 1
    try:
        smd.resolve().relative_to(trusted)
    except ValueError:
        try:
            d.resolve().relative_to(trusted)
            is_dir_trusted = True
        except ValueError:
            is_dir_trusted = False
        if is_dir_trusted and not smd.is_symlink():
            false_positives.append(str(smd))
print(f"CHECKED {checked}")
print(f"FALSE_POSITIVES {len(false_positives)}")
for f in false_positives:
    print(f"  {f}")
PY
)
if [ "$SK_RESULT" = "SKIP" ]; then
  ylw "behavior   $HOME/.hermes/skills does not exist — skipped"
elif [ -z "$SK_RESULT" ]; then
  red "behavior   sentinel script produced no output — venv python or skills_tool import may be broken"; FAIL=1
else
  sk_checked=$(printf '%s\n' "$SK_RESULT" | awk '/^CHECKED /{print $2}')
  sk_fp=$(printf '%s\n' "$SK_RESULT" | awk '/^FALSE_POSITIVES /{print $2}')
  if [ "${sk_fp:-1}" = "0" ]; then
    grn "behavior   $sk_checked skill(s) under the (symlinked) skills dir all resolve inside the trusted tree (no top-level-hop false positives)"
  else
    red "behavior   $sk_fp of ${sk_checked:-?} skill(s) false-flag outside-trusted purely from the top-level symlink hop:"
    printf '%s\n' "$SK_RESULT" | sed -n '3,$p' | while IFS= read -r fpline; do red "             $fpline"; done
    FAIL=1
  fi
fi

hdr "Result"
if [ "$FAIL" -eq 0 ]; then grn "All patches applied, parse-clean, wired, and live."; exit 0
else red "Issues found above. Re-run with --apply --restart to remediate."; exit 1; fi
