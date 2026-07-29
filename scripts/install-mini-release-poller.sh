#!/usr/bin/env bash
# Install the source-controlled Mini release poller LaunchAgent.
#
# This script is deliberately not called by a release cut: the first install is
# an explicit bootstrap operation. It refuses installation unless the poller's
# locked advancement preflight is safe (equal or strict descendant).
set -euo pipefail

ACTION=""
ACTOR=""
REASON=""
CERTIFIED_SHA=""
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --install|--install-and-enable|--freeze|--unfreeze|--status)
      [ -z "$ACTION" ] || { echo "ERROR: choose exactly one action" >&2; exit 2; }
      ACTION="${1#--}"
      shift
      ;;
    --actor) ACTOR="${2:-}"; shift 2 ;;
    --reason) REASON="${2:-}"; shift 2 ;;
    --certified-sha) CERTIFIED_SHA="${2:-}"; shift 2 ;;
    -h|--help|"")
      cat <<'EOF'
Usage:
  install-mini-release-poller.sh --install --certified-sha <full-sha>
  install-mini-release-poller.sh --install-and-enable --actor <id> --reason <why> --certified-sha <full-sha>
  install-mini-release-poller.sh --freeze --actor <id> --reason <why>
  install-mini-release-poller.sh --unfreeze --actor <id> --reason <why> --certified-sha <full-sha>
  install-mini-release-poller.sh --status
EOF
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: ${1:-}" >&2
      exit 2
      ;;
  esac
done
[ -n "$ACTION" ] || { echo "ERROR: one action is required" >&2; exit 2; }

[ "$(uname -s)" = "Darwin" ] || {
  echo "ERROR: Mini release polling is macOS-only" >&2
  exit 1
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CURRENT="$HERMES_HOME/runtime-current"
CUT="$CURRENT/scripts/mini-release-cut.sh"
CONTROL="$CURRENT/scripts/mini-release-poll-control.py"
PYTHON="$CURRENT/venv/bin/python"
SOURCE_PLIST="$CURRENT/scripts/launchd/com.colingreig.hermes.release-poll.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.colingreig.hermes.release-poll.plist"
LABEL="com.colingreig.hermes.release-poll"
DOMAIN="gui/$(id -u)"

[ -x "$CUT" ] || { echo "ERROR: missing release cutter: $CUT" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "ERROR: missing runtime Python: $PYTHON" >&2; exit 1; }
[ -f "$CONTROL" ] && [ ! -L "$CONTROL" ] \
  || { echo "ERROR: missing or symlinked governed poll control: $CONTROL" >&2; exit 1; }

case "$ACTION" in
  status)
    exec "$PYTHON" "$CONTROL" status
    ;;
  freeze)
    [ -n "$ACTOR" ] && [ -n "$REASON" ] \
      || { echo "ERROR: --freeze requires --actor and --reason" >&2; exit 2; }
    exec "$PYTHON" "$CONTROL" freeze --actor "$ACTOR" --reason "$REASON"
    ;;
  unfreeze)
    [ -n "$ACTOR" ] && [ -n "$REASON" ] && [ -n "$CERTIFIED_SHA" ] \
      || { echo "ERROR: --unfreeze requires --actor, --reason, and --certified-sha" >&2; exit 2; }
    exec "$PYTHON" "$CONTROL" unfreeze \
      --actor "$ACTOR" --reason "$REASON" --certified-sha "$CERTIFIED_SHA"
    ;;
  install)
    [ -n "$CERTIFIED_SHA" ] \
      || { echo "ERROR: --install requires --certified-sha" >&2; exit 2; }
    ;;
  install-and-enable)
    [ -n "$ACTOR" ] && [ -n "$REASON" ] && [ -n "$CERTIFIED_SHA" ] \
      || { echo "ERROR: --install-and-enable requires --actor, --reason, and --certified-sha" >&2; exit 2; }
    ;;
esac

[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

# The dedicated preflight takes the real cut lock and fetches current origin
# metadata, but exits before any build, switch, service action, or receipt
# write. Equal and strict descendant states return 0; behind/diverged states
# return nonzero.
"$CUT" --ref "$CERTIFIED_SHA" --certified-sha "$CERTIFIED_SHA" --preflight >/dev/null

mkdir -p "$(dirname "$TARGET_PLIST")"
tmp="$(mktemp "$(dirname "$TARGET_PLIST")/.${LABEL}.swap.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cp "$SOURCE_PLIST" "$tmp"
chmod 0644 "$tmp"
plutil -lint "$tmp" >/dev/null
mv -fh "$tmp" "$TARGET_PLIST"
trap - EXIT
echo "Installed $TARGET_PLIST"

if [ "$ACTION" = "install-and-enable" ]; then
  "$PYTHON" "$CONTROL" unfreeze \
    --actor "$ACTOR" --reason "$REASON" --certified-sha "$CERTIFIED_SHA" >/dev/null
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"; then
    "$PYTHON" "$CONTROL" freeze \
      --actor "$ACTOR" --reason "enable failed: $REASON" >/dev/null || true
    echo "ERROR: enable failed; release polling left frozen" >&2
    exit 1
  fi
  echo "Enabled $DOMAIN/$LABEL"
else
  echo "Not loaded. Poll-control state was not changed."
fi
