#!/usr/bin/env bash
# Install the source-controlled, read-only MacBook delivery watcher LaunchAgent.
set -euo pipefail

ENABLE=0
case "${1:-}" in
  --install) ;;
  --install-and-enable) ENABLE=1 ;;
  -h|--help|"")
    echo "Usage: install-hermes-delivery-watcher.sh --install|--install-and-enable"
    exit 0
    ;;
  *)
    echo "ERROR: unknown argument: ${1:-}" >&2
    exit 2
    ;;
esac

[ "$(uname -s)" = "Darwin" ] || {
  echo "ERROR: the delivery watcher LaunchAgent is macOS-only" >&2
  exit 1
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CURRENT="$HERMES_HOME/runtime-current"
WATCHER="$CURRENT/scripts/hermes_delivery_watch.py"
CORRELATOR="$CURRENT/scripts/task_delivery.py"
SOURCE_PLIST="$CURRENT/scripts/launchd/com.colingreig.hermes.delivery-watch.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.colingreig.hermes.delivery-watch.plist"
LABEL="com.colingreig.hermes.delivery-watch"
DOMAIN="gui/$(id -u)"

[ -f "$WATCHER" ] && [ ! -L "$WATCHER" ] \
  || { echo "ERROR: missing or symlinked watcher: $WATCHER" >&2; exit 1; }
[ -f "$CORRELATOR" ] && [ ! -L "$CORRELATOR" ] \
  || { echo "ERROR: missing or symlinked correlator: $CORRELATOR" >&2; exit 1; }
[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

# Installation never manufactures behavioral config. The operator must review
# the read-only collectors and alert/dead-man endpoints first.
/usr/bin/python3 "$WATCHER" --status >/dev/null
mkdir -p "$HERMES_HOME/logs" "$HERMES_HOME/state/task-delivery-watch"
mkdir -p "$(dirname "$TARGET_PLIST")"
tmp="$(mktemp "$(dirname "$TARGET_PLIST")/.${LABEL}.swap.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
sed \
  -e "s#/Users/colingreig/.hermes#$HERMES_HOME#g" \
  -e "s#/Users/colingreig#$HOME#g" \
  "$SOURCE_PLIST" >"$tmp"
chmod 0644 "$tmp"
plutil -lint "$tmp" >/dev/null
mv -fh "$tmp" "$TARGET_PLIST"
trap - EXIT
echo "Installed $TARGET_PLIST"

if [ "$ENABLE" -eq 1 ]; then
  [ -f "$HERMES_HOME/config.yaml" ] || {
    echo "ERROR: configure delivery_watch in $HERMES_HOME/config.yaml before enabling" >&2
    exit 1
  }
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
  echo "Enabled $DOMAIN/$LABEL"
else
  echo "Not loaded. Review delivery_watch config, then re-run with --install-and-enable."
fi
