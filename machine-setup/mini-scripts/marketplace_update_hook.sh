#!/usr/bin/env bash
# Throttled SessionStart hook for the ignite-marketplace.
#
# Runs at most ONCE per day (24h throttle) on CC SessionStart. Updates
# the marketplace catalog + every installed ignite-marketplace plugin.
# Safe to call repeatedly — the throttle ensures cost stays low.
#
# Throttle stamp: ~/.hermes/state/marketplace-update.stamp
# On success: stamp is touched with current mtime.
# On failure: stamp is left untouched (next SessionStart will retry).
#
# 3/3 cutover (2026-06-21): added by Hermes executor for task 86e1z37j6.

set -u
STAMP="$HOME/.hermes/state/marketplace-update.stamp"
THROTTLE_SECS=86400  # 24h
mkdir -p "$(dirname "$STAMP")"

if [[ -f "$STAMP" ]]; then
  age=$(( $(date +%s) - $(stat -f %m "$STAMP") ))
  if (( age < THROTTLE_SECS )); then
    # Quiet: under throttle, do nothing
    exit 0
  fi
fi

# Use a hermes-safe gitconfig that talks to Colin's classic PAT (not the
# Hermes App — the App token doesn't have access to colingreig's private repos).
export GIT_CONFIG_GLOBAL="${GIT_CONFIG_OVERRIDE_PATH:-/Users/colingreig/.gitconfig}"
# Pull a fresh token from gh (handles the macOS keychain swap)
if command -v gh >/dev/null 2>&1; then
  gh_token="$(gh auth token 2>/dev/null || true)"
  if [[ -n "$gh_token" ]]; then
    export GITHUB_PERSONAL_ACCESS_TOKEN="$gh_token"
  fi
fi

LOG="$HOME/.hermes/logs/marketplace-update-hook.log"
ts() { date '+%F %T %Z'; }
mkdir -p "$(dirname "$LOG")"

echo "$(ts) SessionStart: throttled marketplace update starting" >> "$LOG"

# 1. Refresh the marketplace catalog (cheap — small private repo)
if command -v claude >/dev/null 2>&1; then
  claude plugin marketplace update ignite-marketplace >> "$LOG" 2>&1 || echo "$(ts) WARN: marketplace update failed (rc=$?)" >> "$LOG"
  claude plugin marketplace update ignite-skills >> "$LOG" 2>&1 || echo "$(ts) WARN: ignite-skills marketplace update failed (rc=$?)" >> "$LOG"
fi

# 2. Update every installed ignite-marketplace plugin (one per line, skip on error)
if command -v claude >/dev/null 2>&1; then
  for plugin in $(claude plugin list 2>/dev/null | grep -oE '[a-z0-9-]+@(ignite-marketplace|ignite-skills)' || true); do
    echo "$(ts) updating $plugin" >> "$LOG"
    claude plugin update "$plugin" >> "$LOG" 2>&1 || echo "$(ts) WARN: update $plugin failed (rc=$?)" >> "$LOG"
  done
fi

# Touch stamp only on completion (any failure above = no stamp, next SessionStart retries)
touch "$STAMP"
echo "$(ts) SessionStart: throttled marketplace update complete" >> "$LOG"
exit 0
