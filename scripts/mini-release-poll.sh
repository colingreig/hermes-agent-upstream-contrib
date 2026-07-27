#!/usr/bin/env bash
# Local-only Mini release poller. The release script owns ref resolution,
# advancement validation, locking, receipts, cutover, and rollback.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CUT="$HERMES_HOME/runtime-current/scripts/mini-release-cut.sh"

if [ ! -x "$CUT" ]; then
  printf 'mini-release-poll: release cutter missing or not executable: %s\n' "$CUT" >&2
  exit 1
fi

exec "$CUT" --ref prod-live-patches --if-advanced
