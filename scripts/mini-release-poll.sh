#!/usr/bin/env bash
# Local-only Mini release poller. The release script owns ref resolution,
# advancement validation, locking, receipts, cutover, and rollback.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CUT="$HERMES_HOME/runtime-current/scripts/mini-release-cut.sh"
PYTHON="$HERMES_HOME/runtime-current/venv/bin/python"
CONTROL="$HERMES_HOME/runtime-current/scripts/mini-release-poll-control.py"

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

if ! CERTIFIED_SHA="$("$PYTHON" "$CONTROL" authorize --print-certified-sha)"; then
  printf 'mini-release-poll: frozen or control state unavailable; refusing release\n' >&2
  exit 1
fi

exec "$CUT" --ref prod-live-patches --certified-sha "$CERTIFIED_SHA" --if-advanced --prune
