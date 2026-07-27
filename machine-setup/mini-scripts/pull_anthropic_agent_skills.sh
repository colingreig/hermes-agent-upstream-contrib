#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/colingreig/.claude/plugins/marketplaces/anthropic-agent-skills"
HERMES_HOME="${HERMES_HOME:-/Users/colingreig/.hermes}"
PYTHON="$HERMES_HOME/runtime-current/venv/bin/python"
EVIDENCE="$HERMES_HOME/state/skill-pulls/anthropic-agent-skills-success.json"
RECORDER="$HERMES_HOME/scripts/record_skill_pull_success.py"
GUARD="$HERMES_HOME/scripts/skill_pull_guard.py"
LOG="$HERMES_HOME/logs/anthropic-skills-pull.log"
LOCK="$HERMES_HOME/state/skill-pulls/anthropic-agent-skills.lock"
CATALOG_LOCK="$HERMES_HOME/state/skill-pulls/catalog-update.lock"
GENERATION="$HERMES_HOME/state/skill-pulls/catalog-generation.json"

mkdir -p "$(dirname "$EVIDENCE")" "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

[ -x "$PYTHON" ] || { printf '%s ERROR runtime Python missing\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; exit 1; }
[ -f "$RECORDER" ] && [ ! -L "$RECORDER" ] || { printf '%s ERROR evidence recorder missing\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; exit 1; }
[ -f "$GUARD" ] && [ ! -L "$GUARD" ] || { printf '%s ERROR pull guard missing\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; exit 1; }
[ -d "$ROOT/.git" ] || { printf '%s ERROR canonical repository missing: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$ROOT"; exit 1; }
RESOLVED="$(cd -P "$ROOT" && pwd -P)"
[ "$RESOLVED" = "$ROOT" ] || { printf '%s ERROR repository escapes canonical root: %s -> %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$ROOT" "$RESOLVED"; exit 1; }

LOCK_TOKEN="$("$PYTHON" "$GUARD" acquire --lock "$LOCK" --owner-pid "$$")"
CATALOG_LOCK_TOKEN=""
UPDATING_PUBLISHED=0
PULL_SUCCEEDED=0
cleanup() {
  status=$?
  if [ "$UPDATING_PUBLISHED" -eq 1 ] && [ "$PULL_SUCCEEDED" -eq 0 ]; then
    "$PYTHON" "$RECORDER" --source anthropic-agent-skills \
      --generation-target "$GENERATION" --generation-state failed \
      --operation-id "$CATALOG_LOCK_TOKEN" \
      || printf '%s ERROR could not publish failed catalog state\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  fi
  if [ -n "$CATALOG_LOCK_TOKEN" ]; then
    "$PYTHON" "$GUARD" release --lock "$CATALOG_LOCK" --token "$CATALOG_LOCK_TOKEN" \
      || printf '%s ERROR could not release catalog update lock\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  fi
  "$PYTHON" "$GUARD" release --lock "$LOCK" --token "$LOCK_TOKEN" \
    || printf '%s ERROR could not release owned pull lock\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  return "$status"
}
trap cleanup EXIT
"$PYTHON" "$GUARD" clean --root "$ROOT" --path skills
CATALOG_LOCK_TOKEN="$("$PYTHON" "$GUARD" acquire --lock "$CATALOG_LOCK" --owner-pid "$$")"
"$PYTHON" "$RECORDER" --source anthropic-agent-skills \
  --generation-target "$GENERATION" --generation-state updating \
  --operation-id "$CATALOG_LOCK_TOKEN"
UPDATING_PUBLISHED=1
BEFORE="$(git -C "$ROOT" rev-parse --verify HEAD)"
git -C "$ROOT" fetch --prune origin main
git -C "$ROOT" merge --ff-only FETCH_HEAD
"$PYTHON" "$GUARD" clean --root "$ROOT" --path skills
COMMIT="$(git -C "$ROOT" rev-parse --verify HEAD)"
"$PYTHON" "$RECORDER" --target "$EVIDENCE" --source anthropic-agent-skills \
  --root "$ROOT" --commit "$COMMIT" --generation-target "$GENERATION" \
  --changed-from "$BEFORE" --operation-id "$CATALOG_LOCK_TOKEN"
PULL_SUCCEEDED=1
UPDATING_PUBLISHED=0
printf '%s pull OK source=anthropic-agent-skills commit=%s\n' \
  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$COMMIT"
