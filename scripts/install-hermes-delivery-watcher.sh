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
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WATCHER_SOURCE="$SCRIPT_DIR/hermes_delivery_watch.py"
CORRELATOR_SOURCE="$SCRIPT_DIR/task_delivery.py"
SOURCE_PLIST="$SCRIPT_DIR/launchd/com.colingreig.hermes.delivery-watch.plist"
TARGET_DIR="$HERMES_HOME/libexec/delivery-watch"
WATCHER="$TARGET_DIR/hermes_delivery_watch.py"
CORRELATOR="$TARGET_DIR/task_delivery.py"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.colingreig.hermes.delivery-watch.plist"
LABEL="com.colingreig.hermes.delivery-watch"
DOMAIN="gui/$(id -u)"

[ -f "$WATCHER_SOURCE" ] && [ ! -L "$WATCHER_SOURCE" ] \
  || { echo "ERROR: missing or symlinked watcher source: $WATCHER_SOURCE" >&2; exit 1; }
[ -f "$CORRELATOR_SOURCE" ] && [ ! -L "$CORRELATOR_SOURCE" ] \
  || { echo "ERROR: missing or symlinked correlator source: $CORRELATOR_SOURCE" >&2; exit 1; }
[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

PYTHON=""
python_candidates=()
[ -n "${HERMES_DELIVERY_WATCH_PYTHON:-}" ] \
  && python_candidates+=("$HERMES_DELIVERY_WATCH_PYTHON")
python_candidates+=(
  "$REPO_ROOT/.venv/bin/python"
  "$REPO_ROOT/venv/bin/python"
)
command -v python3 >/dev/null 2>&1 \
  && python_candidates+=("$(command -v python3)")
python_candidates+=("/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3")
for candidate in "${python_candidates[@]}"; do
  [ -x "$candidate" ] || continue
  if PYTHONPATH="$SCRIPT_DIR" "$candidate" -c \
    'import yaml, task_delivery, hermes_delivery_watch' >/dev/null 2>&1; then
    PYTHON="$(CDPATH= cd -- "$(dirname -- "$candidate")" && pwd -P)/$(basename "$candidate")"
    break
  fi
done
[ -n "$PYTHON" ] || {
  echo "ERROR: no Python can import the watcher and required PyYAML dependency" >&2
  exit 1
}

mkdir -p "$HERMES_HOME/logs" "$HERMES_HOME/state/task-delivery-watch" "$TARGET_DIR"
chmod 0700 "$TARGET_DIR"

install_source() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local swap
  swap="$(mktemp "$TARGET_DIR/.${destination##*/}.swap.XXXXXX")"
  cp "$source" "$swap"
  chmod "$mode" "$swap"
  mv -f "$swap" "$destination"
}
install_source "$WATCHER_SOURCE" "$WATCHER" 0755
install_source "$CORRELATOR_SOURCE" "$CORRELATOR" 0644

# Prove the exact installed files load under the interpreter written into the
# LaunchAgent. Installation never manufactures behavioral configuration.
PYTHONPATH="$TARGET_DIR" "$PYTHON" -c \
  'import yaml, task_delivery, hermes_delivery_watch' >/dev/null
"$PYTHON" "$WATCHER" --status >/dev/null

mkdir -p "$(dirname "$TARGET_PLIST")"
tmp="$(mktemp "$(dirname "$TARGET_PLIST")/.${LABEL}.swap.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
"$PYTHON" - "$SOURCE_PLIST" "$tmp" "$PYTHON" "$WATCHER" "$HOME" "$HERMES_HOME" <<'PY'
import sys
from pathlib import Path

source, target, python, watcher, home, hermes_home = sys.argv[1:]
payload = Path(source).read_text(encoding="utf-8")
for marker, value in (
    ("__HERMES_DELIVERY_WATCH_PYTHON__", python),
    ("__HERMES_DELIVERY_WATCH_SCRIPT__", watcher),
    ("__HOME__", home),
    ("__HERMES_HOME__", hermes_home),
):
    payload = payload.replace(marker, value)
Path(target).write_text(payload, encoding="utf-8")
PY
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
  PYTHONPATH="$TARGET_DIR" "$PYTHON" - "$HERMES_HOME/config.yaml" <<'PY'
import sys
from pathlib import Path
from hermes_delivery_watch import _load_config

_load_config(Path(sys.argv[1]))
PY
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
  echo "Enabled $DOMAIN/$LABEL"
else
  echo "Not loaded. Review delivery_watch config, then re-run with --install-and-enable."
fi
