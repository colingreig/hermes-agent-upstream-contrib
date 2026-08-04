#!/usr/bin/env bash
# Local-only Mini release poller. The release script owns ref resolution,
# advancement validation, locking, receipts, cutover, and rollback.
set -euo pipefail

# Liveness heartbeat (ClickUp 86e2ky37p). Printed unconditionally, before any
# check below that can exit early (frozen state, missing dependency, stale
# authority, etc.), so this script's StandardOutPath log advances on every
# launchd-scheduled invocation regardless of outcome. That lets an
# independent liveness contract (fleet_outcome_contracts.json's
# com.colingreig.hermes.release-poll entry) alarm on a genuinely unloaded or
# non-executing poller while staying silent on the common, healthy case of
# "ran, found nothing new to cut."
printf 'mini-release-poll: heartbeat ts=%s pid=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CUT="$HERMES_HOME/runtime-current/scripts/mini-release-cut.sh"
PYTHON="$HERMES_HOME/runtime-current/venv/bin/python"
CONTROL="$HERMES_HOME/runtime-current/scripts/mini-release-poll-control.py"
CERTIFIER="$HERMES_HOME/runtime-current/scripts/certify_prod_live_patches.py"

# runtime-current pointer probe (ClickUp 86e2kt3yr). Every path below — and
# every cron job on this box — dereferences $HERMES_HOME/runtime-current. On
# 2026-08-02 that pointer was left as a bare-name relative symlink by an
# out-of-band write and nothing noticed for minutes: the checks below would
# have reported only "release cutter missing", which reads as a broken release
# rather than a broken pointer. The poller is the fleet's most frequent
# toucher of this path (~15 min), so it is also the cheapest place to get
# detection cadence without a new LaunchAgent. The reason string is emitted on
# stdout with a stable, greppable prefix so a fleet-outcome contract can alarm
# on it distinguishably.
POINTER="$HERMES_HOME/runtime-current"
pointer_defect() {
  if [ ! -L "$POINTER" ]; then
    [ -e "$POINTER" ] && { printf 'not a symlink'; return 0; }
    printf 'missing'
    return 0
  fi
  POINTER_RAW="$(readlink "$POINTER")" || { printf 'unreadable'; return 0; }
  case "$POINTER_RAW" in
    /*) ;;
    *) printf 'relative symlink (must be absolute): -> %s' "$POINTER_RAW"; return 0 ;;
  esac
  case "$POINTER_RAW" in
    "$HERMES_HOME/releases/"*/*) printf 'target is not a direct child of releases/: -> %s' "$POINTER_RAW"; return 0 ;;
    "$HERMES_HOME/releases/"?*) ;;
    *) printf 'target escapes releases/: -> %s' "$POINTER_RAW"; return 0 ;;
  esac
  [ -d "$POINTER_RAW" ] || { printf 'dangling target: -> %s' "$POINTER_RAW"; return 0; }
  [ -x "$POINTER_RAW/venv/bin/python" ] || { printf 'no usable runtime Python: -> %s' "$POINTER_RAW"; return 0; }
  return 1
}
if POINTER_DEFECT="$(pointer_defect)"; then
  printf 'mini-release-poll: runtime-current pointer corrupt: %s\n' "$POINTER_DEFECT"
  printf 'mini-release-poll: runtime-current pointer corrupt: %s\n' "$POINTER_DEFECT" >&2
  printf 'mini-release-poll: repair with: mini-release-cut.sh --repair-pointer\n' >&2
  exit 1
fi

if [ ! -x "$CUT" ]; then
  printf 'mini-release-poll: release cutter missing or not executable: %s\n' "$CUT" >&2
  exit 1
fi
[ -x "$PYTHON" ] || {
  printf 'mini-release-poll: runtime Python missing or not executable: %s\n' "$PYTHON" >&2
  exit 1
}
[ -f "$CONTROL" ] && [ ! -L "$CONTROL" ] || {
  printf 'mini-release-poll: governed control missing or symlinked: %s\n' "$CONTROL" >&2
  exit 1
}
[ -f "$CERTIFIER" ] && [ ! -L "$CERTIFIER" ] || {
  printf 'mini-release-poll: promotion certifier missing or symlinked: %s\n' "$CERTIFIER" >&2
  exit 1
}

if ! CERTIFIED_SHA="$("$PYTHON" "$CONTROL" authorize --print-certified-sha)"; then
  printf 'mini-release-poll: frozen or control state unavailable; refusing release\n' >&2
  exit 1
fi
if ! RECEIPT_ID="$("$PYTHON" "$CONTROL" authorize --print-authority-receipt-id)"; then
  printf 'mini-release-poll: promotion receipt authority unavailable; refusing release\n' >&2
  exit 1
fi
case "$RECEIPT_ID" in
  *[!0-9a-f]*|'') printf 'mini-release-poll: invalid promotion receipt ID\n' >&2; exit 1 ;;
esac
[ "${#RECEIPT_ID}" -eq 64 ] || {
  printf 'mini-release-poll: invalid promotion receipt ID length\n' >&2
  exit 1
}

AUTHORITY_REF="refs/tags/prod-live-patches-promotion-$RECEIPT_ID"
LOCAL_AUTHORITY_REF="refs/hermes-promotion-authority/$RECEIPT_ID"
git -C "$HERMES_HOME/runtime-current" fetch --no-tags origin \
  "+refs/heads/prod-live-patches:refs/remotes/origin/prod-live-patches" \
  "+$AUTHORITY_REF:$LOCAL_AUTHORITY_REF"
REMOTE_SHA="$(git -C "$HERMES_HOME/runtime-current" rev-parse \
  --verify refs/remotes/origin/prod-live-patches^{commit})"
[ "$REMOTE_SHA" = "$CERTIFIED_SHA" ] || {
  printf 'mini-release-poll: remote prod SHA does not match governed control\n' >&2
  exit 1
}
[ "$(git -C "$HERMES_HOME/runtime-current" cat-file -t "$LOCAL_AUTHORITY_REF")" = blob ] || {
  printf 'mini-release-poll: promotion authority ref is not a receipt blob\n' >&2
  exit 1
}
RECEIPT_PATH="$(mktemp "$HERMES_HOME/releases/.promotion-authority.XXXXXX")"
cleanup() { rm -f "$RECEIPT_PATH"; }
trap cleanup EXIT
git -C "$HERMES_HOME/runtime-current" cat-file blob "$LOCAL_AUTHORITY_REF" > "$RECEIPT_PATH"
"$PYTHON" "$CERTIFIER" verify-receipt \
  --receipt "$RECEIPT_PATH" \
  --receipt-id "$RECEIPT_ID" \
  --head-sha "$CERTIFIED_SHA" >/dev/null

# Exit 75 (EX_TEMPFAIL, mini-release-cut.sh's RELEASE_DEFER_EXIT) means the cut
# was DEFERRED because the fleet was still working: nothing switched, the drain
# marker was removed, and poll-control was deliberately left unfrozen so this
# very wrapper retries on its next 15-minute invocation (ClickUp 86e2md9ck).
# Reporting that as a poller failure would be misleading, so it is surfaced as
# its own log line and a clean exit. Repeated deferrals are counted and alarmed
# by the release-cut-drain-deferral fleet-outcome contract, which reads the
# cutter's own "release cut deferred:" line out of this same log — so a fleet
# that can never quiesce pages rather than quietly never deploying.
CUT_STATUS=0
"$CUT" --ref prod-live-patches \
  --certified-sha "$CERTIFIED_SHA" \
  --promotion-receipt-id "$RECEIPT_ID" \
  --if-advanced --prune || CUT_STATUS=$?
if [ "$CUT_STATUS" -eq 75 ]; then
  printf 'mini-release-poll: cut deferred by a busy fleet; authorization intact, retrying next cycle\n'
  exit 0
fi
exit "$CUT_STATUS"
