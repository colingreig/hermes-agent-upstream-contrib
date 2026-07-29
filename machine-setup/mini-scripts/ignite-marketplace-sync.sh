#!/bin/zsh
# Wrapper for launchd com.colingreig.ignite-marketplace-sync.
# Auth: GITHUB_PERSONAL_ACCESS_TOKEN must be in env (resolved from 1Password via the
# read-only service-account token at ~/.config/op-runtime-token). If absent, fall back
# to /tmp/gh_token.txt (cron-cached file).
set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG="$HOME/.hermes/logs/ignite-marketplace-sync.log"
ts() { date '+%F %T %Z'; }
mkdir -p "$(dirname "$LOG")"

# Auth: this job mirrors UPSTREAM source repos under the *private* AI-Marketing-Hub
# org (see sources.json). The "Hermes Dev Assistant" GitHub App is installed only on
# colingreig's account, so an App installation token 404s on every AI-Marketing-Hub
# read — it CANNOT do this job. Only Colin's PAT (a member of AI-Marketing-Hub) can.
# So we resolve GITHUB_PERSONAL_ACCESS_TOKEN from 1Password (Doppler decommissioned
# 2026-07-03) using the read-only service-account token — same pattern as
# ignite-skills-pull.sh. (Do not "fix" this to the App token: it lacks
# AI-Marketing-Hub access by design.)
if [ -z "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ] && [ -s "$HOME/.config/op-runtime-token" ]; then
  # Resolve via the 1Password service-account SDK, NEVER the `op` CLI (op read hangs
  # + pops a desktop consent dialog when tokenless — op-cli-dialog-loop 2026-07-05).
  T="$("$HOME/.hermes/runtime-current/venv/bin/python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from op_sdk_resolve import resolve_refs; sys.stdout.write(resolve_refs([sys.argv[2]]).get(sys.argv[2], ""))' \
    "$HOME/.hermes/scripts" 'op://Dev Toolbox/dev/GITHUB_PERSONAL_ACCESS_TOKEN' 2>>"$LOG" || true)"
  if [ -n "$T" ]; then
    export GITHUB_PERSONAL_ACCESS_TOKEN="$T"
    echo "$(ts) auth: resolved GITHUB_PERSONAL_ACCESS_TOKEN from 1Password" >> "$LOG"
  fi
  unset T
fi

# Fallback: cron-cached PAT file.
if [ -z "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ] && [ -s "/tmp/gh_token.txt" ]; then
  export GITHUB_PERSONAL_ACCESS_TOKEN="$(cat /tmp/gh_token.txt)"
fi

if [ -z "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then
  echo "$(ts) FAIL: no GH token (Doppler resolve failed; not in env or /tmp/gh_token.txt)" >> "$LOG"
  exit 1
fi

# gh CLI reads GH_TOKEN, not GITHUB_PERSONAL_ACCESS_TOKEN — mirror it so the
# `gh api` reads inside sync.sh authenticate with the same PAT.
export GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN"

REPO="$HOME/dev/ignite-marketplace"
if [ ! -d "$REPO" ]; then
  echo "$(ts) FAIL: $REPO missing" >> "$LOG"; exit 1
fi

echo "$(ts) sync.sh starting" >> "$LOG"
bash "$REPO/scripts/sync.sh" >> "$LOG" 2>&1
RC=$?
echo "$(ts) sync.sh exited $RC" >> "$LOG"
exit $RC
