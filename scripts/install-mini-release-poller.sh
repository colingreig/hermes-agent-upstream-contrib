#!/usr/bin/env bash
# Install the source-controlled Mini release poller LaunchAgent.
#
# This script is deliberately not called by a release cut: the first install is
# an explicit bootstrap operation. It refuses installation unless the poller's
# locked advancement preflight is safe (equal or strict descendant).
set -euo pipefail

ENABLE=0
case "${1:-}" in
  --install) ;;
  --install-and-enable) ENABLE=1 ;;
  -h|--help|"")
    echo "Usage: install-mini-release-poller.sh --install|--install-and-enable"
    exit 0
    ;;
  *)
    echo "ERROR: unknown argument: ${1:-}" >&2
    exit 2
    ;;
esac

[ "$(uname -s)" = "Darwin" ] || {
  echo "ERROR: Mini release polling is macOS-only" >&2
  exit 1
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CURRENT="$HERMES_HOME/runtime-current"
CUT="$CURRENT/scripts/mini-release-cut.sh"
SOURCE_PLIST="$CURRENT/scripts/launchd/com.colingreig.hermes.release-poll.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.colingreig.hermes.release-poll.plist"
LABEL="com.colingreig.hermes.release-poll"
DOMAIN="gui/$(id -u)"

[ -x "$CUT" ] || { echo "ERROR: missing release cutter: $CUT" >&2; exit 1; }
[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

# The dedicated preflight takes the real cut lock and fetches current origin
# metadata, but exits before any build, switch, service action, or receipt
# write. Equal and strict descendant states return 0; behind/diverged states
# return nonzero.
"$CUT" --ref prod-live-patches --preflight >/dev/null

mkdir -p "$(dirname "$TARGET_PLIST")"
tmp="$(mktemp "$(dirname "$TARGET_PLIST")/.${LABEL}.swap.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cp "$SOURCE_PLIST" "$tmp"
chmod 0644 "$tmp"
plutil -lint "$tmp" >/dev/null
mv -fh "$tmp" "$TARGET_PLIST"
trap - EXIT
echo "Installed $TARGET_PLIST"

if [ "$ENABLE" -eq 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
  echo "Enabled $DOMAIN/$LABEL"
else
  echo "Not loaded. Re-run with --install-and-enable after reviewing the preflight."
fi
